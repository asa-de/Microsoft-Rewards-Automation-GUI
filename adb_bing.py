"""真机 ADB：必应 App「应用」签到。

正确路径（非网页搜索跳转）：
  主页 → 点头像 → 积分/Microsoft Rewards → 积分页「签入」栏点当日积分

小米/HyperOS 默认禁止模拟点击，需开启：
  开发者选项 → USB 调试（安全设置）
否则只能打开 App，无法点签入。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Optional

LogFn = Callable[[str], None]
StopFn = Callable[[], bool]

BING_PKG = "com.microsoft.bing"
BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _exe_dir() -> Path:
    return Path(__file__).resolve().parent


def resolve_adb_path(explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
    bundled = _exe_dir() / "Assets" / "platform-tools" / "adb.exe"
    if bundled.exists():
        return bundled
    which = shutil.which("adb")
    return Path(which) if which else None


def _run_adb(adb: Path, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(adb), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def list_devices(adb: Optional[Path] = None) -> list[dict]:
    adb = adb or resolve_adb_path()
    if not adb:
        return []
    cp = _run_adb(adb, "devices", "-l")
    out = []
    for line in (cp.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        meta = {"serial": serial, "state": state}
        for token in parts[2:]:
            if ":" in token:
                k, v = token.split(":", 1)
                meta[k] = v
        out.append(meta)
    return out


def pick_device(adb: Optional[Path] = None, serial: Optional[str] = None) -> Optional[str]:
    devices = [d for d in list_devices(adb) if d.get("state") == "device"]
    if not devices:
        return None
    if serial:
        for d in devices:
            if d.get("serial") == serial:
                return serial
        return None
    return devices[0]["serial"]


def _adb_shell(adb: Path, serial: str, *shell_args: str, timeout: float = 45.0) -> subprocess.CompletedProcess:
    return _run_adb(adb, "-s", serial, "shell", *shell_args, timeout=timeout)


def has_bing(adb: Path, serial: str) -> bool:
    cp = _adb_shell(adb, serial, "pm", "path", BING_PKG)
    return "package:" in (cp.stdout or "")


def can_inject_input(adb: Path, serial: str) -> bool:
    cp = _adb_shell(adb, serial, "input", "tap", "1", "1", timeout=10.0)
    err = (cp.stderr or "") + (cp.stdout or "")
    return "INJECT_EVENTS" not in err and "SecurityException" not in err


def _bounds_center(bounds: str) -> Optional[tuple[int, int]]:
    m = BOUNDS_RE.match(bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1 + x2) // 2, (y1 + y2) // 2


def _dump_ui(adb: Path, serial: str) -> ET.Element:
    remote = "/sdcard/apkstart_uidump.xml"
    _adb_shell(adb, serial, "uiautomator", "dump", remote, timeout=20.0)
    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / "ui.xml"
        cp = _run_adb(adb, "-s", serial, "pull", remote, str(local), timeout=20.0)
        if cp.returncode != 0 or not local.exists():
            raise RuntimeError("无法抓取界面（uiautomator dump 失败）")
        return ET.parse(local).getroot()


def _iter_nodes(root: ET.Element):
    for n in root.iter("node"):
        yield n


def _node_blob(n: ET.Element) -> str:
    return " ".join(
        [
            n.attrib.get("text") or "",
            n.attrib.get("content-desc") or "",
            n.attrib.get("resource-id") or "",
        ]
    )


def _find_click_point(root: ET.Element, *needles: str) -> Optional[tuple[int, int, str]]:
    """Find clickable node matching any needle; prefer clickable ancestors."""
    lowered = [x.lower() for x in needles if x]
    candidates = []
    for n in _iter_nodes(root):
        blob = _node_blob(n)
        if not blob:
            continue
        blob_l = blob.lower()
        if not any(k.lower() in blob_l for k in lowered):
            continue
        click = n.attrib.get("clickable") == "true"
        center = _bounds_center(n.attrib.get("bounds") or "")
        if not center:
            continue
        # Prefer exact-ish short labels
        score = 0
        text = (n.attrib.get("text") or "").strip()
        desc = (n.attrib.get("content-desc") or "").strip()
        if click:
            score += 5
        if any(text == k or desc == k for k in needles):
            score += 10
        if any(k in text or k in desc for k in needles):
            score += 3
        candidates.append((score, len(blob), center[0], center[1], blob[:80]))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], x[1]))
    _, _, x, y, label = candidates[0]
    return x, y, label


def _tap(adb: Path, serial: str, x: int, y: int) -> None:
    cp = _adb_shell(adb, serial, "input", "tap", str(x), str(y), timeout=10.0)
    err = (cp.stderr or "") + (cp.stdout or "")
    if "INJECT_EVENTS" in err or "SecurityException" in err:
        raise RuntimeError(
            "手机禁止模拟点击。请打开：设置 → 开发者选项 →「USB 调试（安全设置）」后重试"
        )
    if cp.returncode != 0 and err.strip():
        raise RuntimeError(f"点击失败: {err.strip()[:160]}")


def _dismiss_blockers(adb: Path, serial: str) -> None:
    """关掉会盖住必应的无障碍相关向导等遮罩。"""
    for pkg in (
        "com.google.android.accessibility.switchaccess",
        "com.google.android.marvin.talkback",
    ):
        _adb_shell(adb, serial, "am", "force-stop", pkg, timeout=10.0)
    _adb_shell(adb, serial, "input", "keyevent", "KEYCODE_HOME", timeout=10.0)
    time.sleep(0.4)


def _launch_bing(adb: Path, serial: str) -> None:
    _dismiss_blockers(adb, serial)
    _adb_shell(adb, serial, "am", "force-stop", BING_PKG, timeout=15.0)
    time.sleep(0.6)
    _adb_shell(
        adb,
        serial,
        "monkey",
        "-p",
        BING_PKG,
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
        timeout=20.0,
    )
    time.sleep(4.0)
    # 若被系统向导抢走焦点，再清一次并重开
    focus = _adb_shell(adb, serial, "dumpsys", "window", timeout=15.0).stdout or ""
    if "switchaccess" in focus.lower() or BING_PKG not in focus:
        _dismiss_blockers(adb, serial)
        _adb_shell(
            adb,
            serial,
            "am",
            "start",
            "-n",
            f"{BING_PKG}/com.microsoft.sapphire.app.main.SapphireMainActivity",
            timeout=20.0,
        )
        time.sleep(3.5)


def _click_checkin_points(root: ET.Element) -> Optional[tuple[int, int, str]]:
    """在「签入」栏里点第一个尚未打勾的积分格子。"""
    # Locate 签入 section Y
    qianru_y = None
    for n in _iter_nodes(root):
        if (n.attrib.get("text") or "").strip() == "签入":
            c = _bounds_center(n.attrib.get("bounds") or "")
            if c:
                qianru_y = c[1]
            break
    if qianru_y is None:
        return None

    day_labels = []
    point_tiles = []
    checked_centers = []
    for n in _iter_nodes(root):
        text = (n.attrib.get("text") or "").strip()
        desc = (n.attrib.get("content-desc") or "").strip()
        bounds = n.attrib.get("bounds") or ""
        c = _bounds_center(bounds)
        if not c:
            continue
        x, y = c
        if abs(y - qianru_y) > 400 and not (qianru_y < y < qianru_y + 450):
            # keep nodes roughly under 签入
            if y < qianru_y - 40 or y > qianru_y + 420:
                continue
        if desc == "checked" or "check_mark" in text:
            checked_centers.append(c)
        if re.fullmatch(r"Day\s*\d+", text, re.I):
            day_labels.append((x, y, text, bounds))
        if re.fullmatch(r"\d{1,3}", text) and qianru_y + 40 < y < qianru_y + 280:
            point_tiles.append((x, y, text, bounds))

    # Prefer first point tile whose X is not near a checked mark
    point_tiles.sort(key=lambda t: t[0])
    for x, y, text, bounds in point_tiles:
        near_checked = any(abs(x - cx) < 70 and abs(y - cy) < 80 for cx, cy in checked_centers)
        if near_checked:
            continue
        return x, y, f"签入积分 {text}"

    # Fallback: click the whole 签入 row container
    for n in _iter_nodes(root):
        if n.attrib.get("clickable") != "true":
            continue
        bounds = n.attrib.get("bounds") or ""
        m = BOUNDS_RE.match(bounds)
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if y1 <= qianru_y + 20 and y2 >= qianru_y + 120 and (x2 - x1) > 600:
            return (x1 + x2) // 2, (y1 + y2) // 2, "签入整行"

    return None


async def complete_bing_app_checkin(
    *,
    adb_path: Optional[str] = None,
    serial: Optional[str] = None,
    searches: int = 0,  # kept for API compat; unused in UI flow
    log: Optional[LogFn] = None,
    should_stop: Optional[StopFn] = None,
) -> dict:
    """主页头像 → 积分页 → 点击签入栏积分。"""

    def _log(msg: str) -> None:
        if log:
            log(msg)

    def _stopped() -> bool:
        try:
            return bool(should_stop and should_stop())
        except Exception:
            return False

    adb = resolve_adb_path(adb_path)
    if not adb:
        raise RuntimeError("未找到 adb（请保留 Assets/platform-tools 或把 adb 加入 PATH）")

    device = pick_device(adb, serial)
    if not device:
        raise RuntimeError("未检测到已授权的安卓设备（adb devices 无 device）")

    if not has_bing(adb, device):
        raise RuntimeError(f"设备 {device} 未安装必应 App（{BING_PKG}）")

    if not can_inject_input(adb, device):
        # 帮用户打开开发者选项页
        _adb_shell(adb, device, "am", "start", "-a", "android.settings.APPLICATION_DEVELOPMENT_SETTINGS")
        raise RuntimeError(
            "当前手机禁止 ADB 模拟点击（常见于小米）。"
            "请在开发者选项开启「USB 调试（安全设置）」后重试"
        )

    _log(f"ADB 应用签到：设备 {device}（头像→积分→签入）")
    _launch_bing(adb, device)
    if _stopped():
        return {"ok": False, "stopped": True, "device": device}

    # 1) 点头像 / 个人资料（主页 sa_profile_button）
    root = _dump_ui(adb, device)
    avatar = (
        _find_click_point(root, "sa_profile_button", "个人资料", "sapphire_profile_image", "profile")
        or _find_click_point(root, "头像")
    )
    if not avatar:
        for n in _iter_nodes(root):
            rid = (n.attrib.get("resource-id") or "").lower()
            desc = (n.attrib.get("content-desc") or "").strip()
            if n.attrib.get("clickable") != "true":
                continue
            if "sa_profile_button" in rid or "profile" in rid or desc in ("个人资料", "头像", "帐户", "账户"):
                c = _bounds_center(n.attrib.get("bounds") or "")
                if c:
                    avatar = (c[0], c[1], desc or rid)
                    break
    if not avatar:
        raise RuntimeError("主页未找到头像入口，请确认必应已打开到主页")
    _log(f"点击头像: {avatar[2]}")
    _tap(adb, device, avatar[0], avatar[1])
    await asyncio.sleep(2.0)
    if _stopped():
        return {"ok": False, "stopped": True, "device": device}

    # 2) 点「积分」或 Microsoft Rewards
    root = _dump_ui(adb, device)
    rewards = (
        _find_click_point(root, "Microsoft Rewards")
        or _find_click_point(root, "积分")
        or _find_click_point(root, "sa_profile_my_rewards_points", "每日积分")
    )
    if not rewards:
        raise RuntimeError("个人页未找到「积分 / Microsoft Rewards」入口")
    _log(f"进入积分页: {rewards[2]}")
    _tap(adb, device, rewards[0], rewards[1])
    await asyncio.sleep(3.5)
    if _stopped():
        return {"ok": False, "stopped": True, "device": device}

    # 3) 在「签入」栏点积分
    root = _dump_ui(adb, device)
    # 可能还在加载
    if "签入" not in ET.tostring(root, encoding="unicode"):
        await asyncio.sleep(2.5)
        root = _dump_ui(adb, device)
    target = _click_checkin_points(root)
    if not target:
        # 也许已经在积分页但文案不同
        target = _find_click_point(root, "签入", "签到", "check in", "Check-in")
    if not target:
        raise RuntimeError("积分页未找到「签入」栏可点积分，请确认页面已打开")

    _log(f"点击签入: {target[2]}")
    _tap(adb, device, target[0], target[1])
    await asyncio.sleep(2.5)

    # 复核是否出现 checked
    root2 = _dump_ui(adb, device)
    checked = any(
        (n.attrib.get("content-desc") or "") == "checked" or "check_mark" in (n.attrib.get("text") or "")
        for n in _iter_nodes(root2)
    )
    _log("ADB 应用签到结束" + ("（检测到已勾选标记）" if checked else ""))
    return {
        "ok": True,
        "device": device,
        "checked": checked,
        "package": BING_PKG,
        "flow": "avatar_rewards_checkin",
    }
