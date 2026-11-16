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


def _is_puzzle_offer(href: str, text: str = "") -> bool:
    blob = f"{href or ''} {text or ''}".lower()
    return any(
        k in blob
        for k in (
            "imagepuzzle",
            "spotlight",
            "puzzle",
            "jigsaw",
            "拼图",
            "完成此拼图",
            "排列图块",
        )
    )


def _is_skip_href(href: str) -> bool:
    href_l = (href or "").lower()
    if not href_l.startswith("http"):
        return True
    skip_bits = (
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
        "support.microsoft.com",
        "login.live.com",
        "account.microsoft.com",
        "rewards.bing.com/dashboard",
        "rewards.bing.com/redeem",
        "rewards.bing.com/about",
        "rewards.bing.com/earn",
    )
    return any(b in href_l for b in skip_bits)


async def _complete_activity_page(page: Page, *, offer_hint: str = "", open_only: bool = False) -> str:
    """尝试完成当前活动页，返回处理方式说明。

    日常任务里的拼图/打开类：只需打开页面，无需真正拼图。
    """
    await page.wait_for_timeout(900)
    url = (page.url or "").lower()
    hint = offer_hint or ""

    # 拼图或明确“仅打开”：停留即可
    if open_only or _is_puzzle_offer(url, hint):
        await page.wait_for_timeout(random.randint(1800, 3200))
        return "opened_puzzle" if _is_puzzle_offer(url, hint) else "opened_only"

    try:
        body_snip = ((await page.locator("body").inner_text(timeout=2000)) or "")[:800]
    except Exception:
        body_snip = ""
    if _is_puzzle_offer(url, body_snip) or ("拼图" in body_snip and "排列图块" in body_snip):
        await page.wait_for_timeout(random.randint(1800, 3200))
        return "opened_puzzle"

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

    await page.wait_for_timeout(random.randint(2200, 4000))
    return "dwelt"


