"""真机 ADB：必应 App 切换已登录账号 +「应用」签到。

切换：头像 → 设置账户 →（如需）注销 → 登录 → 帐户页点目标邮箱 → 登录
签到：头像 → Microsoft Rewards → 「签入」栏点当日积分

小米需开启：开发者选项 → USB 调试（安全设置）
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
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


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
    last_err = ""
    for _ in range(3):
        cp = _adb_shell(adb, serial, "uiautomator", "dump", remote, timeout=20.0)
        last_err = ((cp.stderr or "") + (cp.stdout or "")).strip()
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "ui.xml"
            pull = _run_adb(adb, "-s", serial, "pull", remote, str(local), timeout=20.0)
            if pull.returncode == 0 and local.exists() and local.stat().st_size > 100:
                try:
                    return ET.parse(local).getroot()
                except Exception as e:
                    last_err = str(e)
        time.sleep(1.0)
    raise RuntimeError(f"无法抓取界面（uiautomator dump 失败）: {last_err[:120]}")


def _iter_nodes(root: ET.Element):
    for n in root.iter("node"):
        yield n


def _ui_text(root: ET.Element) -> str:
    return ET.tostring(root, encoding="unicode")


def _normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def _extract_emails(root: ET.Element) -> list[str]:
    found = []
    for n in _iter_nodes(root):
        for key in ("text", "content-desc"):
            val = (n.attrib.get(key) or "").strip()
            for m in EMAIL_RE.findall(val):
                e = _normalize_email(m)
                if e not in found:
                    found.append(e)
    return found


def _current_profile_email(root: ET.Element) -> Optional[str]:
    for n in _iter_nodes(root):
        rid = n.attrib.get("resource-id") or ""
        if "sapphire_profile_account_hint" in rid or "profile_display_email" in rid:
            t = (n.attrib.get("text") or "").strip()
            if EMAIL_RE.fullmatch(t):
                return _normalize_email(t)
    emails = _extract_emails(root)
    return emails[0] if emails else None


def _find_click_point(root: ET.Element, *needles: str) -> Optional[tuple[int, int, str]]:
    lowered = [x.lower() for x in needles if x]
    candidates = []
    for n in _iter_nodes(root):
        text = (n.attrib.get("text") or "").strip()
        desc = (n.attrib.get("content-desc") or "").strip()
        rid = n.attrib.get("resource-id") or ""
        blob = f"{text} {desc} {rid}"
        if not blob.strip():
            continue
        blob_l = blob.lower()
        if not any(k in blob_l for k in lowered):
            continue
        center = _bounds_center(n.attrib.get("bounds") or "")
        if not center:
            continue
        score = 0
        if n.attrib.get("clickable") == "true":
            score += 5
        if any(text == k or desc == k for k in needles):
            score += 10
        if any(k.lower() in rid.lower() for k in lowered):
            score += 4
        candidates.append((score, len(blob), center[0], center[1], blob[:100]))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], x[1]))
    _, _, x, y, label = candidates[0]
    return x, y, label


def _tap(adb: Path, serial: str, x: int, y: int) -> None:
    # 避免小米「传送门/文字识别」抢点击
    for pkg in ("com.miui.contentextension", "com.miui.taplus"):
        _adb_shell(adb, serial, "am", "force-stop", pkg, timeout=8.0)
    cp = _adb_shell(adb, serial, "input", "tap", str(x), str(y), timeout=10.0)
    err = (cp.stderr or "") + (cp.stdout or "")
    if "INJECT_EVENTS" in err or "SecurityException" in err:
        raise RuntimeError(
            "手机禁止模拟点击。请打开：设置 → 开发者选项 →「USB 调试（安全设置）」后重试"
        )
    if cp.returncode != 0 and err.strip():
        raise RuntimeError(f"点击失败: {err.strip()[:160]}")


def _is_screen_on(adb: Path, serial: str) -> bool:
    out = (_adb_shell(adb, serial, "dumpsys", "power", timeout=12.0).stdout or "")
    if "mWakefulness=Awake" in out:
        return True
    if "Display Power: state=ON" in out:
        return True
    if "mScreenOn=true" in out:
        return True
    # dumpsys display fallback
    disp = (_adb_shell(adb, serial, "dumpsys", "display", timeout=12.0).stdout or "")
    return "mScreenState=ON" in disp or "mState=ON" in disp


def _wake_screen(adb: Path, serial: str) -> None:
    """点亮屏幕；无安全锁时可尝试滑掉锁屏。有密码/指纹仍需手动解锁。"""
    if not _is_screen_on(adb, serial):
        _adb_shell(adb, serial, "input", "keyevent", "KEYCODE_WAKEUP", timeout=8.0)
        time.sleep(0.6)
    # 再发一次，部分机型息屏后第一次无效
    if not _is_screen_on(adb, serial):
        _adb_shell(adb, serial, "input", "keyevent", "KEYCODE_POWER", timeout=8.0)
        time.sleep(0.8)
    _adb_shell(adb, serial, "wm", "dismiss-keyguard", timeout=8.0)
    # 上滑一次，覆盖「滑动解锁 / 点亮后锁屏页」
    size = (_adb_shell(adb, serial, "wm", "size", timeout=8.0).stdout or "").strip()
    m = re.search(r"(\d+)\s*x\s*(\d+)", size)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        x = w // 2
        _adb_shell(
            adb,
            serial,
            "input",
            "swipe",
            str(x),
            str(int(h * 0.82)),
            str(x),
            str(int(h * 0.28)),
            "300",
            timeout=10.0,
        )
        time.sleep(0.4)


def _dismiss_blockers(adb: Path, serial: str) -> None:
    _wake_screen(adb, serial)
    for pkg in (
        "com.google.android.accessibility.switchaccess",
        "com.miui.contentextension",
        "com.miui.taplus",
    ):
        _adb_shell(adb, serial, "am", "force-stop", pkg, timeout=8.0)
    _adb_shell(adb, serial, "input", "keyevent", "KEYCODE_HOME", timeout=10.0)
    time.sleep(0.3)


def _launch_bing(adb: Path, serial: str, *, force_stop: bool = True) -> None:
    _dismiss_blockers(adb, serial)
    if force_stop:
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


def _back_to_home(adb: Path, serial: str) -> None:
    """登录后回到主页，避免 force-stop 丢会话。"""
    for _ in range(4):
        root = _dump_ui(adb, serial)
        if _find_click_point(root, "sa_profile_button", "个人资料") and not _is_account_picker(root):
            return
        back = _find_click_point(root, "sa_template_header_action_back", "返回", "Navigate up")
        if back:
            _tap(adb, serial, back[0], back[1])
        else:
            _adb_shell(adb, serial, "input", "keyevent", "KEYCODE_BACK", timeout=8.0)
        time.sleep(1.0)
    _launch_bing(adb, serial, force_stop=False)


def _is_account_picker(root: ET.Element) -> bool:
    text = _ui_text(root)
    return ("若要登录，请选择一个帐户" in text) or ("sapphire_amp_list_title" in text) or (
        "sapphire_amp_action_button" in text and "profile_display_email" in text
    )


def _is_profile_page(root: ET.Element) -> bool:
    text = _ui_text(root)
    return (
        "sa_profile_rewards_root" in text
        or "sa_profile_my_rewards_points" in text
        or ("Microsoft Rewards" in text and "sapphire_profile_account_hint" in text)
        or ("每日积分" in text and "sapphire_profile_display_name" in text)
    )


def _is_settings_page(root: ET.Element) -> bool:
    text = _ui_text(root)
    return "sapphire_settings_root" in text or (
        "sapphire_settings_hint" in text and ("注销" in text or "账户" in text)
    )


def _open_profile(adb: Path, serial: str) -> ET.Element:
    root = _dump_ui(adb, serial)
    if _is_account_picker(root):
        return root
    if _is_profile_page(root):
        return root

    # 设置页等：先返回主页再点头像
    if _is_settings_page(root):
        back = (
            _find_click_point(root, "sa_template_header_action_back", "返回")
            or _find_click_point(root, "sa_profile_header_action_back")
        )
        if back:
            _tap(adb, serial, back[0], back[1])
            time.sleep(1.2)
            root = _dump_ui(adb, serial)
            if _is_profile_page(root):
                return root

    # 主页头像：优先专用按钮，不要用个人页里的 sapphire_profile_image（会进设置）
    avatar = None
    for n in _iter_nodes(root):
        rid = n.attrib.get("resource-id") or ""
        desc = (n.attrib.get("content-desc") or "").strip()
        if "sa_profile_button" in rid or (desc == "个人资料" and "sapphire_profile_image" not in rid):
            c = _bounds_center(n.attrib.get("bounds") or "")
            if c:
                avatar = (c[0], c[1], desc or rid)
                break
    if not avatar:
        avatar = _find_click_point(root, "sa_profile_button")
    if not avatar:
        back = _find_click_point(root, "sa_template_header_action_back", "返回")
        if back:
            _tap(adb, serial, back[0], back[1])
            time.sleep(1.2)
            root = _dump_ui(adb, serial)
            if _is_profile_page(root):
                return root
            avatar = _find_click_point(root, "sa_profile_button")
    if not avatar:
        raise RuntimeError("主页未找到头像入口")
    _tap(adb, serial, avatar[0], avatar[1])
    time.sleep(2.0)
    root = _dump_ui(adb, serial)
    if _is_settings_page(root):
        # 误进设置：返回到个人页
        back = _find_click_point(root, "sa_template_header_action_back", "返回")
        if back:
            _tap(adb, serial, back[0], back[1])
            time.sleep(1.2)
            root = _dump_ui(adb, serial)
    return root


def _open_account_settings(adb: Path, serial: str, root: ET.Element) -> ET.Element:
    """从个人页进入设置账户区。"""
    target = (
        _find_click_point(root, "sapphire_profile_account_hint")
        or _find_click_point(root, "sapphire_profile_display_name")
        or _find_click_point(root, "sapphire_profile_image", "个人资料")
        or _find_click_point(root, "设置")
    )
    if not target:
        raise RuntimeError("个人页未找到账户/设置入口")
    _tap(adb, serial, target[0], target[1])
    time.sleep(2.2)
    return _dump_ui(adb, serial)


def _open_account_manager(adb: Path, serial: str, root: ET.Element) -> ET.Element:
    """进入「若要登录，请选择一个帐户」页面。"""
    text = _ui_text(root)
    if "若要登录，请选择一个帐户" in text or "sapphire_amp_list_title" in text:
        return root

    # 已登录：先注销
    if "注销" in text:
        pt = _find_click_point(root, "注销")
        if pt:
            _tap(adb, serial, pt[0], pt[1])
            time.sleep(2.2)
            root = _dump_ui(adb, serial)
            text = _ui_text(root)

    # 点「登录」进入帐户选择页（点左侧账户行，避免误触小米文字识别）
    if "登录" in text or "Microsoft 帐户" in text or "Microsoft 账户" in text:
        # prefer exact hint button area but slightly left of far-right edge
        for n in _iter_nodes(root):
            t = (n.attrib.get("text") or "").strip()
            rid = n.attrib.get("resource-id") or ""
            if t == "登录" and "sapphire_settings_hint" in rid:
                c = _bounds_center(n.attrib.get("bounds") or "")
                if c:
                    _tap(adb, serial, c[0], c[1])
                    break
        else:
            pt = _find_click_point(root, "登录", "Microsoft 帐户", "Microsoft 账户")
            if not pt:
                raise RuntimeError("未找到登录入口")
            _tap(adb, serial, pt[0], pt[1])
        time.sleep(3.5)
        root = _dump_ui(adb, serial)

    text = _ui_text(root)
    if "若要登录，请选择一个帐户" not in text and "AccountManagePage" not in (
        _adb_shell(adb, serial, "dumpsys", "window", timeout=12.0).stdout or ""
    ):
        # 再等一会
        time.sleep(2.0)
        root = _dump_ui(adb, serial)
    return root


def _select_account_and_login(adb: Path, serial: str, email: str, root: ET.Element) -> None:
    email_n = _normalize_email(email)
    # 点目标邮箱
    hit = None
    for n in _iter_nodes(root):
        t = (n.attrib.get("text") or "").strip()
        d = (n.attrib.get("content-desc") or "").strip()
        rid = n.attrib.get("resource-id") or ""
        if _normalize_email(t) == email_n or _normalize_email(d) == email_n:
            c = _bounds_center(n.attrib.get("bounds") or "")
            if c:
                hit = (c[0], c[1], t or d)
                # prefer email id
                if "profile_display_email" in rid:
                    break
    if not hit:
        raise RuntimeError(f"帐户页未找到已保存账号: {email}（请先在必应 App 内手动登录过该账号）")
    _tap(adb, serial, hit[0], hit[1])
    time.sleep(1.0)

    root = _dump_ui(adb, serial)
    login_btn = _find_click_point(root, "sapphire_amp_action_button") or _find_click_point(root, "登录")
    # Prefer bottom 登录 button by resource id
    for n in _iter_nodes(root):
        rid = n.attrib.get("resource-id") or ""
        t = (n.attrib.get("text") or "").strip()
        if "sapphire_amp_action_button" in rid and t == "登录":
            c = _bounds_center(n.attrib.get("bounds") or "")
            if c:
                login_btn = (c[0], c[1], "登录按钮")
                break
    if not login_btn:
        raise RuntimeError("帐户页未找到「登录」按钮")
    _tap(adb, serial, login_btn[0], login_btn[1])
    time.sleep(4.0)


async def switch_bing_account(
    adb: Path,
    serial: str,
    email: str,
    *,
    log: Optional[LogFn] = None,
) -> str:
    """切换到已保存的微软账号，返回最终邮箱。"""

    def _log(msg: str) -> None:
        if log:
            log(msg)

    email_n = _normalize_email(email)
    if not email_n:
        raise RuntimeError("未提供目标账号邮箱")

    root = _dump_ui(adb, serial)
    # App 有时直接停在帐户选择页（注销后/上次未选完）
    if not _is_account_picker(root):
        root = _open_profile(adb, serial)

    if _is_account_picker(root):
        _log(f"ADB 当前在帐户页，选择 {email_n}")
        _select_account_and_login(adb, serial, email_n, root)
        _back_to_home(adb, serial)
        root = _open_profile(adb, serial)
        if _is_account_picker(root):
            raise RuntimeError("登录后仍停在帐户选择页")
        cur2 = _current_profile_email(root)
        if cur2 != email_n:
            raise RuntimeError(f"切换后账号不匹配：期望 {email_n}，实际 {cur2 or '未知'}")
        _log(f"ADB 已切换到: {cur2}")
        _adb_shell(adb, serial, "input", "keyevent", "KEYCODE_BACK", timeout=8.0)
        time.sleep(0.8)
        return cur2

    cur = _current_profile_email(root)
    if cur == email_n:
        _log(f"ADB 已是目标账号: {email_n}")
        _adb_shell(adb, serial, "input", "keyevent", "KEYCODE_BACK", timeout=8.0)
        time.sleep(0.8)
        return email_n

    _log(f"ADB 切换账号: {cur or '?'} → {email_n}")
    root = _open_account_settings(adb, serial, root)
    root = _open_account_manager(adb, serial, root)
    if not _is_account_picker(root) and "若要登录" not in _ui_text(root):
        time.sleep(2.0)
        root = _dump_ui(adb, serial)
    _select_account_and_login(adb, serial, email_n, root)
    _back_to_home(adb, serial)
    root = _open_profile(adb, serial)
    if _is_account_picker(root):
        raise RuntimeError("登录后仍停在帐户选择页")
    cur2 = _current_profile_email(root)
    if cur2 != email_n:
        raise RuntimeError(f"切换后账号不匹配：期望 {email_n}，实际 {cur2 or '未知'}")
    _log(f"ADB 已切换到: {cur2}")
    _adb_shell(adb, serial, "input", "keyevent", "KEYCODE_BACK", timeout=8.0)
    time.sleep(0.8)
    return cur2


def _click_checkin_points(root: ET.Element) -> Optional[tuple[int, int, str]]:
    qianru_y = None
    for n in _iter_nodes(root):
        if (n.attrib.get("text") or "").strip() == "签入":
            c = _bounds_center(n.attrib.get("bounds") or "")
            if c:
                qianru_y = c[1]
            break
    if qianru_y is None:
        return None

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
        if y < qianru_y - 40 or y > qianru_y + 420:
            continue
        if desc == "checked" or "check_mark" in text:
            checked_centers.append(c)
        if re.fullmatch(r"\d{1,3}", text) and qianru_y + 40 < y < qianru_y + 280:
            point_tiles.append((x, y, text, bounds))

    point_tiles.sort(key=lambda t: t[0])
    for x, y, text, _bounds in point_tiles:
        near_checked = any(abs(x - cx) < 70 and abs(y - cy) < 80 for cx, cy in checked_centers)
        if near_checked:
            continue
        return x, y, f"签入积分 {text}"

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


async def _do_checkin(adb: Path, serial: str, *, log: Optional[LogFn] = None) -> bool:
    def _log(msg: str) -> None:
        if log:
            log(msg)

    root = _open_profile(adb, serial)
    if _is_account_picker(root) or _is_settings_page(root):
        raise RuntimeError("签到前未进入个人页（仍在帐户/设置页）")
    rewards = (
        _find_click_point(root, "Microsoft Rewards")
        or _find_click_point(root, "sa_profile_my_rewards_points", "每日积分")
        or _find_click_point(root, "sa_profile_rewards_root")
        or _find_click_point(root, "总积分", "积分")
    )
    if not rewards:
        raise RuntimeError("个人页未找到「积分 / Microsoft Rewards」入口")
    _log(f"进入积分页: {rewards[2]}")
    _tap(adb, serial, rewards[0], rewards[1])
    await asyncio.sleep(3.5)

    root = _dump_ui(adb, serial)
    if "签入" not in _ui_text(root):
        await asyncio.sleep(2.5)
        root = _dump_ui(adb, serial)
    target = _click_checkin_points(root) or _find_click_point(root, "签入", "签到")
    if not target:
        raise RuntimeError("积分页未找到「签入」栏可点积分")
    _log(f"点击签入: {target[2]}")
    _tap(adb, serial, target[0], target[1])
    await asyncio.sleep(2.5)
    root2 = _dump_ui(adb, serial)
    return any(
        (n.attrib.get("content-desc") or "") == "checked" or "check_mark" in (n.attrib.get("text") or "")
        for n in _iter_nodes(root2)
    )


async def complete_bing_app_checkin(
    *,
    adb_path: Optional[str] = None,
    serial: Optional[str] = None,
    account_email: Optional[str] = None,
    searches: int = 0,  # compat
    log: Optional[LogFn] = None,
    should_stop: Optional[StopFn] = None,
) -> dict:
    """可选切换到 account_email（需已在 App 登录过），再执行签入。"""

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
        _adb_shell(adb, device, "am", "start", "-a", "android.settings.APPLICATION_DEVELOPMENT_SETTINGS")
        raise RuntimeError(
            "当前手机禁止 ADB 模拟点击（常见于小米）。"
            "请在开发者选项开启「USB 调试（安全设置）」后重试"
        )

    email = _normalize_email(account_email or "")
    _log(f"ADB 应用签到：设备 {device}" + (f"，账号 {email}" if email else ""))
    _wake_screen(adb, device)
    if not _is_screen_on(adb, device):
        raise RuntimeError("无法点亮屏幕，请手动亮屏并解锁后再试")
    _log("ADB 已点亮屏幕")
    _launch_bing(adb, device)
    if _stopped():
        return {"ok": False, "stopped": True, "device": device}

    active = None
    if email:
        active = await switch_bing_account(adb, device, email, log=log)
        # 不 force-stop，保留刚登录会话
        _back_to_home(adb, device)
        if _stopped():
            return {"ok": False, "stopped": True, "device": device, "account": active}

    checked = await _do_checkin(adb, device, log=log)
    _log("ADB 应用签到结束" + ("（检测到已勾选标记）" if checked else ""))
    return {
        "ok": True,
        "device": device,
        "checked": checked,
        "account": active or email or None,
        "package": BING_PKG,
        "flow": "switch_account_checkin" if email else "avatar_rewards_checkin",
    }
