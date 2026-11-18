"""每日活动与自动领取（适配新版 rewards.bing.com Next.js UI）。"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

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
    href_l = (href or "").lower().strip()
    # 空链接/页内锚点：靠文案点击，不在这里拦截
    if not href_l or href_l.startswith(("javascript:", "mailto:", "#")):
        return False
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
        "microsoft.com/store",
        "apps.apple.com",
        "play.google.com",
    )
    return any(b in href_l for b in skip_bits)


def _is_skip_offer_text(*parts: str) -> bool:
    """过滤下载应用、等级门槛、邀请等无效「任务」。"""
    blob = " ".join(p for p in parts if p)
    if not blob:
        return False
    skip_words = (
        "立即下载",
        "必应应用",
        "新用户领取",
        "需要银牌",
        "需要金牌",
        "需要会员",
        "银牌级别",
        "金牌级别",
        "邀请好友",
        "壁纸",
        "安装扩展",
        "桌面应用",
        "下载应用",
        "App Store",
        "Google Play",
        # 首页新手引导入口，不是每日活动卡片本身
        "完成每日任务集",
        "开始使用 Rewards",
        "搜索 7 天",
    )
    return any(w in blob for w in skip_words)


async def _expand_homepage_dailyset(page: Page) -> None:
    """新版仪表盘「每日活动」(#dailyset) 默认折叠，需展开后才有 +分卡片。"""
    try:
        cur = page.url or ""
        if "section=dailyset" not in cur:
            await page.goto(
                "https://rewards.bing.com/dashboard?section=dailyset",
                wait_until=DOMCONTENTLOADED,
                timeout=60000,
            )
            await page.wait_for_timeout(1800)
    except Exception:
        pass

    for _ in range(2):
        try:
            closed = page.locator('#dailyset [aria-expanded="false"]')
            if await closed.count() <= 0:
                # 已展开，或找不到折叠按钮
                opened = page.locator('#dailyset [aria-expanded="true"]')
                if await opened.count() > 0:
                    return
            else:
                await closed.first.scroll_into_view_if_needed(timeout=4000)
                await closed.first.click(timeout=4000)
                await page.wait_for_timeout(1200)
                if await page.locator('#dailyset [aria-expanded="true"]').count() > 0:
                    return
        except Exception:
            pass
        try:
            heading = page.locator('#dailyset h2, h2:text-is("每日活动")').first
            if await heading.count() > 0:
                await heading.scroll_into_view_if_needed(timeout=3000)
                await heading.click(timeout=3000)
                await page.wait_for_timeout(1000)
        except Exception:
            pass


async def _dismiss_web_popups(page: Page) -> None:
    """关掉活动页常见遮罩/提示，避免挡住答题。"""
    for text in ("知道了", "我知道了", "Got it", "关闭", "Close", "确定", "OK", "跳过", "稍后", "Not now"):
        try:
            loc = page.get_by_role("button", name=re.compile(re.escape(text), re.I))
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click(timeout=1500)
                await page.wait_for_timeout(400)
                return
        except Exception:
            pass
        try:
            loc = page.get_by_text(text, exact=True)
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click(timeout=1500)
                await page.wait_for_timeout(400)
                return
        except Exception:
            pass


async def _clear_click_blockers(page: Page) -> None:
    """清掉 Rewards 首页高 z-index 飞层，避免 body/遮罩吞掉卡片点击。"""
    await _dismiss_web_popups(page)
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
    except Exception:
        pass
    try:
        await page.evaluate(
            """() => {
              const killers = [];
              for (const el of document.querySelectorAll('body *')) {
                const s = getComputedStyle(el);
                const z = parseInt(s.zIndex || '0', 10);
                if (!(z >= 1000 && (s.position === 'fixed' || s.position === 'absolute'))) continue;
                if (el.closest('#dailyset, [data-bi-id], main, header, nav')) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 80 || r.height < 80) continue;
                // 全屏/大半屏拦截层：隐藏指针
                if (r.width * r.height > window.innerWidth * window.innerHeight * 0.35) {
                  el.style.pointerEvents = 'none';
                  killers.push(1);
                }
              }
              return killers.length;
            }"""
        )
    except Exception:
        pass


def _offer_is_tour(offer: dict) -> bool:
    href = ((offer.get("href") or "") + " " + (offer.get("nearby") or "")).lower()
    return ("filters=sid" in href) or ("form=tgrew" in href)


def _looks_like_quiz(url: str, hint: str, body: str = "") -> bool:
    blob = f"{url} {hint} {body}".lower()
    return any(
        k in blob
        for k in (
            "quiz",
            "rqanswer",
            "rqstart",
            "极速问答",
            "测验",
            "投票",
            "trivia",
            "poll",
            "thisorthat",
            "warpspeed",
        )
    )


async def _wait_quiz_ready(page: Page, timeout_ms: int = 8000) -> bool:
    selectors = (
        "#rqStartQuiz",
        "#rqAnswerOption0",
        ".rqOption",
        "#btoption0",
        ".btOption",
        "#rqOverlayOptions",
        '[id^="rqAnswerOption"]',
        '[id^="btoption"]',
    )
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    return True
            except Exception:
                continue
        await page.wait_for_timeout(400)
    return False


async def _complete_activity_page(page: Page, *, offer_hint: str = "", open_only: bool = False) -> str:
    """尝试完成当前活动页，返回处理方式说明。

    注意：必应页顶栏永远有搜索框，不能一找不到测验就乱搜，否则会毁掉测验/探索积分归因。
    """
    await page.wait_for_timeout(900)
    await _dismiss_web_popups(page)
    url = (page.url or "").lower()
    hint = offer_hint or ""

    # 拼图或明确“仅打开”：停留即可
    if open_only or _is_puzzle_offer(url, hint):
        await page.wait_for_timeout(random.randint(1800, 3200))
        return "opened_puzzle" if _is_puzzle_offer(url, hint) else "opened_only"

    try:
        body_snip = ((await page.locator("body").inner_text(timeout=2000)) or "")[:1200]
    except Exception:
        body_snip = ""
    if _is_puzzle_offer(url, body_snip) or ("拼图" in body_snip and "排列图块" in body_snip):
        await page.wait_for_timeout(random.randint(1800, 3200))
        return "opened_puzzle"

    cur = (page.url or "").lower()
    is_search_landing = "bing.com" in cur and ("/search?" in cur or ("q=" in cur and "/search" in cur))
    is_quiz = _looks_like_quiz(url, hint, body_snip) and not (
        # 探索/风光搜索卡：即使页面里有无关 radio，也不要当测验点掉
        is_search_landing and not _looks_like_quiz(url, hint, "")
    )

    # checkuser / 活动链跳转后测验控件可能晚出
    if is_quiz or ((("rewards" in url) or ("checkuser" in url)) and not is_search_landing):
        await _wait_quiz_ready(page, 8000)
        await _dismiss_web_popups(page)

    # 投票/测验：多轮点选项/下一题（搜索落地页跳过，避免误点）
    quiz_steps = 0
    if is_quiz:
        quiz_sels = (
            "#rqStartQuiz",
            "#rqAnswerOption0",
            "#rqAnswerOption1",
            "#rqAnswerOption2",
            ".rqOption",
            "#btoption0",
            "#btoption1",
            "#btoption2",
            ".btOption",
            '[id^="rqAnswerOption"]',
            '[id^="btoption"]',
            'input[type="radio"]',
            '[role="radio"]',
            "label:has(input[type=radio])",
            "#rqNextQuestion",
            "#nextQuestionbtn",
            "button:has-text('提交')",
            "button:has-text('下一题')",
            "button:has-text('Next')",
            "input[type=submit]",
        )
        for _ in range(12):
            await _dismiss_web_popups(page)
            clicked = False
            for sel in quiz_sels:
                try:
                    loc = page.locator(sel)
                    n = await loc.count()
                    if n <= 0:
                        continue
                    idx = 0 if n == 1 else random.randint(0, min(n, 4) - 1)
                    target = loc.nth(idx)
                    if not await target.is_visible():
                        continue
                    await target.click(timeout=2500, force=True)
                    await page.wait_for_timeout(1100)
                    quiz_steps += 1
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                if quiz_steps == 0 and await _wait_quiz_ready(page, 2500):
                    continue
                break

        if quiz_steps:
            await page.wait_for_timeout(random.randint(1500, 2800))
            return f"quiz_steps_{quiz_steps}"

    # 已通过活动链落到带 q= 的搜索结果：停留/滚动；禁止改写搜索词
    if is_search_landing:
        # sid/tgrew 观光卡：点进结果可能丢掉归因，只停留滚动
        tour_like = ("filters=sid" in cur) or ("form=tgrew" in cur) or ("form=tgrew" in hint.lower())
        reported = False
        if tour_like:
            # 落地后立刻等 reportActivity；没有则多滚几轮再等
            try:
                await page.wait_for_request(
                    lambda r: "reportactivity" in (r.url or "").lower(),
                    timeout=8000,
                )
                reported = True
            except Exception:
                reported = False
        try:
            for _ in range(4 if tour_like else 2):
                await page.mouse.wheel(0, random.randint(500, 1100))
                await page.wait_for_timeout(random.randint(800, 1400))
        except Exception:
            pass
        if tour_like and not reported:
            try:
                await page.wait_for_request(
                    lambda r: "reportactivity" in (r.url or "").lower(),
                    timeout=10000,
                )
                reported = True
            except Exception:
                pass
        if not tour_like:
            # 普通 RewardsDO 搜索卡可点一条结果
            try:
                links = page.locator("#b_results h2 a, #b_results .b_algo h2 a")
                n = await links.count()
                if n > 0:
                    idx = 0 if n == 1 else random.randint(0, min(n, 3) - 1)
                    await _human_click_locator(page, links.nth(idx))
                    await _wait_page_ready(page)
                    await page.wait_for_timeout(random.randint(1500, 2800))
                    return "search_landed_result"
            except Exception:
                pass
        # 观光/sid 卡记账偏慢，多停留一会
        await page.wait_for_timeout(
            random.randint(12000 if tour_like else 2800, 18000 if tour_like else 4500)
        )
        if tour_like:
            return "search_tour_dwell_ok" if reported else "search_tour_dwell"
        return "search_landed_dwell"

    # 仅当活动本身像“需要搜索”，且当前还不是结果页时，才主动搜
    need_search = any(k in f"{hint} {body_snip}".lower() for k in ("搜索", "search", "查找", "逛一逛"))
    if need_search and not is_quiz:
        try:
            box = page.locator("#sb_form_q, input[name='q'], textarea[name='q']")
            if await box.count() > 0 and "bing.com" in cur:
                # 若框里已有活动带来的词，直接回车，不要换成随机词
                existing = ""
                try:
                    existing = (await box.first.input_value()) or ""
                except Exception:
                    existing = ""
                if existing.strip():
                    await page.keyboard.press("Enter")
                else:
                    q = random.choice(["天气", "新闻", "微软积分", "今日热点", "地图"])
                    await box.first.fill("")
                    await box.first.type(q, delay=60)
                    await page.keyboard.press("Enter")
                await page.wait_for_timeout(2500)
                return "search_submitted"
        except Exception:
            pass

    await page.wait_for_timeout(random.randint(2800, 4500))
    return "dwelt"


_COLLECT_SECTION_JS = """({ titles, nextSectionPattern, rootSelector, includeCompleted }) => {
  const norm = (s) => (s || '').trim().replace(/\\s+/g, ' ');
  const yOf = (el) => {
    try { return el.getBoundingClientRect().top + window.scrollY; }
    catch (e) { return 0; }
  };
  const ptsRe = /[+＋]\\s*\\d+/;
  const doneRe = /^(已完成|completed|已领取)$/i;
  const titleSet = (titles || []).map((t) => norm(t)).filter(Boolean);

  const readStatus = (card) => {
    let plus = null;
    let done = false;
    for (const s of card.querySelectorAll('div,span,p,em,strong,label')) {
      const st = norm(s.innerText || '');
      if (!st || st.length > 18) continue;
      if (doneRe.test(st)) { done = true; continue; }
      if (/^[+＋]\\s*\\d+$/.test(st)) { plus = st; continue; }
    }
    // 兜底：整卡文案
    const ct = norm(card.innerText || '');
    if (!done && /(^|\\s)(已完成|completed|已领取)(\\s|$)/i.test(ct) && !ptsRe.test(ct)) done = true;
    if (!plus) {
      const m = ct.match(/[+＋]\\s*(\\d+)/);
      if (m) plus = '+' + m[1];
    }
    return { plus, done };
  };

  const smallestCard = (start, root) => {
    let card = start;
    let best = start;
    for (let i = 0; i < 8; i++) {
      const p = card.parentElement;
      if (!p || p === root || p === document.body) break;
      const pt = norm(p.innerText || '');
      if (pt.length > 260) break;
      const plusCount = (pt.match(/[+＋]\\s*\\d+/g) || []).length;
      const doneCount = (pt.match(/已完成|completed|已领取/gi) || []).length;
      // 多卡集合：多个 +分 或多个「已完成」
      if (plusCount + doneCount >= 3) break;
      if (plusCount >= 2 && doneCount >= 1) break;
      card = p;
      const st = readStatus(card);
      if (st.plus || st.done) best = card;
      // 单卡且已有标题+状态即可停
      if ((st.plus || st.done) && pt.length >= 8 && pt.length <= 220 && plusCount <= 1) {
        best = card;
        break;
      }
    }
    return best;
  };

  let root = null;
  let matchedTitle = '';
  if (rootSelector) {
    root = document.querySelector(rootSelector);
    matchedTitle = titleSet[0] || rootSelector;
  }
  let heading = null;
  if (!root) {
    for (const el of document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,span,p')) {
      const own = norm(el.innerText || '');
      if (!own || own.length > 40) continue;
      for (const t of titleSet) {
        if (own === t || (own.startsWith(t) && own.length <= t.length + 12)) {
          heading = el; matchedTitle = t; break;
        }
      }
      if (heading) break;
    }
    if (!heading) return [];
    // 标题附近找分区容器
    root = heading.parentElement || heading;
    for (let i = 0; i < 6; i++) {
      const p = root.parentElement;
      if (!p || p === document.body) break;
      const pt = norm(p.innerText || '');
      if (pt.length > 6000) break;
      root = p;
      // 够大且含多链接就停
      if (p.querySelectorAll('a[href]').length >= 2 && pt.length > 80) break;
    }
  }

  const headingY = heading ? yOf(heading) : yOf(root);
  let nextSectionRe;
  try { nextSectionRe = new RegExp(nextSectionPattern, 'i'); }
  catch (e) { nextSectionRe = /^(更多活动|探索|游戏|必应搜索|邀请)/i; }
  let nextY = Infinity;
  for (const el of document.querySelectorAll('h1,h2,h3,h4,h5,h6')) {
    const own = norm(el.innerText || '');
    if (!own || own.length > 40) continue;
    if (titleSet.some((t) => own === t || own.startsWith(t))) continue;
    if (!nextSectionRe.test(own)) continue;
    const y = yOf(el);
    if (y > headingY + 40 && y < nextY) nextY = y;
  }
  if (nextY === Infinity) nextY = headingY + 4000;

  const items = [];
  const seen = new Set();
  const links = root.querySelectorAll('a[href], button, [role="link"]');
  for (const a of links) {
    const y = yOf(a);
    if (!rootSelector && (y < headingY - 40 || y >= nextY)) continue;
    const aText = norm(a.innerText || a.getAttribute('aria-label') || '');
    if (aText.length > 200) continue;
    const card = smallestCard(a, root);
    const cardText = norm(card.innerText || '');
    if (cardText.length < 4 || cardText.length > 260) continue;
    const st = readStatus(card);
    const plusCount = (cardText.match(/[+＋]\\s*\\d+/g) || []).length;
    if (plusCount > 1) continue; // 仍是多卡
    if (st.done && !includeCompleted) continue;
    if (!st.done && !st.plus && !/\\d+\\s*积分/.test(cardText)) continue;
    // 待做必须带 +分；完成卡是「数字 + 已完成」无加号
    if (!st.done && !ptsRe.test(cardText)) continue;
    if (st.done && ptsRe.test(cardText)) continue; // 完成卡不应再带 +分

    let href = '';
    if (a.tagName === 'A') href = a.href || '';
    else {
      const link = card.querySelector && card.querySelector('a[href]');
      if (link) href = link.href || '';
    }
    const titleGuess = cardText
      .replace(/[+＋]\\s*\\d+/g, ' ')
      .replace(/\\d+\\s*(已完成|已领取)/g, ' ')
      .replace(/\\b已完成\\b/g, ' ')
      .replace(/\\bcompleted\\b/ig, ' ')
      .replace(/\\b已领取\\b/g, ' ')
      .replace(/\\b已激活\\b/g, ' ')
      .replace(/\\d+\\s*积分/g, ' ')
      .replace(/需要[^\\s]{0,6}级别/g, ' ')
      .trim()
      .slice(0, 80);
    if (!titleGuess) continue;
    // 打卡/促销大卡过滤（非整张日常卡）
    if (/(个任务|连续打卡|开始使用 Rewards|Discord|Spotify)/i.test(cardText) && plusCount <= 1 && /\\d+\\/\\d+/.test(cardText)) {
      continue;
    }
    const key = ((href || '').split('#')[0] + '|' + titleGuess).toLowerCase().slice(0, 140);
    if (seen.has(key)) continue;
    seen.add(key);
    items.push({
      href: href || '',
      text: titleGuess,
      nearby: cardText.slice(0, 280),
      section: matchedTitle,
      titleGuess,
      status: st.done ? 'completed' : 'pending',
      pointsLabel: st.plus || '',
    });
  }
  items.sort((a, b) => (a.nearby || '').length - (b.nearby || '').length);
  return items;
}"""


async def collect_section_point_offers(
    page: Page,
    *,
    section_titles: list[str],
    next_section_pattern: str,
    root_selector: Optional[str] = None,
    include_completed: bool = False,
) -> list[dict]:
    """收集指定分区下的任务卡片。

    真实 DOM：待做卡带「+N」徽章；完成卡为「N 已完成」（无加号，右侧文案「已完成」）。
    """
    try:
        await page.evaluate(
            """(titles) => {
              const norm = (s) => (s || '').trim().replace(/\\s+/g, ' ');
              for (const el of document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,span,p,#dailyset')) {
                const own = norm(el.innerText || '');
                if (!own || own.length > 40) continue;
                if ((titles || []).some((t) => own === t || own.startsWith(t))) {
                  el.scrollIntoView({ block: 'center' });
                  break;
                }
              }
            }""",
            section_titles,
        )
        await page.wait_for_timeout(600)
    except Exception:
        pass

    offers = await page.evaluate(
        _COLLECT_SECTION_JS,
        {
            "titles": section_titles,
            "nextSectionPattern": next_section_pattern,
            "rootSelector": root_selector,
            "includeCompleted": include_completed,
        },
    )

    out = []
    seen = set()
    filtered_skip = 0
    for o in offers or []:
        href = (o.get("href") or "").strip()
        text = (o.get("text") or "").strip()
        nearby = (o.get("nearby") or text).strip()
        status = (o.get("status") or "pending").strip()
        if status != "completed":
            if not re.search(r"[+＋]\s*\d+", nearby or text) and not re.search(r"(\d+)\s*积分", nearby or text):
                continue
            # 完成文案优先：出现「已完成」且无 +分 → 不当待做
            if re.search(r"(已完成|completed|已领取)", nearby, re.I) and not re.search(r"[+＋]\s*\d+", nearby):
                filtered_skip += 1
                continue
        if _is_skip_offer_text(text, nearby):
            filtered_skip += 1
            continue
        if href and _is_skip_href(href):
            href = ""
        key = (href.split("#")[0].lower() if href else "") or f"text:{(nearby or text)[:80].lower()}"
        if key in seen:
            continue
        seen.add(key)
        label = (o.get("titleGuess") or text or nearby)[:120]
        click = label[:24].strip()
        if "完成此拼图" in (nearby or text):
            click = "完成此拼图"
        out.append(
            {
                "href": href,
                "text": label,
                "nearby": nearby[:220],
                "section": o.get("section") or "",
                "is_puzzle": _is_puzzle_offer(href, f"{text} {nearby}"),
                "click_text": click,
                "status": status,
                "points_label": o.get("pointsLabel") or "",
            }
        )

    collect_section_point_offers._last_filter_skip = filtered_skip  # type: ignore[attr-defined]
    collect_section_point_offers._last_raw_count = len(offers or [])  # type: ignore[attr-defined]

    def _rank(item: dict) -> tuple:
        near = item.get("nearby") or ""
        pts = 0
        m = re.search(r"[+＋]\s*(\d+)", near)
        if m:
            pts = int(m.group(1))
        return (0 if item.get("is_puzzle") else 1, -pts, item.get("text") or "")

    out.sort(key=_rank)
    return out


async def collect_homepage_daily_set(page: Page, *, include_completed: bool = False) -> list[dict]:
    """首页「每日活动」(#dailyset) 分区卡片。

    实测：分区默认折叠；完成卡文案为「N 已完成」（无 +）；待做卡为「+N」。
    """
    await _expand_homepage_dailyset(page)
    next_pat = (
        r"^(日常任务|更多活动|探索|游戏|必应搜索|邀请|抽奖|购物|连续打卡|"
        r"More activities|Earn|Redeem|Punch cards|开始使用)"
    )
    # 优先用 #dailyset，避免扫到印章/连续打卡的 +500
    last = await collect_section_point_offers(
        page,
        section_titles=["每日活动", "Daily activities", "Daily set"],
        next_section_pattern=next_pat,
        root_selector="#dailyset",
        include_completed=include_completed,
    )
    if last:
        return last
    for titles in (
        ["每日活动", "Daily activities", "Daily Activities", "Daily set", "Daily Set"],
        ["每日任务", "Daily tasks"],
    ):
        last = await collect_section_point_offers(
            page,
            section_titles=titles,
            next_section_pattern=next_pat,
            include_completed=include_completed,
        )
        if last:
            return last
    return last


async def collect_earn_offers(page: Page, *, include_completed: bool = False) -> list[dict]:
    """积分赚取页「日常任务」分区卡片（待做=+N，完成=已完成）。"""
    return await collect_section_point_offers(
        page,
        section_titles=["日常任务"],
        next_section_pattern=(
            r"^(每日任务|每日活动|更多活动|探索|游戏|必应搜索|邀请|抽奖|购物|升级活动|"
            r"Redeem|More activities|Daily activities|Daily set|连续打卡)"
        ),
        include_completed=include_completed,
    )


async def claim_available_rewards(
    page: Page,
    *,
    context: Optional[BrowserContext] = None,
    log: Optional[LogFn] = None,
    should_stop: Optional[StopFn] = None,
) -> int:
    """尝试领取仪表盘上的可领取积分。返回点击领取的次数。"""
    if _stopped(should_stop):
        return 0

    _log(log, "开始自动领取...")
    clicks = 0
    try:
        if page.is_closed() and context is not None:
            pages = [p for p in context.pages if not p.is_closed()]
            page = pages[-1] if pages else await context.new_page()
        await page.goto("https://rewards.bing.com/dashboard", wait_until=DOMCONTENTLOADED, timeout=60000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        # 活动过程中旧 page 可能已关：用 context 新开一页再试一次
        if context is not None and ("closed" in str(e).lower() or "Target page" in str(e)):
            try:
                pages = [p for p in context.pages if not p.is_closed()]
                page = pages[-1] if pages else await context.new_page()
                await page.goto(
                    "https://rewards.bing.com/dashboard",
                    wait_until=DOMCONTENTLOADED,
                    timeout=60000,
                )
                await page.wait_for_timeout(2000)
            except Exception as e2:
                _log(log, f"打开仪表盘失败: {e2}")
                return 0
        else:
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
    """活动去重键。checkuser 等跳转链会被多卡共用，必须以标题区分。"""
    href = (offer.get("href") or "").strip().split("#")[0].lower()
    title = (offer.get("text") or offer.get("titleGuess") or offer.get("click_text") or "").strip().lower()
    title = re.sub(r"\s+", " ", title)[:48]
    shared = (not href) or any(
        x in href
        for x in (
            "rewards/checkuser",
            "bing.com/search?",
            "bing.com/rewards/redeem",
        )
    )
    if title and (shared or not href):
        return f"title:{title}"
    if href and title:
        return f"{href}|{title[:24]}"
    if href:
        return href
    near = (offer.get("nearby") or "").strip().lower()
    return f"text:{near[:80]}"


async def _wait_page_ready(page: Page, *, timeout_ms: int = 45000) -> None:
    """等活动页尽量加载完整：DOMContentLoaded → load，再给 SPA/跳转一点缓冲。"""
    try:
        await page.wait_for_load_state(DOMCONTENTLOADED, timeout=timeout_ms)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("load", timeout=timeout_ms)
    except Exception:
        pass
    # checkuser / 客户端跳转后再等一轮
    await page.wait_for_timeout(1200)
    try:
        await page.wait_for_load_state(DOMCONTENTLOADED, timeout=15000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("load", timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(800)


async def _human_pause(ms_min: int = 350, ms_max: int = 900) -> None:
    await asyncio.sleep(random.uniform(ms_min, ms_max) / 1000.0)


async def _human_click_locator(page: Page, locator) -> None:
    """滚动可见 → 鼠标移入 → 短停 → 点击（单次，模拟真人）。"""
    # 兼容 Locator / ElementHandle
    target = locator.first if hasattr(locator, "first") else locator
    try:
        await target.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    await _human_pause(250, 700)
    box = None
    try:
        box = await target.bounding_box()
    except Exception:
        box = None
    if box and box.get("width", 0) > 2 and box.get("height", 0) > 2:
        x = box["x"] + box["width"] * random.uniform(0.28, 0.72)
        y = box["y"] + box["height"] * random.uniform(0.28, 0.72)
        await page.mouse.move(x, y, steps=random.randint(10, 22))
        await _human_pause(120, 380)
        await page.mouse.down()
        await _human_pause(40, 140)
        await page.mouse.up()
    else:
        await target.click(timeout=5000, delay=random.randint(60, 160))
    await _human_pause(200, 500)


def _offer_search_query(href: str) -> str:
    try:
        q = parse_qs(urlparse(href).query).get("q", [""])[0]
        return unquote(q or "").strip()
    except Exception:
        return ""


def _href_unique_token(href: str) -> str:
    """从活动链接提取尽量唯一的匹配片段（sid / CREA / 完整 q）。"""
    try:
        u = urlparse(href)
        qs = parse_qs(u.query)
        for key in ("filters", "sid", "CREA", "OCID", "FORM", "form"):
            vals = qs.get(key) or []
            if vals and len(str(vals[0])) >= 6:
                return str(vals[0])[:80]
        q = unquote((qs.get("q") or [""])[0] or "").strip()
        if q:
            return q
    except Exception:
        pass
    return ""


async def _resolve_offer_locator(page: Page, offer: dict):
    """优先精确 href，避免点到同区其它已完成搜索卡。"""
    href = (offer.get("href") or "").strip()
    click_text = (offer.get("click_text") or "").strip()
    title = (offer.get("text") or "").strip()
    nearby = (offer.get("nearby") or title).strip()

    if href:
        matched = await page.evaluate(
            """(href) => {
              const links = [...document.querySelectorAll('a[href]')];
              // 1) 完整 href
              let hit = links.filter((a) => a.href === href);
              if (hit.length === 1) return hit[0].href;
              // 2) 查询串一致（忽略域名）
              try {
                const want = new URL(href);
                hit = links.filter((a) => {
                  try {
                    const u = new URL(a.href);
                    return u.search && u.search === want.search;
                  } catch (e) { return false; }
                });
                if (hit.length === 1) return hit[0].href;
                const q = decodeURIComponent(want.searchParams.get('q') || '');
                if (q) {
                  hit = links.filter((a) => {
                    try {
                      const u = new URL(a.href);
                      return decodeURIComponent(u.searchParams.get('q') || '').includes(q);
                    } catch (e) { return false; }
                  });
                  if (hit.length === 1) return hit[0].href;
                }
                const filters = want.searchParams.get('filters') || '';
                if (filters.length >= 8) {
                  hit = links.filter((a) => (a.href || '').includes(filters.slice(0, 40)));
                  if (hit.length === 1) return hit[0].href;
                }
              } catch (e) {}
              return '';
            }""",
            href,
        )
        if matched:
            # 避免 CSS 特殊字符：用 JS 按绝对 href 找唯一节点再包成 locator
            ok = await page.evaluate(
                """(h) => {
                  const el = [...document.querySelectorAll('a[href]')].find((a) => a.href === h);
                  if (!el) return false;
                  el.setAttribute('data-mra-offer', '1');
                  return true;
                }""",
                matched,
            )
            if ok:
                loc = page.locator('a[data-mra-offer="1"]')
                if await loc.count() > 0:
                    return loc.first
            # 退化为按 q 文本的 a:has-text 不稳，最后直接返回 None 让上层 goto href
            return None

    # 标题：要求出现在可点击 a 上，且尽量唯一
    for needle in (title[:18], click_text, "完成此拼图"):
        if not needle or len(needle) < 4:
            continue
        loc = page.locator(f'a:has-text("{needle}")')
        n = await loc.count()
        if n == 1:
            return loc.first
        if n > 1:
            # 多个时结合 +分 父卡仍带 pending 的那个不好判，退回第一个含 needle 且 href 含 search 的
            for i in range(n):
                a = loc.nth(i)
                try:
                    h = (await a.get_attribute("href")) or ""
                except Exception:
                    h = ""
                if href and _offer_search_query(href) and _offer_search_query(href) in unquote(h):
                    return a
            return loc.first

    first_line = (nearby.split("+")[0] or nearby)[:16].strip()
    if first_line and len(first_line) >= 4:
        loc = page.locator(f'a:has-text("{first_line}")')
        if await loc.count() == 1:
            return loc.first
    return None


async def _ensure_correct_landing(page: Page, offer: dict, *, log: Optional[LogFn] = None) -> None:
    """若点错到其它搜索卡，强制导航到本活动 href。"""
    href = (offer.get("href") or "").strip()
    if not href:
        return
    expected_q = _offer_search_query(href)
    if not expected_q:
        return
    cur = page.url or ""
    cur_q = _offer_search_query(cur)
    # 也看搜索框
    try:
        box = page.locator("#sb_form_q, input[name='q']")
        if await box.count() > 0:
            val = (await box.first.input_value()) or ""
            if expected_q in val or expected_q in unquote(val):
                return
    except Exception:
        pass
    if expected_q in unquote(cur) or (cur_q and (expected_q in cur_q or cur_q in expected_q)):
        return
    _log(log, f"落地页不匹配（期望「{expected_q}」，实际「{cur_q or cur[:60]}」），改开正确链接")
    await page.goto(href, wait_until="load", timeout=45000)
    await _wait_page_ready(page)


async def _js_click_offer_anchor(page: Page, href: str) -> bool:
    """直接触发卡片 <a>.click()，绕过 body 遮罩，保留 target=_blank 与 Rewards 归因。"""
    if not href:
        return False
    try:
        return bool(
            await page.evaluate(
                """(h) => {
                  const links = [...document.querySelectorAll('a[href]')];
                  let el = links.find((a) => a.href === h);
                  if (!el) {
                    try {
                      const want = new URL(h);
                      const q = decodeURIComponent(want.searchParams.get('q') || '');
                      const filters = want.searchParams.get('filters') || '';
                      if (q) {
                        el = links.find((a) => {
                          try {
                            return decodeURIComponent(new URL(a.href).searchParams.get('q') || '').includes(q);
                          } catch (e) { return false; }
                        });
                      }
                      if (!el && filters.length >= 8) {
                        el = links.find((a) => (a.href || '').includes(filters.slice(0, 40)));
                      }
                    } catch (e) {}
                  }
                  if (!el) {
                    el = document.querySelector('a[data-mra-offer="1"]');
                  }
                  if (!el) return false;
                  el.scrollIntoView({ block: 'center', inline: 'nearest' });
                  el.click();
                  return true;
                }""",
                href,
            )
        )
    except Exception:
        return False


async def _open_offer_card(
    page: Page,
    context: BrowserContext,
    offer: dict,
    *,
    log: Optional[LogFn] = None,
) -> Page:
    """拟人点击单张任务卡片；同一时刻只开这一张。

    首页常有高 z-index 飞层导致 mouse 点到 body；观光卡必须点真实 <a>
    才会带上 referrer 并在 Bing 侧触发 reportActivity。禁止优先 page.goto。
    """
    href = (offer.get("href") or "").strip()
    tour = _offer_is_tour(offer)

    page = await _close_extra_pages(context, page)
    before_pages = list(context.pages)
    await _clear_click_blockers(page)

    loc = await _resolve_offer_locator(page, offer)
    new_page = None
    report_hits: list[str] = []

    def _on_req(req) -> None:
        u = (req.url or "").lower()
        if "reportactivity" in u:
            report_hits.append(req.url)

    context.on("request", _on_req)
    try:
        async def _opened() -> bool:
            if any(p not in before_pages and not p.is_closed() for p in context.pages):
                return True
            cur = page.url or ""
            if href and "rewards.bing.com" not in cur and ("bing.com/search" in cur or "bing.com/" in cur):
                return True
            expected_q = _offer_search_query(href)
            if expected_q and expected_q in unquote(cur):
                return True
            return False

        async def _try_human() -> None:
            if loc is None:
                return
            try:
                await _human_click_locator(page, loc)
            except Exception:
                pass

        async def _try_force_locator() -> None:
            if loc is None:
                return
            try:
                await loc.click(timeout=4000, force=True, delay=random.randint(40, 120))
            except Exception:
                pass

        async def _try_js_anchor() -> None:
            await _js_click_offer_anchor(page, href)

        # 顺序：拟人 → 校验；失败再 force → JS <a>.click（观光卡关键）
        attempts = (_try_human, _try_force_locator, _try_js_anchor)
        if tour:
            # 观光卡优先 DOM click，避免遮罩吞鼠标后又 goto 丢归因
            attempts = (_try_js_anchor, _try_force_locator, _try_human)

        for attempt in attempts:
            before_pages = list(context.pages)
            try:
                async with context.expect_page(timeout=8000) as new_page_info:
                    await attempt()
                new_page = await new_page_info.value
                break
            except Exception:
                await page.wait_for_timeout(600)
                if await _opened():
                    break
                continue

        if new_page is None and not await _opened() and href:
            # 最后手段：带 Referer 打开，尽量贴近从 Rewards 点出
            try:
                await _human_pause(300, 600)
                await page.goto(
                    href,
                    wait_until="load",
                    timeout=45000,
                    referer="https://rewards.bing.com/",
                )
            except TypeError:
                # 旧版 Playwright 无 referer 参数
                try:
                    await page.goto(href, wait_until="load", timeout=45000)
                except Exception:
                    pass
            except Exception:
                try:
                    await page.evaluate(
                        """(u) => {
                          const a = document.createElement('a');
                          a.href = u; a.target = '_blank'; a.rel = 'noopener noreferrer';
                          document.body.appendChild(a); a.click(); a.remove();
                        }""",
                        href,
                    )
                    await page.wait_for_timeout(1500)
                except Exception:
                    pass

        pages_now = [p for p in context.pages if not p.is_closed()]
        if new_page is None:
            newcomers = [p for p in pages_now if p not in before_pages]
            if newcomers:
                new_page = newcomers[-1]
            else:
                new_page = page

        for p in pages_now:
            if p == page or p == new_page:
                continue
            try:
                await p.close()
            except Exception:
                pass

        await _wait_page_ready(new_page)
        # 观光卡：若被 _ensure 纠偏成 goto，可能丢掉 rdrig；仅在 q 明显错时纠正
        if tour:
            expected_q = _offer_search_query(href)
            cur_q = _offer_search_query(new_page.url or "")
            if expected_q and expected_q not in unquote(new_page.url or "") and expected_q not in (cur_q or ""):
                await _ensure_correct_landing(new_page, offer, log=log)
        else:
            await _ensure_correct_landing(new_page, offer, log=log)

        if tour and not report_hits:
            try:
                await new_page.wait_for_request(
                    lambda r: "reportactivity" in (r.url or "").lower(),
                    timeout=10000,
                )
                report_hits.append("late")
            except Exception:
                pass
            if report_hits:
                _log(log, "观光卡已收到 reportActivity")
            else:
                _log(log, "观光卡暂未捕获 reportActivity，继续停留等待记账")
        return new_page
    finally:
        try:
            context.remove_listener("request", _on_req)
        except Exception:
            pass


async def _reload_offer_list(page: Page, list_url: str) -> Page:
    try:
        if list_url.rstrip("/") not in (page.url or ""):
            await page.goto(list_url, wait_until="load", timeout=60000)
        else:
            await page.reload(wait_until="load", timeout=60000)
        await page.wait_for_timeout(random.randint(1600, 2600))
    except Exception:
        pass
    return page


async def _execute_one_offer(
    page: Page,
    context: BrowserContext,
    offer: dict,
    *,
    list_url: str,
    label: str,
    log: Optional[LogFn] = None,
) -> Page:
    """只操作一张卡：拟人点击 → 等加载 → 完成动作 → 关页 → 刷新列表。"""
    href = (offer.get("href") or "").strip()
    title = (offer.get("text") or "")[:60]
    hint = f"{offer.get('text', '')} {offer.get('nearby', '')}"
    is_puzzle = bool(offer.get("is_puzzle"))

    page = await _close_extra_pages(context, page)
    list_page = page
    await _human_pause(400, 1100)

    activity_page = await _open_offer_card(page, context, offer, log=log)
    await _wait_page_ready(activity_page)
    if not _offer_is_tour(offer):
        await _ensure_correct_landing(activity_page, offer, log=log)

    try:
        if (
            href
            and activity_page == list_page
            and "rewards.bing.com" in (activity_page.url or "")
            and href.lower() not in (activity_page.url or "").lower()
        ):
            await _human_pause(300, 700)
            # 仍停在仪表盘：再点一次真实锚点（goto 易丢观光卡归因）
            before = list(context.pages)
            try:
                async with context.expect_page(timeout=8000) as ni:
                    await _js_click_offer_anchor(list_page, href)
                activity_page = await ni.value
            except Exception:
                newcomers = [p for p in context.pages if p not in before and not p.is_closed()]
                if newcomers:
                    activity_page = newcomers[-1]
                elif activity_page == list_page and "rewards.bing.com" in (activity_page.url or ""):
                    try:
                        await activity_page.goto(
                            href,
                            wait_until="load",
                            timeout=45000,
                            referer="https://rewards.bing.com/",
                        )
                    except TypeError:
                        await activity_page.goto(href, wait_until="load", timeout=45000)
            await _wait_page_ready(activity_page)
            if not _offer_is_tour(offer):
                await _ensure_correct_landing(activity_page, offer, log=log)
    except Exception:
        pass

    try:
        how = await _complete_activity_page(
            activity_page,
            offer_hint=hint,
            open_only=is_puzzle,
        )
        _log(log, f"{label}已操作 ({how}): {title}")
        await activity_page.wait_for_timeout(random.randint(1800, 3200))
    except Exception as e:
        _log(log, f"{label}异常: {e}")

    try:
        if activity_page != list_page and not activity_page.is_closed():
            await activity_page.close()
    except Exception:
        pass

    page = await _latest_page(context, list_page if not list_page.is_closed() else page)
    page = await _close_extra_pages(context, page)
    await _human_pause(500, 1200)
    page = await _reload_offer_list(page, list_url)
    # 刷新后再等一会，再交给外层做完成检测
    await _sleep(1600, 2800)
    return page


async def _check_offer_completion(page: Page, offer: dict) -> dict:
    """抓取同一活动卡片状态：以状态节点「已完成」为准（完成卡是「N 已完成」，无 +）。"""
    href = (offer.get("href") or "").strip()
    title = (offer.get("text") or offer.get("click_text") or "").strip()[:48]
    title_key = title[:16].strip()
    try:
        info = await page.evaluate(
            """({ href, title, titleKey }) => {
              const norm = (s) => (s || '').trim().replace(/\\s+/g, ' ');
              const ptsRe = /[+＋]\\s*\\d+/;
              const doneExact = /^(已完成|completed|已领取)$/i;
              let best = null;
              const sharedHref = !href || /rewards\\/checkuser|bing\\.com\\/search\\?/i.test(href);
              for (const a of document.querySelectorAll('a[href], button, [role="link"]')) {
                const h = ((a.href || a.getAttribute('href') || '') + '').split('#')[0];
                const aText = norm(a.innerText || a.getAttribute('aria-label') || '');
                let score = 0;
                // 共用 checkuser 时标题权重更高，避免串到别的卡
                if (titleKey && (aText.includes(titleKey) || norm((a.closest('div')||{}).innerText||'').includes(titleKey))) score += sharedHref ? 10 : 5;
                if (title && aText.includes(title.slice(0, 10))) score += 3;
                if (!sharedHref && href && h && (h === href || (href.length > 12 && h.includes(href.slice(0, 48))))) score += 8;
                if (score < 5) continue;
                // 收束到单卡
                let card = a;
                for (let i = 0; i < 8; i++) {
                  const p = card.parentElement;
                  if (!p || p === document.body) break;
                  const pt = norm(p.innerText || '');
                  if (pt.length > 260) break;
                  const plusCount = (pt.match(/[+＋]\\s*\\d+/g) || []).length;
                  const doneCount = (pt.match(/已完成|completed|已领取/gi) || []).length;
                  if (plusCount + doneCount >= 3) break;
                  card = p;
                  if ((ptsRe.test(pt) || doneCount) && pt.length <= 220) break;
                }
                const ct = norm(card.innerText || '');
                let hasDoneLabel = false;
                let hasPlus = false;
                for (const s of card.querySelectorAll('div,span,p,em,strong')) {
                  const st = norm(s.innerText || '');
                  if (!st || st.length > 18) continue;
                  if (doneExact.test(st)) hasDoneLabel = true;
                  if (/^[+＋]\\s*\\d+$/.test(st)) hasPlus = true;
                }
                if (!hasPlus) hasPlus = ptsRe.test(ct);
                if (!hasDoneLabel) hasDoneLabel = /(^|\\s)(已完成|completed|已领取)(\\s|$)/i.test(ct) && !hasPlus;
                const cand = { score, text: ct.slice(0, 180), hasPlus, hasDoneLabel, len: ct.length };
                if (!best || cand.score > best.score || (cand.score === best.score && cand.len < best.len)) best = cand;
              }
              if (!best) return { found: false, hasPlus: false, hasDoneLabel: false, text: '' };
              return { found: true, hasPlus: !!best.hasPlus, hasDoneLabel: !!best.hasDoneLabel, text: best.text };
            }""",
            {"href": href, "title": title, "titleKey": title_key},
        )
    except Exception as e:
        return {"done": False, "reason": f"状态抓取失败: {e}", "has_plus": False, "has_done_label": False}

    if not info or not info.get("found"):
        return {"done": True, "reason": "卡片已消失", "has_plus": False, "has_done_label": False}

    has_done = bool(info.get("hasDoneLabel"))
    has_plus = bool(info.get("hasPlus"))
    if has_done and not has_plus:
        return {"done": True, "reason": "卡片显示已完成", "has_plus": False, "has_done_label": True}
    if has_plus:
        return {
            "done": False,
            "reason": "有已完成文案但仍带+分" if has_done else "卡片仍带+分",
            "has_plus": True,
            "has_done_label": has_done,
        }
    return {"done": True, "reason": "卡片不再带+分", "has_plus": False, "has_done_label": has_done}


async def _offer_still_pending(collect_fn: Callable[[Page], Awaitable[list]], page: Page, offer: dict) -> bool:
    """未完成：状态节点不是「已完成」，且待做列表仍能扫到同 key。"""
    status = await _check_offer_completion(page, offer)
    if status.get("done"):
        return False
    key = _offer_key(offer)
    remaining = await collect_fn(page)
    return any(_offer_key(o) == key for o in remaining)


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
    max_retries_per_offer: int = 3,
) -> tuple[Page, int]:
    """逐张拟人操作活动卡：点一张 → 完成 → 刷新检测；未完成则重复该卡。"""
    if _stopped(should_stop):
        return page, 0

    _log(log, f"开始{label}（逐张拟人操作）...")
    try:
        await page.goto(list_url, wait_until="load", timeout=60000)
        await page.wait_for_timeout(random.randint(2000, 3200))
    except Exception as e:
        _log(log, f"打开{label}页面失败: {e}")
        return page, 0

    verified = 0
    hard_fail: set[str] = set()
    empty_rounds = 0

    while verified < max_offers and not _stopped(should_stop):
        page = await _close_extra_pages(context, page)
        offers = await collect_fn(page)
        pending = [o for o in offers if _offer_key(o) not in hard_fail]
        _log(log, f"{label} 待做 {len(pending)} 个（候选 {len(offers)}）")
        if not pending:
            empty_rounds += 1
            if empty_rounds == 1 and verified == 0:
                try:
                    tip = await page.evaluate(
                        """() => {
                          const t = (document.body && document.body.innerText) || '';
                          return {
                            hasDailyAct: /每日活动|Daily activities/i.test(t),
                            hasDailyTask: /每日任务|Daily set|Daily tasks/i.test(t),
                            hasRoutine: /日常任务/.test(t),
                            plusCount: (t.match(/[+＋]\\s*\\d+/g) || []).length,
                            url: location.href || '',
                          };
                        }"""
                    )
                    raw_n = getattr(collect_section_point_offers, "_last_raw_count", None)
                    skip_n = getattr(collect_section_point_offers, "_last_filter_skip", None)
                    _log(
                        log,
                        f"{label} 未发现可做卡片（页面含每日活动={tip.get('hasDailyAct')} "
                        f"每日任务={tip.get('hasDailyTask')} 日常任务={tip.get('hasRoutine')} "
                        f"+分标记约 {tip.get('plusCount')} 个"
                        f"；识别候选={raw_n} 文本过滤={skip_n}）",
                    )
                except Exception:
                    pass
            break

        empty_rounds = 0
        # 严格一次只取一张，做完并复核后再扫下一张
        offer = pending[0]
        key = _offer_key(offer)
        title = (offer.get("text") or "")[:60]
        current = offer
        ok = False

        for attempt in range(1, max_retries_per_offer + 1):
            if _stopped(should_stop):
                break
            _log(
                log,
                f"处理{label}: {title}"
                + (f"（第 {attempt}/{max_retries_per_offer} 次）" if attempt > 1 else ""),
            )
            try:
                page = await _execute_one_offer(
                    page,
                    context,
                    current,
                    list_url=list_url,
                    label=label,
                    log=log,
                )
            except Exception as e:
                _log(log, f"打开/执行{label}失败: {e}")
                page = await _close_extra_pages(context, page)
                page = await _reload_offer_list(page, list_url)
                refreshed = await collect_fn(page)
                current = next((o for o in refreshed if _offer_key(o) == key), current)
                await _human_pause(700, 1400)
                continue

            # 完成检测前再显式刷新一次，避免旧 DOM
            page = await _reload_offer_list(page, list_url)
            await _human_pause(800, 1500)
            status = await _check_offer_completion(page, current)
            still = await _offer_still_pending(collect_fn, page, current)
            if status.get("done") and not still:
                verified += 1
                ok = True
                _log(log, f"{label}复核通过（{status.get('reason')}）: {title}")
                break

            if status.get("done") and still:
                _log(log, f"{label}状态冲突（{status.get('reason')}，收集器仍见+分）: {title}")
            else:
                _log(log, f"{label}复核未通过（{status.get('reason')}）: {title}")

            refreshed = await collect_fn(page)
            found = next((o for o in refreshed if _offer_key(o) == key), None)
            if found is None:
                verified += 1
                ok = True
                _log(log, f"{label}复核通过（刷新后卡片消失）: {title}")
                break
            current = found
            if attempt < max_retries_per_offer:
                _log(log, f"{label}未完成，重复操作: {title}")
                await _human_pause(900, 1800)

        if not ok:
            hard_fail.add(key)
            _log(log, f"{label}复核失败已达 {max_retries_per_offer} 次，跳过: {title}")

        page = await _close_extra_pages(context, page)
        # 下一张卡前稍停，避免连点
        await _human_pause(800, 1600)

    # 对先前失败的卡再整轮重试一次（避免点错/归因延迟导致误杀）
    if hard_fail and not _stopped(should_stop):
        retry_keys = set(hard_fail)
        hard_fail.clear()
        _log(log, f"{label} 对失败卡片再试一轮（{len(retry_keys)} 个）")
        page = await _reload_offer_list(page, list_url)
        await _human_pause(1000, 1800)
        offers = await collect_fn(page)
        for offer in offers:
            if _stopped(should_stop) or verified >= max_offers:
                break
            key = _offer_key(offer)
            if key not in retry_keys:
                continue
            title = (offer.get("text") or "")[:60]
            current = offer
            ok = False
            for attempt in range(1, max_retries_per_offer + 1):
                _log(log, f"重试{label}: {title}（第 {attempt}/{max_retries_per_offer} 次）")
                try:
                    page = await _execute_one_offer(
                        page, context, current, list_url=list_url, label=label, log=log
                    )
                except Exception as e:
                    _log(log, f"重试打开失败: {e}")
                    page = await _reload_offer_list(page, list_url)
                    continue
                page = await _reload_offer_list(page, list_url)
                status = await _check_offer_completion(page, current)
                still = await _offer_still_pending(collect_fn, page, current)
                if status.get("done") and not still:
                    verified += 1
                    ok = True
                    _log(log, f"{label}重试复核通过（{status.get('reason')}）: {title}")
                    break
                found = next((o for o in await collect_fn(page) if _offer_key(o) == key), None)
                if found is None:
                    verified += 1
                    ok = True
                    _log(log, f"{label}重试复核通过（卡片消失）: {title}")
                    break
                current = found
                await _human_pause(900, 1600)
            if not ok:
                hard_fail.add(key)
                _log(log, f"{label}重试仍未完成，跳过: {title}")
            page = await _close_extra_pages(context, page)
            await _human_pause(700, 1400)

    page = await _reload_offer_list(page, list_url)
    all_left = await collect_fn(page)
    if all_left:
        titles = ", ".join((o.get("text") or "")[:24] for o in all_left[:5])
        _log(log, f"{label}结束仍剩 {len(all_left)} 个带 +分: {titles}")
    _log(log, f"{label}结束，复核通过 {verified} 个")
    return page, verified


async def complete_daily_activities(
    page: Page,
    context: BrowserContext,
    *,
    log: Optional[LogFn] = None,
    should_stop: Optional[StopFn] = None,
    max_offers: int = 16,
) -> tuple[Page, int]:
    """完成首页每日任务/活动 + 赚取页「日常任务」。

    拼图类只需打开，无需真正拼图。返回 (可用 page, 完成数)，避免调用方持有已关闭页。
    """
    if _stopped(should_stop):
        return page, 0

    total = 0

    # 1) 首页「每日任务/每日活动」——只扫对应分区
    async def _collect_homepage(p: Page) -> list:
        offers = await collect_homepage_daily_set(p)
        if offers:
            return offers
        try:
            if "rewards.bing.com/dashboard" in (p.url or "") or (p.url or "").rstrip("/").endswith(
                "rewards.bing.com"
            ):
                await p.goto("https://rewards.bing.com/", wait_until=DOMCONTENTLOADED, timeout=60000)
                await p.wait_for_timeout(2000)
                return await collect_homepage_daily_set(p)
        except Exception:
            pass
        return offers

    page, n1 = await _run_point_offers(
        page,
        context,
        list_url="https://rewards.bing.com/dashboard?section=dailyset",
        collect_fn=_collect_homepage,
        label="首页每日活动",
        log=log,
        should_stop=should_stop,
        max_offers=max_offers,
    )
    total += n1

    if _stopped(should_stop):
        return await _latest_page(context, page), total

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

    page = await _latest_page(context, page)
    _log(log, f"活动合计完成 {total} 个（每日活动 {n1} + 日常任务 {n2}）")
    return page, total