async def collect_section_point_offers(
    page: Page,
    *,
    section_titles: list[str],
    next_section_pattern: str,
) -> list[dict]:
    """收集指定分区标题下带 +积分 的任务卡片。"""
    offers = await page.evaluate(
        """({ titles, nextSectionPattern }) => {
          const norm = (s) => (s || '').trim().replace(/\\s+/g, ' ');
          const yOf = (el) => {
            try {
              const r = el.getBoundingClientRect();
              return r.top + window.scrollY;
            } catch (e) {
              return 0;
            }
          };
          const titleSet = (titles || []).map((t) => norm(t)).filter(Boolean);
          if (!titleSet.length) return [];

          let heading = null;
          let matchedTitle = '';
          const candidates = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,span,p'));
          for (const el of candidates) {
            const own = norm(el.innerText || '');
            if (!own || own.length > 48) continue;
            for (const t of titleSet) {
              if (own === t || (own.startsWith(t) && own.length <= t.length + 12)) {
                heading = el;
                matchedTitle = t;
                break;
              }
            }
            if (heading) break;
          }
          // 再放宽：标题被包在短容器里
          if (!heading) {
            for (const el of candidates) {
              const own = norm(el.innerText || '');
              if (!own || own.length > 64) continue;
              for (const t of titleSet) {
                if (own.includes(t)) {
                  heading = el;
                  matchedTitle = t;
                  break;
                }
              }
              if (heading) break;
            }
          }
          if (!heading) return [];

          const headingY = yOf(heading);
          let nextSectionRe;
          try {
            nextSectionRe = new RegExp(nextSectionPattern, 'i');
          } catch (e) {
            nextSectionRe = /^(更多活动|探索|游戏|必应搜索|邀请)/i;
          }
          let nextY = Infinity;
          for (const el of document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,span,p')) {
            const own = norm(el.innerText || '');
            if (!own || own.length > 28) continue;
            if (titleSet.some((t) => own === t || own.startsWith(t))) continue;
            if (!nextSectionRe.test(own)) continue;
            const y = yOf(el);
            if (y > headingY + 20 && y < nextY) nextY = y;
          }

          const items = [];
          const seen = new Set();
          const clickables = Array.from(
            document.querySelectorAll('a[href], button, [role="link"], [role="button"]')
          );

          for (const el of clickables) {
            const y = yOf(el);
            if (y < headingY - 4) continue;
            if (y >= nextY) continue;

            let href = '';
            if (el.tagName === 'A') href = el.href || '';
            if (!href) {
              const innerA = el.querySelector && el.querySelector('a[href]');
              if (innerA) href = innerA.href || '';
            }
            if (!href) {
              href = (el.getAttribute && (el.getAttribute('href') || el.getAttribute('data-href'))) || '';
            }

            let scope = el;
            for (let i = 0; i < 5; i++) {
              const p = scope.parentElement;
              if (!p) break;
              const pt = norm(p.innerText || '');
              if (pt.length > 0 && pt.length <= 220 && !titleSet.some((t) => pt.startsWith(t))) scope = p;
              else break;
            }
            const nearby = norm(scope.innerText || '').slice(0, 220);
            const hasPts = /\\+\\s*\\d+/.test(nearby) || /(\\d+)\\s*积分/.test(nearby);
            if (!hasPts) continue;
            if (/(已完成|completed|已领取)/i.test(nearby) && !hasPts) continue;

            const text = norm(el.innerText || el.getAttribute('aria-label') || nearby).slice(0, 120);
            const key = (href || text).split('#')[0].toLowerCase();
            if (!key || seen.has(key)) continue;
            seen.add(key);

            items.push({
              href: href || '',
              text,
              nearby,
              section: matchedTitle,
              titleGuess: (nearby.match(/完成此拼图|[^\\n]{2,40}/) || [text])[0].slice(0, 80),
            });
          }
          return items;
        }""",
        {
            "titles": section_titles,
            "nextSectionPattern": next_section_pattern,
        },
    )

    out = []
    seen = set()
    for o in offers or []:
        href = (o.get("href") or "").strip()
        text = (o.get("text") or "").strip()
        nearby = (o.get("nearby") or text).strip()
        if not re.search(r"\+\s*\d+", nearby or text) and not re.search(r"(\d+)\s*积分", nearby or text):
            continue
        if href and _is_skip_href(href):
            # 首页/赚取页自身链接跳过，但保留无 href 的文案卡片
            continue
        key = (href.split("#")[0].lower() if href else "") or f"text:{(nearby or text)[:80].lower()}"
        if key in seen:
            continue
        seen.add(key)
        label = (o.get("titleGuess") or text or nearby)[:120]
        out.append(
            {
                "href": href,
                "text": label,
                "nearby": nearby[:220],
                "section": o.get("section") or "",
                "is_puzzle": _is_puzzle_offer(href, f"{text} {nearby}"),
                "click_text": "完成此拼图"
                if "完成此拼图" in (nearby or text)
                else (label.split(" ")[0][:40] if label else ""),
            }
        )

    def _rank(item: dict) -> tuple:
        near = item.get("nearby") or ""
        pts = 0
        m = re.search(r"\+\s*(\d+)", near)
        if m:
            pts = int(m.group(1))
        return (0 if item.get("is_puzzle") else 1, -pts, item.get("text") or "")

    out.sort(key=_rank)
    return out


async def collect_homepage_daily_set(page: Page) -> list[dict]:
    """首页「每日活动」分区中带 +积分 的卡片（只扫该分区，不整页检索）。"""
    return await collect_section_point_offers(
        page,
        section_titles=["每日活动", "Daily activities", "Daily Activities"],
        next_section_pattern=(
            r"^(每日任务|日常任务|更多活动|探索|游戏|必应搜索|邀请|抽奖|购物|连续打卡|"
            r"Daily set|More activities|Earn|Redeem|Punch cards)"
        ),
    )


async def collect_earn_offers(page: Page) -> list[dict]:
    """积分赚取页「日常任务」分区里带 +积分 的卡片。"""
    return await collect_section_point_offers(
        page,
        section_titles=["日常任务"],
        next_section_pattern=(
            r"^(每日任务|每日活动|更多活动|探索|游戏|必应搜索|邀请|抽奖|购物|"
            r"Redeem|More activities|Daily activities|Daily set)"
        ),
    )


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


