"""每日活动与自动领取（适配新版 rewards.bing.com Next.js UI）。"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

from playwright.async_api import Page, BrowserContext

from main import DOMCONTENTLOADED

LogFn = Callable[[str], None]
StopFn = Callable[[], bool]


def _log(log: Optional[LogFn], msg: str) -> None:
    if log:
        log(msg)


def _stopped(should_stop: Optional[StopFn]) -> bool:
    try:
        return bool(should_stop and should_stop())
    except Exception:
        return False


async def _sleep(ms_min: int = 800, ms_max: int = 1800) -> None:
    await asyncio.sleep(random.uniform(ms_min, ms_max) / 1000.0)


async def _close_extra_pages(context: BrowserContext, keep: Page) -> Page:
    pages = [p for p in context.pages if not p.is_closed()]
    for p in pages:
        if p != keep:
            try:
                await p.close()
            except Exception:
                pass
    if keep.is_closed():
        pages = [p for p in context.pages if not p.is_closed()]
        return pages[-1] if pages else keep
    return keep


async def _latest_page(context: BrowserContext, fallback: Page) -> Page:
    pages = [p for p in context.pages if not p.is_closed()]
    return pages[-1] if pages else fallback


def _is_actionable_offer(href: str, text: str) -> bool:
    href_l = (href or "").lower()
    text_l = (text or "").lower()
    if not href_l.startswith("http"):
        return False

    # 明确跳过
    skip_host_bits = (
        "referandearn",
        "refer?",
        "/refer",
        "wallpaper",
        "autodownload",
        "xbox.com",
        "account/general",
        "orderhistory",
        "redeem/sku",
        "privacy",
        "sitemap",
        "faq",
    )
    if any(b in href_l for b in skip_host_bits):
        return False
    if any(b in text_l for b in ("邀请好友", "壁纸", "安装", "扩展", "桌面应用", "已完成")):
        return False

    # 每日/探索类
    if re.search(r"\+\s*\d+", text or ""):
        return True
    if "spotlight" in href_l or "imagepuzzle" in href_l:
        return True
    if "publ=rewardsdo" in href_l or "rewardsdo" in href_l:
        return True
    if "crea=" in href_l and "bing.com" in href_l:
        return True
    if any(k in href_l for k in ("quiz", "poll", "bnui", "rewardsoffers")):
        return True
    return False


async def _complete_activity_page(page: Page) -> str:
    """尝试完成当前活动页，返回处理方式说明。"""
    await page.wait_for_timeout(1200)
    url = (page.url or "").lower()

    # 拼图：优先跳过（仍计进度）
    try:
        skip = page.locator('button:has-text("跳过拼图"), a:has-text("跳过拼图"), button:has-text("Skip")')
        if await skip.count() > 0:
            await skip.first.click(timeout=3000)
            await page.wait_for_timeout(1500)
            return "skipped_puzzle"
    except Exception:
        pass

    # 投票/测验：点可选选项
    for sel in (
        'input[type="radio"]',
        '[role="radio"]',
        "label:has(input[type=radio])",
        "#rqAnswerOption0",
        ".rqOption",
        "button:has-text('提交')",
        "input[type=submit]",
        "#rqStartQuiz",
        "#rqNextQuestion",
        "#nextQuestionbtn",
    ):
        try:
            loc = page.locator(sel)
            n = await loc.count()
            if n <= 0:
                continue
            idx = 0 if n == 1 else random.randint(0, min(n, 3) - 1)
            await loc.nth(idx).click(timeout=2000)
            await page.wait_for_timeout(800)
        except Exception:
            continue

    # 搜索框类活动
    try:
        box = page.locator("#sb_form_q, input[name='q'], textarea[name='q']")
        if await box.count() > 0 and "bing.com" in url:
            q = random.choice(["天气", "新闻", "微软积分", "今日热点", "地图"])
            await box.first.fill("")
            await box.first.type(q, delay=60)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(2000)
            return "search_submitted"
    except Exception:
        pass

    # 通用：停留一会儿
    await page.wait_for_timeout(random.randint(2500, 5000))
    return "dwelt"


async def collect_earn_offers(page: Page) -> list[dict]:
    offers = await page.evaluate(
        """() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
          href: a.href || '',
          text: (a.innerText || a.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 120)
        }))"""
    )
    out = []
    seen = set()
    for o in offers or []:
        href = (o.get("href") or "").strip()
        text = (o.get("text") or "").strip()
        if not _is_actionable_offer(href, text):
            continue
        key = href.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append({"href": href, "text": text or urlparse(href).path})
    return out


async def claim_available_rewards(
    page: Page,
    *,
    log: Optional[LogFn] = None,
    should_stop: Optional[StopFn] = None,
) -> int:
    """尝试领取仪表盘上的可领取积分。返回点击领取的次数。"""
    if _stopped(should_stop):
        return 0

    _log(log, "开始自动领取...")
    clicks = 0
    try:
        await page.goto("https://rewards.bing.com/dashboard", wait_until=DOMCONTENTLOADED, timeout=60000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        _log(log, f"打开仪表盘失败: {e}")
        return 0

    # 点击“可领取”卡片，并处理可能弹出的遮罩层
    try:
        claim_card = page.locator('button:has-text("可领取")')
        if await claim_card.count() > 0:
            await claim_card.first.click(timeout=5000)
            clicks += 1
            await page.wait_for_timeout(1200)
    except Exception as e:
        _log(log, f"点击可领取失败: {e}")

    # 在弹层/页面中寻找可点的“领取”
    try:
        candidates = page.locator(
            'button:has-text("领取"), a:has-text("领取"), [role="button"]:has-text("领取"), p:has-text("领取")'
        )
        n = await candidates.count()
        for i in range(min(n, 20)):
            if _stopped(should_stop):
                break
            el = candidates.nth(i)
            try:
                text = ((await el.inner_text()) or "").strip().replace("\n", " ")
            except Exception:
                continue
            if not re.search(r"领取", text):
                continue
            # 跳过仅展示“可领取”大卡片本身（避免循环点开）
            if text.startswith("可领取") and "领取" in text and len(text) > 6:
                # 仍允许点它内部，但优先短文本
                if not re.search(r"^领取\s*>?$", text) and "领取积分" not in text:
                    continue
            try:
                await el.click(timeout=2500, force=True)
                clicks += 1
                await _sleep(900, 1800)
            except Exception:
                try:
                    await el.evaluate("node => node.click()")
                    clicks += 1
                    await _sleep(900, 1800)
                except Exception:
                    continue
    except Exception as e:
        _log(log, f"查找领取按钮失败: {e}")

    # 关掉可能残留的遮罩
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)
    except Exception:
        pass

    _log(log, f"自动领取结束（触发 {clicks} 次点击）")
    return clicks


async def complete_daily_activities(
    page: Page,
    context: BrowserContext,
    *,
    log: Optional[LogFn] = None,
    should_stop: Optional[StopFn] = None,
    max_offers: int = 8,
) -> int:
    """完成赚取页上的每日/探索类活动。返回尝试完成的活动数。"""
    if _stopped(should_stop):
        return 0

    _log(log, "开始每日活动...")
    try:
        await page.goto("https://rewards.bing.com/earn", wait_until=DOMCONTENTLOADED, timeout=60000)
        await page.wait_for_timeout(2500)
    except Exception as e:
        _log(log, f"打开赚取页失败: {e}")
        return 0

    offers = await collect_earn_offers(page)
    _log(log, f"发现可尝试活动 {len(offers)} 个")
    done = 0

    for offer in offers[:max_offers]:
        if _stopped(should_stop):
            break
        href = offer["href"]
        title = offer["text"][:60]
        _log(log, f"处理活动: {title}")

        page = await _close_extra_pages(context, page)
        before = list(context.pages)

        try:
            async with context.expect_page(timeout=4000) as new_page_info:
                # 优先点页面上的同链接，保留站点自身点击逻辑
                loc = page.locator(f'a[href="{href}"]')
                if await loc.count() == 0:
                    loc = page.locator(f'a[href*="{urlparse(href).path}"]')
                if await loc.count() > 0:
                    await loc.first.click(timeout=4000)
                else:
                    await page.evaluate("(u) => window.open(u, '_blank')", href)
            activity_page = await new_page_info.value
        except Exception:
            # 同页跳转
            try:
                await page.goto(href, wait_until=DOMCONTENTLOADED, timeout=45000)
                activity_page = page
            except Exception as e:
                _log(log, f"打开活动失败: {e}")
                try:
                    await page.goto("https://rewards.bing.com/earn", wait_until=DOMCONTENTLOADED, timeout=60000)
                except Exception:
                    pass
                continue

        try:
            await activity_page.wait_for_load_state(DOMCONTENTLOADED, timeout=30000)
        except Exception:
            pass

        try:
            how = await _complete_activity_page(activity_page)
            _log(log, f"活动处理完成 ({how}): {title}")
            done += 1
        except Exception as e:
            _log(log, f"活动处理异常: {e}")

        # 关闭新开页，回到赚取页
        try:
            if activity_page != page and not activity_page.is_closed():
                await activity_page.close()
        except Exception:
            pass

        page = await _latest_page(context, page)
        try:
            if "rewards.bing.com/earn" not in (page.url or ""):
                await page.goto("https://rewards.bing.com/earn", wait_until=DOMCONTENTLOADED, timeout=60000)
            await page.wait_for_timeout(1200)
        except Exception:
            pass

        await _sleep(1000, 2200)

        # 防止标签无限增长
        if len(context.pages) > len(before) + 1:
            page = await _close_extra_pages(context, page)

    _log(log, f"每日活动结束，尝试完成 {done} 个")
    return done