def _offer_key(offer: dict) -> str:
    href = (offer.get("href") or "").strip()
    if href:
        return href.split("#")[0].lower()
    near = (offer.get("nearby") or offer.get("text") or "").strip().lower()
    return f"text:{near[:80]}"


async def _open_offer_card(page: Page, context: BrowserContext, offer: dict) -> Page:
    """点击任务卡片；优先按文案点（如完成此拼图），否则按 href。"""
    href = (offer.get("href") or "").strip()
    click_text = (offer.get("click_text") or "").strip()
    nearby = (offer.get("nearby") or offer.get("text") or "").strip()
    origin_url = page.url or ""

    async def _click_target() -> None:
        for needle in (click_text, "完成此拼图"):
            if not needle:
                continue
            loc = page.locator(
                f'a:has-text("{needle}"), button:has-text("{needle}"), [role="link"]:has-text("{needle}")'
            )
            if await loc.count() > 0:
                await loc.first.click(timeout=4000)
                return
            loc2 = page.get_by_text(needle, exact=False)
            if await loc2.count() > 0:
                await loc2.first.click(timeout=4000)
                return

        if href:
            loc = page.locator(f'a[href="{href}"]')
            if await loc.count() == 0:
                path = urlparse(href).path or ""
                if path and path != "/":
                    loc = page.locator(f'a[href*="{path}"]')
            if await loc.count() > 0:
                await loc.first.click(timeout=4000)
                return
            await page.evaluate("(u) => window.open(u, '_blank')", href)
            return

        first_line = (nearby.split("+")[0] or nearby)[:30].strip()
        if first_line:
            loc = page.get_by_text(first_line, exact=False)
            if await loc.count() > 0:
                await loc.first.click(timeout=4000)
                return
        raise RuntimeError("找不到可点击的任务卡片")

    try:
        async with context.expect_page(timeout=5000) as new_page_info:
            await _click_target()
        return await new_page_info.value
    except Exception:
        if href:
            try:
                # 仍停在原任务列表页时，直接打开外链
                cur = page.url or ""
                if cur == origin_url or "rewards.bing.com" in cur:
                    await page.goto(href, wait_until=DOMCONTENTLOADED, timeout=45000)
            except Exception:
                pass
        return page


async def _run_point_offers(
    page: Page,
    context: BrowserContext,
    *,
    list_url: str,
    collect_fn: Callable[[Page], Awaitable[list]],
    label: str,
    log: Optional[LogFn] = None,
    should_stop: Optional[StopFn] = None,
    max_offers: int = 16,
) -> tuple[Page, int]:
    """在指定列表页收集并完成带积分任务。返回 (page, 完成数)。"""
    if _stopped(should_stop):
        return page, 0

    _log(log, f"开始{label}...")
    try:
        await page.goto(list_url, wait_until=DOMCONTENTLOADED, timeout=60000)
        await page.wait_for_timeout(2500)
    except Exception as e:
        _log(log, f"打开{label}页面失败: {e}")
        return page, 0

    done = 0
    attempted: set[str] = set()

    for round_idx in range(1, 3):
        if _stopped(should_stop) or done >= max_offers:
            break

        offers = await collect_fn(page)
        pending = [o for o in offers if _offer_key(o) not in attempted]
        _log(log, f"{label} 第 {round_idx} 轮发现 {len(pending)} 个（候选 {len(offers)}）")
        if not pending:
            if round_idx == 1:
                try:
                    tip = await page.evaluate(
                        """() => {
                          const t = (document.body && document.body.innerText) || '';
                          return {
                            hasDailyAct: /每日活动|Daily activities/i.test(t),
                            hasRoutine: /日常任务/.test(t),
                            plusCount: (t.match(/\\+\\s*\\d+/g) || []).length,
                            url: location.href || '',
                          };
                        }"""
                    )
                    _log(
                        log,
                        f"{label} 未发现可做卡片（页面含每日活动={tip.get('hasDailyAct')} "
                        f"日常任务={tip.get('hasRoutine')} +分标记约 {tip.get('plusCount')} 个）",
                    )
                except Exception:
                    pass
            break

        for offer in pending:
            if _stopped(should_stop) or done >= max_offers:
                break

            key = _offer_key(offer)
            if key in attempted:
                continue
            attempted.add(key)

            href = (offer.get("href") or "").strip()
            title = (offer.get("text") or "")[:60]
            hint = f"{offer.get('text', '')} {offer.get('nearby', '')}"
            is_puzzle = bool(offer.get("is_puzzle"))
            kind = "拼图(仅打开)" if is_puzzle else label
            _log(log, f"处理{kind}: {title}")

            page = await _close_extra_pages(context, page)
            before = list(context.pages)
            list_page = page

            try:
                activity_page = await _open_offer_card(page, context, offer)
            except Exception as e:
                _log(log, f"打开{label}失败: {e}")
                try:
                    await page.goto(list_url, wait_until=DOMCONTENTLOADED, timeout=60000)
                except Exception:
                    pass
                continue

            try:
                await activity_page.wait_for_load_state(DOMCONTENTLOADED, timeout=30000)
            except Exception:
                pass

            try:
                if (
                    href
                    and activity_page == list_page
                    and "rewards.bing.com" in (activity_page.url or "")
                    and href.lower() not in (activity_page.url or "").lower()
                ):
                    await activity_page.goto(href, wait_until=DOMCONTENTLOADED, timeout=45000)
            except Exception:
                pass

            try:
                how = await _complete_activity_page(
                    activity_page,
                    offer_hint=hint,
                    open_only=is_puzzle,
                )
                _log(log, f"{label}完成 ({how}): {title}")
                done += 1
            except Exception as e:
                _log(log, f"{label}异常: {e}")

            try:
                if activity_page != list_page and not activity_page.is_closed():
                    await activity_page.close()
            except Exception:
                pass

            page = await _latest_page(context, list_page if not list_page.is_closed() else page)
            try:
                if list_url.rstrip("/") not in (page.url or ""):
                    await page.goto(list_url, wait_until=DOMCONTENTLOADED, timeout=60000)
                else:
                    await page.reload(wait_until=DOMCONTENTLOADED, timeout=60000)
                await page.wait_for_timeout(1200)
            except Exception:
                pass

            await _sleep(800, 1800)

            if len(context.pages) > len(before) + 1:
                page = await _close_extra_pages(context, page)

    _log(log, f"{label}结束，尝试完成 {done} 个")
    return page, done


async def complete_daily_activities(
    page: Page,
    context: BrowserContext,
    *,
    log: Optional[LogFn] = None,
    should_stop: Optional[StopFn] = None,
    max_offers: int = 16,
) -> int:
    """完成首页「每日活动」+ 赚取页「日常任务」（均只做对应分区内带积分卡片）。

    拼图类只需打开，无需真正拼图。首页不整页检索其它入口。
    """
    if _stopped(should_stop):
        return 0

    total = 0

    # 1) 首页「每日活动」——只扫该分区
    async def _collect_homepage(p: Page) -> list:
        offers = await collect_homepage_daily_set(p)
        if offers:
            return offers
        try:
            if "rewards.bing.com/dashboard" in (p.url or ""):
                await p.goto("https://rewards.bing.com/", wait_until=DOMCONTENTLOADED, timeout=60000)
                await p.wait_for_timeout(2000)
                return await collect_homepage_daily_set(p)
        except Exception:
            pass
        return offers

    page, n1 = await _run_point_offers(
        page,
        context,
        list_url="https://rewards.bing.com/dashboard",
        collect_fn=_collect_homepage,
        label="首页每日活动",
        log=log,
        should_stop=should_stop,
        max_offers=max_offers,
    )
    total += n1

    if _stopped(should_stop):
        return total

    # 2) 赚取页「日常任务」
    page, n2 = await _run_point_offers(
        page,
        context,
        list_url="https://rewards.bing.com/earn",
        collect_fn=collect_earn_offers,
        label="赚取页日常任务",
        log=log,
        should_stop=should_stop,
        max_offers=max_offers,
    )
    total += n2

    _log(log, f"活动合计完成 {total} 个（每日活动 {n1} + 日常任务 {n2}）")
    return total
