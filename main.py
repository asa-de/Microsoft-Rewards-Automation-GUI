import asyncio
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from playwright.async_api import async_playwright

# 常量：集中管理字符串，避免拼写检查对字符串字面量误报
BROWSER_CHANNEL = "msedge"
DOMCONTENTLOADED = "domcontentloaded"

# 记录程序启动时间，用于展示运行时长
APP_START_TS = time.time()


async def _maybe_click(page, selectors: list[str]) -> bool:
    """尝试点击一组 selector 中第一个可点击的元素。"""
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            try:
                if not await el.is_visible():
                    continue
            except Exception:
                # 部分情况下 is_visible 可能抛异常（比如 detached）
                pass
            await el.click(timeout=1500)
            return True
        except Exception:
            continue
    return False


async def _maybe_accept_bing_dialogs(page) -> None:
    """尽量处理 Bing/Consent 弹窗（失败也不影响主流程）。"""
    # 常见的 consent/隐私提示按钮（不同地区/语言可能不同）
    selectors = [
        "#bnp_btn_accept",  # Bing Cookie banner
        "button#bnp_btn_accept",
        "button:has-text('Accept')",
        "button:has-text('I agree')",
        "button:has-text('同意')",
        "button:has-text('接受')",
        "button:has-text('我同意')",
    ]
    try:
        await _maybe_click(page, selectors)
    except Exception:
        pass


async def _human_type_into_search_box(page, query: str) -> None:
    """更像真人：点击搜索框 -> 全选清空 -> 逐字输入。"""
    box = "#sb_form_q"
    await page.wait_for_selector(box, state="visible", timeout=8000)
    await page.click(box)
    # 全选清空（在 Windows/Linux 下 Control+A 更通用）
    try:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
    except Exception:
        # 兜底：直接填充
        pass

    # 逐字输入：Playwright 的 delay 是“每个字符”的延时（毫秒）
    per_char_delay = random.randint(55, 140)
    await page.keyboard.type(query, delay=per_char_delay)

    # 给联想/脚本一点反应时间
    await asyncio.sleep(random.uniform(0.2, 0.9))


async def _maybe_human_scroll(page) -> None:
    """轻量随机滚动，避免过于“机械”。"""
    # 保守：不是每次都滚
    if random.random() > 0.65:
        return
    try:
        dy = random.randint(250, 1200)
        await page.mouse.wheel(0, dy)
        await asyncio.sleep(random.uniform(0.4, 1.4))
        # 少量概率再滚回一点
        if random.random() < 0.25:
            await page.mouse.wheel(0, -random.randint(120, 500))
            await asyncio.sleep(random.uniform(0.2, 0.8))
    except Exception:
        return


async def _maybe_click_one_result(page) -> None:
    """偶尔点开一个自然结果再返回（默认很保守）。"""
    # 风险控制：点击结果更接近真人，但也更容易引入不可控跳转
    if random.random() > 0.15:
        return
    try:
        links = await page.query_selector_all("li.b_algo h2 a")
        if not links:
            return
        # 选前 1~3 条更像随手点
        candidates = links[: min(len(links), 3)]
        link = random.choice(candidates)
        try:
            await link.scroll_into_view_if_needed()
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.2, 0.8))

        await link.click(timeout=3000)
        await page.wait_for_load_state(DOMCONTENTLOADED, timeout=15000)
        await asyncio.sleep(random.uniform(2.0, 6.0))
        try:
            await page.go_back(wait_until=DOMCONTENTLOADED, timeout=15000)
        except Exception:
            # go_back 不行就不强求
            pass
    except Exception:
        return

async def _perform_bing_search_like_human(page, query: str) -> bool:
    """对齐 userscript：在搜索框输入并提交，而不是每次直接跳转 search?q=。"""
    try:
        # 确保在 Bing 上（有时搜索过程中会跳去别的域）
        if "bing.com" not in (page.url or ""):
            await page.goto("https://www.bing.com", wait_until=DOMCONTENTLOADED)

        await _maybe_accept_bing_dialogs(page)
        await _human_type_into_search_box(page, query)

        # userscript 本质上是 form submit / click go；这里用 Enter 提交
        await page.keyboard.press("Enter")
        await page.wait_for_load_state(DOMCONTENTLOADED, timeout=20000)
        return True
    except Exception:
        return False


def _get_exe_dir() -> Path:

    try:
        return Path(sys.argv[0]).resolve().parent
    except Exception:
        return Path.cwd()


def _get_bundle_dir() -> Path:
    """返回运行时资源目录。

    - 源码运行：等同于 main.py 所在目录
    - Nuitka onefile：等同于临时解压目录（__file__ 所在）
    """
    try:
        return Path(__file__).resolve().parent
    except Exception:
        return _get_exe_dir()


def _sanitize_filename(name: str) -> str:
    """把账号名转为 Windows 可用的文件名。"""
    # Windows 禁止字符: < > : " / \ | ? *
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in invalid else ch for ch in (name or ""))
    cleaned = cleaned.strip().strip(".")
    return cleaned or "未知账户"


# 定义User-Agent
USER_AGENTS = {
    "windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
}

REWARDS_API_URL = "https://rewards.bing.com/api/getuserinfo?type=1&X-Requested-With=XMLHttpRequest"

DASHBOARD_REFRESH_SECONDS = 30

SEARCH_CONFIG = {
    "points_per_search": 3,
    # PC 模式的随机等待区间（毫秒）
    "pc": {"min_delay_ms": 50_000, "max_delay_ms": 100_000},
}

SEARCH_KEYWORDS = [
    # 通用/信息
    "weather", "local weather", "today forecast", "news", "breaking news", "world news", "business news",
    "sports", "score", "highlights", "finance", "stock", "market news", "exchange rate", "inflation",
    "movies", "movie reviews", "box office", "tv series", "streaming", "anime", "documentary",
    "tech", "technology trends", "gadgets", "smartphone", "laptop", "windows tips", "edge browser",
    "food", "recipe", "easy dinner", "coffee", "tea", "baking", "dessert", "travel", "hotel", "flight",
    "music", "playlist", "podcast", "art", "museum", "photography", "design", "architecture",

    # 学习/知识
    "history", "world history", "ancient civilizations", "science", "biology", "chemistry", "physics",
    "math", "algebra", "geometry", "calculus", "statistics", "psychology", "philosophy", "economics",
    "nature", "wildlife", "plants", "space", "astronomy", "planets", "nasa", "space telescope",
    "geography", "maps", "time zone", "languages", "english learning", "grammar", "vocabulary",

    # 生活方式/健康
    "health", "fitness", "workout", "running", "yoga", "sleep", "nutrition", "meditation",
    "mental health", "stress relief", "habits", "productivity", "time management",

    # 运动/赛事
    "football", "soccer", "basketball", "tennis", "baseball", "formula 1", "olympics", "world cup",
    "nba", "nfl", "mlb", "uefa", "premier league", "champions league",

    # 汽车/交通
    "cars", "car review", "electric car", "tesla", "ev charging", "hybrid car", "motorcycle",
    "public transport", "traffic", "driving tips",

    # 游戏/娱乐
    "games", "video games", "pc gaming", "console", "esports", "game guide", "walkthrough",
    "minecraft", "fortnite", "league of legends", "valorant",

    # 图书/写作
    "books", "book list", "best novels", "nonfiction", "audiobooks", "writing", "story ideas",
    "poetry", "quotes",

    # 时尚/家居
    "fashion", "outfit ideas", "street style", "skincare", "hair care", "home decor",
    "interior design", "minimalism", "cleaning tips",

    # 美食/饮食扩展
    "healthy recipe", "meal prep", "air fryer", "soup", "pasta", "pizza", "salad", "bbq",

    # 科技开发/编程（会更随机一些）
    "python", "python async", "javascript", "typescript", "react", "nodejs", "docker",
    "linux", "powershell", "git", "api", "json", "automation", "web scraping",

    # 旅行目的地/文化
    "japan travel", "korea travel", "italy travel", "paris", "london", "new york",
    "street food", "culture", "festival",

    # AI / 热点科技
    "chatgpt", "gpt-4", "gemini ai", "copilot", "midjourney", "stable diffusion", "ai news",
    "machine learning", "deep learning", "computer vision", "nlp", "data science", "kaggle",

    # 经济/理财
    "personal finance", "side hustle", "mortgage rates", "housing market", "bond yield",
    "crypto price", "bitcoin", "ethereum", "stablecoin", "defi", "blockchain use case",

    # 职场/效率
    "resume tips", "interview questions", "career change", "remote work", "project management",
    "kanban", "scrum", "product roadmap", "meeting notes template", "time blocking",

    # 学习中文/本地化
    "中文学习", "拼音练习", "汉字笔顺", "唐诗三百首", "宋词", "成语故事", "古诗词解释",
    "中国历史朝代", "三国演义", "水浒传", "红楼梦",

    # 生活服务/实用
    "汇率换算", "火车票查询", "飞机票打折", "天气预报 明天", "菜谱 家常", "网购优惠",
    "手机评测", "路由器设置", "宽带测速",

    # 兴趣爱好
    "摄影技巧", "相机参数", "吉他和弦", "钢琴入门", "绘画教程", "手帐", "模型涂装",
    "露营装备", "徒步路线", "骑行训练",

    # 健康扩展
    "低脂餐", "增肌训练", "心率区间", "跑步配速", "马拉松训练计划", "颈椎放松",
    "护眼 tips", "喝水提醒",

    # 影视/剧集
    "netflix ranking", "disney plus", "hbo series", "kdrama", "cdrama", "imdb top",
    "影评 解读", "预告片",

    # 季节/节日
    "chinese new year", "lantern festival", "mid autumn", "dragon boat festival", "valentines day ideas",
    "halloween costume", "christmas markets", "new year resolution",

    # 本地化生活
    "上海天气", "北京地铁", "广州美食", "深圳科技园", "杭州旅游", "成都火锅", "西安古迹", "武汉樱花",

    # 电商/比价
    "京东优惠", "淘宝双11", "拼多多砍价", "黑五折扣", "prime day deals", "price comparison", "coupon code",

    # 硬件/DIY
    "组装电脑", "显卡天梯", "cpu 性能排行", "机械键盘", "3d 打印 入门", "树莓派 项目", "arduino 教程",

    # 开发框架/工具
    "fastapi", "django", "flask", "langchain", "pytorch", "tensorflow", "pandas tutorial", "numpy cheat sheet",

    # 商业/管理
    "swot analysis", "business model canvas", "okr 示例", "stakeholder map", "marketing plan", "seo checklist",
    "a/b testing", "customer journey",

    # 环保/可持续
    "solar panel cost", "ev tax credit", "carbon footprint", "recycling guide", "food waste tips", "sustainable fashion",

    # 家庭/育儿/教育
    "parenting tips", "儿童绘本", "早教 游戏", "儿童编程", "数学思维训练", "科学小实验", "亲子旅行",

    # 求职/证书/考试
    "leetcode", "system design", "coding interview", "behavioral questions", "简历模板", "公务员考试", "雅思口语",
    "托福词汇", "四六级真题", "CPA 考试", "PMP 备考",

    # 修理/排障
    "windows 蓝屏", "电脑重装", "excel 公式", "word 排版", "打印机故障", "网络延迟 高", "路由器拨号失败",

    # 旅行细分/攻略
    "北海道 自驾", "冲绳 潜水", "普吉岛 浮潜", "巴厘岛 蜜月", "瑞士 火车 pass", "冰岛 自驾环岛",
    "纽约 博物馆", "巴黎 卢浮宫 预约", "伦敦 大英博物馆",

    # 美食细分
    "手冲咖啡", "精品咖啡豆", "烘焙配方", "酵母面包", "寿司做法", "川菜家常", "粤菜 蒸鱼",
    "甜品食谱", "低糖甜品",

    # 运动细分
    "无氧训练计划", "HIIT 训练", "壶铃训练", "普拉提", "瑜伽体式", "羽毛球技巧", "网球发球",
    "游泳换气", "滑雪入门",

    # 小众兴趣/冷知识
    "天文观星", "流星雨时间", "星座故事", "冷知识 随机", "趣味数学", "谜语答案", "桌游推荐",
    "油管趋势", "reddit hot topics",

    # 便民/效率工具
    "markdown 教程", "正则表达式 示例", "表情包下载", "pdf 合并", "图片压缩 在线", "录屏 软件",
    "windows 快捷键", "截图工具",

    # 金融市场细分
    "美联储议息", "cpi 数据", "非农就业", "纳斯达克走势", "标普500 指数", "港股通", "a股行情",
    "黄金价格", "原油价格", "外汇汇率",

    # 城市/本地出行
    "共享单车 价格", "打车券", "停车收费", "新能源 绿牌 政策", "限行 查询",

    # 购物指南/测评
    "显示器 推荐", "蓝牙耳机 评测", "降噪耳机 对比", "行李箱 20寸", "旅行背包 30L",
    "机械键盘 热插拔", "人体工学 椅子", "颈枕 评测",

    # 效率与组织
    "gtd 方法", "bullet journal", "第二大脑 笔记", "obsidian 模板", "notion dashboard",

    # 编程工具链
    "vscode shortcuts", "git rebase", "git stash 用法", "docker compose 示例", "kubernetes 基础",
    "helm chart", "ci cd pipeline", "github actions",

    # 设计/创意
    "color palette generator", "font pairing", "icon pack free", "figma tutorial", "ui kit", "logo 设计 灵感",

    # 语言学习细分
    "西语基础", "法语入门", "日语五十音", "韩语发音", "德语词汇", "西班牙语听力", "英语口音练习",

    # 生活小技巧/安全
    "房屋除湿", "驱蚊 方法", "急救常识", "心肺复苏 cpr", "家庭应急包", "网络诈骗 识别",

    # 宠物/园艺
    "猫咪驱虫", "狗粮 成分", "猫砂 选择", "多肉养护", "阳台种菜", "花卉浇水 频率",

    # 文化/艺术扩展
    "国画 入门", "书法 笔画", "古典音乐 曲目", "钢琴名曲", "艺术展 资讯", "摄影后期 教程",
]


def generate_random_query() -> str:
    """生成随机搜索词（关键词 + 随机串）。"""
    keyword = random.choice(SEARCH_KEYWORDS)
    random_string = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=8))
    return f"{keyword} {random_string}"


def prepare_cookies_for_playwright(cookies: list) -> list:
    """清理/规范化 Cookie，避免 Playwright add_cookies / storage_state 注入失败。

    Playwright 约束：cookie 只能是 url，或 domain+path，二者不能混用。
    """
    prepared: list[dict] = []
    for raw in cookies or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        item = {k: v for k, v in raw.items() if k not in {"partitionKey", "_crHasCrossSiteAncestor"}}
        name = str(item.get("name") or "")
        same_site = item.get("sameSite")
        if same_site not in ("Strict", "Lax", "None"):
            item.pop("sameSite", None)

        # __Host- / __Secure-：优先用 url（不能同时带 path/domain）
        if name.startswith("__Host-") or name.startswith("__Secure-"):
            domain = str(item.pop("domain", "") or "").lstrip(".")
            item.pop("path", None)
            item["secure"] = True
            item["url"] = f"https://{domain}/" if domain else "https://login.live.com/"
        elif item.get("url"):
            # 有 url 时去掉 domain/path，避免 “either url or path” 报错
            item.pop("domain", None)
            item.pop("path", None)
        elif item.get("domain"):
            if not item.get("path"):
                item["path"] = "/"
        else:
            item["url"] = "https://rewards.bing.com/"

        # Playwright: session cookie 用 -1；非法值直接去掉让其成为 session
        exp = item.get("expires")
        if exp is not None:
            try:
                exp_f = float(exp)
                if exp_f > 0 and exp_f < 10_000_000_000:
                    item["expires"] = exp_f
                elif exp_f <= 0:
                    item["expires"] = -1
                else:
                    item.pop("expires", None)
            except Exception:
                item.pop("expires", None)

        prepared.append(item)
    return prepared


def load_auth_state(path: Path) -> dict:
    """读取账号会话文件，兼容旧版纯 cookie 数组与 Playwright storage_state。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {"cookies": prepare_cookies_for_playwright(data), "origins": []}
    if isinstance(data, dict):
        cookies = data.get("cookies") or []
        origins = data.get("origins") or []
        return {
            "cookies": prepare_cookies_for_playwright(cookies),
            "origins": origins,
        }
    raise ValueError(f"无法识别的会话文件格式: {path}")


def read_account_meta(path: Path) -> dict:
    """读取会话文件中的账号元数据（若有）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict) and isinstance(data.get("_account"), dict):
        return dict(data["_account"])
    return {}


def cookie_identity_key(cookies: list) -> Optional[str]:
    """从 Cookie 生成稳定账号指纹，用于去重。"""
    by_name: dict[str, str] = {}
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if isinstance(name, str) and isinstance(value, str) and value:
            by_name[name] = value

    anon = by_name.get("ANON") or ""
    m = re.search(r"(?:^|&)A=([^&]+)", anon)
    if m and m.group(1):
        return f"anon:{m.group(1)}"

    for key in ("MSPAuth", "RPSSecAuth", "__Host-MSAAUTH"):
        val = by_name.get(key) or ""
        if val and val.lower() != "disabled" and len(val) >= 12:
            return f"{key.lower()}:{val[:48]}"
    return None


def account_identity_from_file(path: Path) -> Optional[str]:
    """读取已有会话文件的稳定账号指纹。"""
    meta = read_account_meta(path)
    key = meta.get("key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    try:
        state = load_auth_state(path)
        return cookie_identity_key(state.get("cookies") or [])
    except Exception:
        return None


def find_account_files_by_identity(cookies_dir: Path, identity_key: str) -> list[Path]:
    """找出同一账号指纹的所有会话文件。"""
    if not identity_key:
        return []
    matches: list[Path] = []
    for p in sorted(Path(cookies_dir).glob("*.json")):
        try:
            if account_identity_from_file(p) == identity_key:
                matches.append(p)
        except Exception:
            continue
    return matches


def dedupe_account_files(cookies_dir: Path) -> list[tuple[Path, list[Path]]]:
    """扫描目录，按账号指纹去重：保留最新文件，删除其余副本。"""
    cookies_dir = Path(cookies_dir)
    groups: dict[str, list[Path]] = {}
    for p in sorted(cookies_dir.glob("*.json")):
        key = account_identity_from_file(p)
        if not key:
            continue
        groups.setdefault(key, []).append(p)

    results: list[tuple[Path, list[Path]]] = []
    for key, files in groups.items():
        if len(files) <= 1:
            continue
        files_sorted = sorted(files, key=lambda x: x.stat().st_mtime, reverse=True)
        keep = files_sorted[0]
        removed: list[Path] = []
        for p in files_sorted[1:]:
            try:
                p.unlink()
                removed.append(p)
            except Exception:
                continue
        if removed:
            results.append((keep, removed))
    return results


async def apply_auth_state(context, path: Path) -> None:
    """把会话文件注入到已有 BrowserContext。"""
    state = load_auth_state(path)
    cookies = state.get("cookies") or []
    if cookies:
        await context.add_cookies(cookies)


async def save_auth_state(context, path: Path, account: dict | None = None) -> None:
    """保存完整 storage_state（cookies + origins）。

    落盘保持 Playwright 原始格式，并可选写入 `_account` 元数据；加载时会忽略该字段。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = await context.storage_state()
    if account:
        state["_account"] = {
            "name": account.get("name"),
            "key": account.get("key"),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


async def resolve_account_identity(page, context) -> dict:
    """解析展示名 + 稳定去重 key。

    优先邮箱；其次页面名；再退到 Cookie 指纹。不再默认用纯时间戳做主名，避免同账号多条。
    """
    display_name = None
    email = None

    # 1) 页面可见邮箱/文案
    try:
        scraped = await page.evaluate(
            """() => {
              const body = (document.body && document.body.innerText) || '';
              const html = document.documentElement ? document.documentElement.innerHTML : '';
              const emailRe = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/ig;
              const emails = [];
              for (const src of [body, html]) {
                const found = src.match(emailRe) || [];
                for (const e of found) {
                  const low = e.toLowerCase();
                  if (low.endsWith('.png') || low.endsWith('.jpg') || low.endsWith('.js')) continue;
                  if (!emails.includes(low)) emails.push(low);
                }
              }
              return { emails, title: document.title || '' };
            }"""
        )
        emails = scraped.get("emails") or []
        if emails:
            email = str(emails[0]).strip()
            display_name = email
    except Exception:
        pass

    # 2) 旧版 Me Control
    if not display_name:
        for sel in (
            "#mectrl_currentAccount_secondary",
            "#mectrl_currentAccount_primary",
            "#mectrl_name",
        ):
            try:
                el = await page.query_selector(sel)
                if not el:
                    continue
                text = (await el.text_content() or "").strip()
                if text:
                    display_name = text
                    if "@" in text:
                        email = text
                    break
            except Exception:
                continue

    # 3) Cookie 指纹（稳定去重）
    try:
        cookies = await context.cookies()
    except Exception:
        cookies = []
    identity_key = None
    if email:
        identity_key = f"email:{email.lower()}"
    if not identity_key:
        identity_key = cookie_identity_key(cookies)

    if not display_name and identity_key:
        if identity_key.startswith("anon:"):
            display_name = f"user_{identity_key.split(':', 1)[1][:8]}"
        else:
            display_name = f"user_{identity_key.split(':', 1)[-1][:8]}"

    if not display_name:
        display_name = f"account_{time.strftime('%Y%m%d_%H%M%S')}"
    if not identity_key:
        # 最后兜底：用展示名，至少同名会覆盖
        identity_key = f"name:{display_name.lower()}"

    return {
        "name": _sanitize_filename(display_name),
        "key": identity_key,
        "email": email,
    }


async def save_account_session(context, cookies_dir: Path, account: dict) -> tuple[Path, list[Path]]:
    """按账号指纹去重保存：覆盖已有文件，并删除同账号的其它副本。"""
    cookies_dir = Path(cookies_dir)
    cookies_dir.mkdir(parents=True, exist_ok=True)

    name = _sanitize_filename(account.get("name") or "account")
    key = account.get("key") or f"name:{name.lower()}"
    preferred = cookies_dir / f"{name}.json"

    matches = find_account_files_by_identity(cookies_dir, key)
    # 也把同名文件算进去（即使还没有写入 key）
    if preferred.exists() and preferred not in matches:
        matches.append(preferred)

    target = preferred
    if matches:
        # 优先覆盖已有同名；否则覆盖最早那个，再改名为新展示名
        same_name = [p for p in matches if p.name.lower() == preferred.name.lower()]
        target = same_name[0] if same_name else matches[0]

    # 若目标文件名不是期望展示名，且期望名未被其它账号占用，则改用期望名
    if target.resolve() != preferred.resolve():
        other_key = account_identity_from_file(preferred) if preferred.exists() else None
        if (not preferred.exists()) or other_key in (None, key):
            target = preferred

    await save_auth_state(context, target, account={"name": name, "key": key})

    removed: list[Path] = []
    for p in find_account_files_by_identity(cookies_dir, key):
        if p.resolve() == target.resolve():
            continue
        try:
            p.unlink()
            removed.append(p)
        except Exception:
            continue

    # 同名但指纹读不到的旧时间戳文件：若内容 identity 对得上已处理；
    # 额外清理：保存后若 preferred 之外仍有同 key，上面已删。
    return target, removed



_earn_progress_cache: dict = {"ts": 0.0, "data": None, "ver": 4}
_EARN_PROGRESS_TTL_SEC = 45.0
_EARN_PROGRESS_VER = 4


def _normalize_streaks(raw_streaks) -> list:
    out = []
    for s in raw_streaks or []:
        if not isinstance(s, dict):
            continue
        out.append(
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "title": s.get("title"),
                "day": s.get("day"),
                "day_total": s.get("day_total", s.get("dayTotal")),
                "goal_cur": s.get("goal_cur", s.get("goalCur")),
                "goal_max": s.get("goal_max", s.get("goalMax")),
            }
        )
    return out


def _normalize_stamps(raw_stamps) -> dict:
    if not isinstance(raw_stamps, dict):
        return {"current": None, "total": None, "reward": None}
    return {
        "current": raw_stamps.get("current"),
        "total": raw_stamps.get("total"),
        "reward": raw_stamps.get("reward"),
    }


async def scrape_earn_page_progress(page, *, force: bool = False) -> dict:
    """从赚取页抓连续打卡 / 印章奖励 / 日常任务。短缓存避免高频刷新反复跳转。"""
    now = time.time()
    cached = _earn_progress_cache.get("data")
    if (
        not force
        and cached is not None
        and _earn_progress_cache.get("ver") == _EARN_PROGRESS_VER
        and (now - float(_earn_progress_cache.get("ts") or 0)) < _EARN_PROGRESS_TTL_SEC
    ):
        return cached

    result = {
        "pending": 0,
        "done": 0,
        "pointsPending": 0,
        "streaks": [],
        "stamps": {"current": None, "total": None, "reward": None},
    }
    try:
        cur = ""
        try:
            cur = page.url or ""
        except Exception:
            cur = ""
        if "rewards.bing.com/earn" not in cur:
            await page.goto("https://rewards.bing.com/earn", wait_until=DOMCONTENTLOADED, timeout=45000)
            await page.wait_for_timeout(1800)
        else:
            await page.wait_for_timeout(400)

        earn_extra = await page.evaluate(
            """() => {
              const norm = (s) => (s || '').trim().replace(/\\s+/g, ' ');
              const yOf = (el) => {
                try { return el.getBoundingClientRect().top + window.scrollY; }
                catch (e) { return 0; }
              };
              const body = norm(document.body && document.body.innerText);

              let heading = null;
              for (const el of document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,span,p')) {
                const own = norm(el.innerText || '');
                if (own === '日常任务' || (own.startsWith('日常任务') && own.length <= 12)) {
                  heading = el; break;
                }
              }
              let pending = 0, done = 0, pointsPending = 0;
              if (heading) {
                const headingY = yOf(heading);
                const nextRe = /^(每日任务|每日活动|更多活动|探索|游戏|必应搜索|邀请|连续打卡|Redeem|More activities|Daily set)/i;
                let nextY = Infinity;
                for (const el of document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,span,p')) {
                  const own = norm(el.innerText || '');
                  if (!own || own.length > 28 || own.startsWith('日常任务')) continue;
                  if (!nextRe.test(own)) continue;
                  const y = yOf(el);
                  if (y > headingY + 20 && y < nextY) nextY = y;
                }
                const seen = new Set();
                for (const el of document.querySelectorAll('a[href], button, [role="link"], [role="button"]')) {
                  const y = yOf(el);
                  if (y < headingY - 4 || y >= nextY) continue;
                  let scope = el;
                  for (let i = 0; i < 5; i++) {
                    const p = scope.parentElement;
                    if (!p) break;
                    const pt = norm(p.innerText || '');
                    if (pt.length > 0 && pt.length <= 220 && !pt.startsWith('日常任务')) scope = p;
                    else break;
                  }
                  const nearby = norm(scope.innerText || '').slice(0, 220);
                  const key = nearby.slice(0, 80);
                  if (!key || seen.has(key)) continue;
                  seen.add(key);
                  const m = nearby.match(/\\+\\s*(\\d+)/);
                  if (m) { pending += 1; pointsPending += Number(m[1]) || 0; }
                  else if (/(已完成|completed|已领取)/i.test(nearby)) done += 1;
                }
              }


              // 连续打卡：在分区内按「第x天」卡片分类（Edge 用「分钟:」识别）
              const streaks = [];
              const seenIds = new Set();
              const classify = (t) => {
                if (/分钟\\s*[:：]/.test(t) || (/Edge/i.test(t) && /浏览器/.test(t)) || /Edge\\s*浏览器连续打卡/.test(t) || /Edge浏览器连续打卡/.test(t)) {
                  return { id: 'edge', name: 'Edge', goalRe: /分钟\\s*[:：]\\s*(\\d+)\\s*\\/\\s*(\\d+)/ };
                }
                if (/签到\\s*[:：]/.test(t) || /必应应用连续打卡/.test(t)) {
                  return { id: 'bing_app', name: '应用', goalRe: /签到\\s*[:：]\\s*(\\d+)\\s*\\/\\s*(\\d+)/ };
                }
                if (/每日连续打卡活动/.test(t) || (/活动\\s*[:：]/.test(t) && /连续打卡/.test(t))) {
                  return { id: 'daily_activity', name: '活动', goalRe: /活动\\s*[:：]\\s*(\\d+)\\s*\\/\\s*(\\d+)/ };
                }
                if (/必应搜索连续打卡/.test(t) || (/搜索\\s*[:：]/.test(t) && /连续打卡/.test(t))) {
                  return { id: 'bing_search', name: '搜索', goalRe: /搜索\\s*[:：]\\s*(\\d+)\\s*\\/\\s*(\\d+)/ };
                }
                return null;
              };

              const streakCandidates = [];
              for (const el of document.querySelectorAll('a, div, section, article, li, button')) {
                const txt = norm(el.innerText || '');
                if (!/第\\s*\\d+\\s*天/.test(txt)) continue;
                if (txt.length < 10 || txt.length > 420) continue;
                if (!/(搜索\\s*[:：]|活动\\s*[:：]|分钟\\s*[:：]|签到\\s*[:：]|连续打卡|Edge)/i.test(txt)) continue;
                const kind = classify(txt);
                if (!kind) continue;
                streakCandidates.push({ el, txt, kind, len: txt.length });
              }
              // 同 id 取最短文案（更接近单卡）
              streakCandidates.sort((a, b) => a.len - b.len);
              for (const c of streakCandidates) {
                if (seenIds.has(c.kind.id)) continue;
                const dayM = c.txt.match(/第\\s*(\\d+)\\s*天\\s*\\/\\s*共\\s*(\\d+)\\s*天/);
                const goalM = c.txt.match(c.kind.goalRe);
                seenIds.add(c.kind.id);
                streaks.push({
                  id: c.kind.id,
                  name: c.kind.name,
                  title: c.kind.name,
                  day: dayM ? Number(dayM[1]) : null,
                  dayTotal: dayM ? Number(dayM[2]) : null,
                  goalCur: goalM ? Number(goalM[1]) : null,
                  goalMax: goalM ? Number(goalM[2]) : null,
                });
              }

              // 正文兜底：Edge 标题可能带特殊空格
              if (!seenIds.has('edge')) {
                const edgeRe = /Edge\\s*浏览器连续打卡[\\s\\S]{0,280}?第\\s*(\\d+)\\s*天\\s*\\/\\s*共\\s*(\\d+)\\s*天[\\s\\S]{0,120}?分钟\\s*[:：]\\s*(\\d+)\\s*\\/\\s*(\\d+)/i;
                const em = body.match(edgeRe);
                if (em) {
                  streaks.push({
                    id: 'edge', name: 'Edge', title: 'Edge',
                    day: Number(em[1]), dayTotal: Number(em[2]),
                    goalCur: Number(em[3]), goalMax: Number(em[4]),
                  });
                  seenIds.add('edge');
                } else {
                  const em2 = body.match(/分钟\\s*[:：]\\s*(\\d+)\\s*\\/\\s*(\\d+)/);
                  const em3 = body.match(/Edge[\\s\\S]{0,200}?第\\s*(\\d+)\\s*天\\s*\\/\\s*共\\s*(\\d+)\\s*天/i);
                  if (em2 || em3) {
                    streaks.push({
                      id: 'edge', name: 'Edge', title: 'Edge',
                      day: em3 ? Number(em3[1]) : null,
                      dayTotal: em3 ? Number(em3[2]) : null,
                      goalCur: em2 ? Number(em2[1]) : null,
                      goalMax: em2 ? Number(em2[2]) : null,
                    });
                    seenIds.add('edge');
                  }
                }
              }

              // 印章：奖励/总数用文案；已得数量优先数圆形槽位，再用 HTML 字段兜底
              const stamps = { current: null, total: null, reward: null };
              const totalM = body.match(/赚取\\s*(\\d+)\\s*个印章/);
              if (totalM) stamps.total = Number(totalM[1]);
              let stampCard = null;
              let stampCardScore = 1e9;
              for (const el of document.querySelectorAll('a, div, section, article')) {
                const t = norm(el.innerText || '');
                if (!/个印章/.test(t) || !/奖励/.test(t)) continue;
                if (t.length < 8 || t.length > 420) continue;
                if (t.length < stampCardScore) {
                  stampCard = el;
                  stampCardScore = t.length;
                  const rm = t.match(/([\\d,]+)\\s*奖励/);
                  if (rm) stamps.reward = Number(String(rm[1]).replace(/,/g, ''));
                  if (!stamps.total) {
                    const tm = t.match(/赚取\\s*(\\d+)\\s*个印章/);
                    if (tm) stamps.total = Number(tm[1]);
                  }
                }
              }
              // 文案节点可能不含圆点，向上扩到能包住印章网格的容器
              if (stampCard) {
                let node = stampCard;
                for (let i = 0; i < 5 && node.parentElement; i++) {
                  const parent = node.parentElement;
                  const pt = norm(parent.innerText || '');
                  if (pt.length > 700) break;
                  const svgs = parent.querySelectorAll('svg, img').length;
                  const boxes = parent.querySelectorAll('div, span, button').length;
                  if (svgs >= 8 || boxes >= 20) {
                    stampCard = parent;
                  }
                  node = parent;
                }
              }

              // 印章已得数：优先 progressbar / JSON；再按「两行×约6个」星标网格计黑圆
              const readLum = (el) => {
                let node = el;
                for (let i = 0; i < 4 && node; i++) {
                  const bg = (getComputedStyle(node).backgroundColor || '').toLowerCase();
                  const m = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?/);
                  if (m) {
                    const a = m[4] != null ? Number(m[4]) : 1;
                    if (a >= 0.08) {
                      return (Number(m[1]) + Number(m[2]) + Number(m[3])) / 3;
                    }
                  }
                  node = node.parentElement;
                }
                return null;
              };

              if (stampCard) {
                const bar = stampCard.querySelector('[role="progressbar"]');
                if (bar) {
                  const now = Number(bar.getAttribute('aria-valuenow'));
                  const max = Number(bar.getAttribute('aria-valuemax'));
                  if (Number.isFinite(now)) stamps.current = now;
                  if (Number.isFinite(max) && max > 0) stamps.total = max;
                }
              }

              if (stamps.current == null) {
                const html = document.documentElement.innerHTML || '';
                const patterns = [
                  /"earnedStamps"\\s*:\\s*(\\d+)/i,
                  /"stampsEarned"\\s*:\\s*(\\d+)/i,
                  /"completedStamps"\\s*:\\s*(\\d+)/i,
                  /"stampsCollected"\\s*:\\s*(\\d+)/i,
                  /"stampCount"\\s*:\\s*(\\d+)/i,
                ];
                for (const re of patterns) {
                  const m = html.match(re);
                  if (!m) continue;
                  const n = Number(m[1]);
                  if (!Number.isFinite(n)) continue;
                  if (stamps.total && (n < 0 || n > stamps.total)) continue;
                  stamps.current = n;
                  break;
                }
              }

              // 核心：只数印章卡里「黑色实心圆」（已得），灰框空槽不计入
              if (stamps.current == null && stampCard) {
                const expect = stamps.total || 12;
                const seenPos = new Set();
                const darkDots = [];
                const allDots = [];
                for (const el of stampCard.querySelectorAll('div, span, button, i, svg, img')) {
                  const r = el.getBoundingClientRect();
                  if (r.width < 16 || r.width > 42 || r.height < 16 || r.height > 42) continue;
                  if (Math.abs(r.width - r.height) > 7) continue;
                  let nested = false;
                  for (const ch of Array.from(el.children || [])) {
                    const cr = ch.getBoundingClientRect();
                    if (cr.width >= 16 && cr.width <= 42 && Math.abs(cr.width - cr.height) <= 7) {
                      nested = true; break;
                    }
                  }
                  if (nested && el.tagName !== 'SVG' && el.tagName !== 'IMG') continue;
                  const key = Math.round(r.left / 2) * 2 + ':' + Math.round(r.top / 2) * 2;
                  if (seenPos.has(key)) continue;
                  seenPos.add(key);
                  const lum = readLum(el);
                  if (lum == null) continue;
                  const item = { lum, x: r.left, y: r.top, w: r.width };
                  allDots.push(item);
                  // 页面上已得印章是近黑色圆
                  if (lum <= 95) darkDots.push(item);
                }

                // 优先：黑圆数量（通常正好是已得印章）
                if (darkDots.length >= 1 && darkDots.length <= expect) {
                  stamps.current = darkDots.length;
                } else if (allDots.length >= 8) {
                  // 按两行网格，取每行最多 6 个后数黑圆
                  const ys = allDots.map((d) => d.y).sort((a, b) => a - b);
                  let splitY = ys[Math.floor(ys.length / 2)];
                  let maxGap = 0;
                  for (let i = 1; i < ys.length; i++) {
                    const gap = ys[i] - ys[i - 1];
                    if (gap > maxGap) { maxGap = gap; splitY = (ys[i] + ys[i - 1]) / 2; }
                  }
                  const rowOf = (row) => row.sort((a, b) => a.x - b.x).slice(0, 6);
                  const r1 = rowOf(allDots.filter((d) => d.y < splitY));
                  const r2 = rowOf(allDots.filter((d) => d.y >= splitY));
                  const grid = r1.concat(r2);
                  const filled = grid.filter((d) => d.lum <= 95).length;
                  if (filled >= 1 && filled <= expect) stamps.current = filled;
                }
                if (!stamps.total) stamps.total = expect;
              }

              return { pending, done, pointsPending, streaks, stamps };
            }"""
        ) or {}
        result = {
            "pending": int(earn_extra.get("pending") or 0),
            "done": int(earn_extra.get("done") or 0),
            "pointsPending": int(earn_extra.get("pointsPending") or 0),
            "streaks": _normalize_streaks(earn_extra.get("streaks")),
            "stamps": _normalize_stamps(earn_extra.get("stamps")),
        }
        _earn_progress_cache["ts"] = now
        _earn_progress_cache["data"] = result
        _earn_progress_cache["ver"] = _EARN_PROGRESS_VER
    except Exception:
        if cached is not None and _earn_progress_cache.get("ver") == _EARN_PROGRESS_VER:
            return cached
    return result


def _merge_earn_progress_into_userinfo(userinfo: dict, earn: dict) -> dict:
    if not isinstance(userinfo, dict):
        userinfo = {}
    progress = userinfo.get("_progress")
    if not isinstance(progress, dict):
        progress = {}
        userinfo["_progress"] = progress

    streaks = earn.get("streaks") or []
    stamps = earn.get("stamps") or {}
    if streaks:
        progress["streaks"] = streaks
    if isinstance(stamps, dict) and (
        stamps.get("current") is not None
        or stamps.get("total") is not None
        or stamps.get("reward") is not None
    ):
        progress["stamps"] = stamps

    routine_pending = int(earn.get("pending") or 0)
    routine_done = int(earn.get("done") or 0)
    if "routine_done" not in progress:
        progress["routine_done"] = routine_done
        progress["routine_total"] = routine_pending + routine_done
    return userinfo


async def _attach_earn_progress(page, userinfo: dict) -> dict:
    if page is None:
        return userinfo
    try:
        earn = await scrape_earn_page_progress(page, force=False)
        streaks = earn.get("streaks") or []
        stamps = earn.get("stamps") or {}
        need_retry = False
        if not any(s.get("id") == "edge" and s.get("day") is not None for s in streaks):
            need_retry = True
        if stamps.get("current") is None and stamps.get("total"):
            need_retry = True
        if need_retry:
            earn = await scrape_earn_page_progress(page, force=True)
        return _merge_earn_progress_into_userinfo(userinfo, earn)
    except Exception:
        return userinfo


async def fetch_rewards_userinfo(context, *, timeout_ms: int = 15000, page=None):
    """从 Rewards API 拉取用户信息（使用当前 context 的 Cookie 身份）。

    优先走页面内 fetch（带上浏览器真实 Cookie，含分区 Cookie），
    避免 context.request 出现“页面已登录但 API 401”的情况。
    """
    if page is not None:
        try:
            cur = ""
            try:
                cur = page.url or ""
            except Exception:
                cur = ""
            if "rewards.bing.com" not in cur:
                await page.goto(
                    "https://rewards.bing.com/",
                    wait_until=DOMCONTENTLOADED,
                    timeout=min(timeout_ms, 60000),
                )
                await page.wait_for_timeout(800)
        except Exception:
            pass

        page_err = None
        try:
            data = await page.evaluate(
                """async () => {
                    const ts = Date.now();
                    const url = `https://rewards.bing.com/api/getuserinfo?type=1&X-Requested-With=XMLHttpRequest&_=${ts}`;
                    const resp = await fetch(url, {
                        method: 'GET',
                        credentials: 'include',
                        headers: {
                            'X-Requested-With': 'XMLHttpRequest',
                            'Accept': 'application/json, text/plain, */*',
                        },
                    });
                    if (!resp.ok) {
                        throw new Error(`${resp.status} ${resp.statusText}`);
                    }
                    return await resp.json();
                }"""
            )
            if isinstance(data, dict):
                return await _attach_earn_progress(page, data)
            raise RuntimeError("Rewards API 返回了非 JSON 对象")
        except Exception as e:
            # 回退到 context.request
            page_err = e
    else:
        page_err = None

    timestamp = int(time.time() * 1000)
    url = f"{REWARDS_API_URL}&_={timestamp}"

    try:
        resp = await context.request.get(
            url,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://rewards.bing.com/",
                "Accept": "application/json, text/plain, */*",
            },
            timeout=timeout_ms,
        )
    except Exception as e:
        detail = f"{e}"
        if page_err is not None:
            detail = f"page_fetch={page_err}; request={e}"
        raise RuntimeError(f"Rewards API 请求超时/失败: {detail}") from e

    if not resp.ok:
        detail = f"{resp.status} {resp.status_text}"
        if page_err is not None:
            detail = f"page_fetch={page_err}; request={detail}"
        # 新版 Rewards（Next.js）常对 getuserinfo 返回 401，但页面本身已登录；
        # 有 page 时回退到 DOM/RSC 抓取，避免面板空白。
        if page is not None and resp.status in (401, 403, 404):
            try:
                return await scrape_rewards_userinfo_from_page(page)
            except Exception as scrape_err:
                raise RuntimeError(
                    f"Rewards API 请求失败: {detail}; scrape={scrape_err}"
                ) from scrape_err
        raise RuntimeError(f"Rewards API 请求失败: {detail}")
    data = await resp.json()
    if isinstance(data, dict):
        return await _attach_earn_progress(page, data)
    return data


def _parse_balance_level_from_html(html: str) -> tuple[int | None, int | None]:
    if not html:
        return None, None
    m = re.search(r'\\*"balance\\*"\s*:\s*(\d+)\s*,\s*\\*"level\\*"\s*:\s*(\d+)', html)
    if not m:
        m = re.search(r'"balance"\s*:\s*(\d+)\s*,\s*"level"\s*:\s*(\d+)', html)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


async def scrape_rewards_userinfo_from_page(page) -> dict:
    """从新版 Rewards 页面抓取积分信息，构造与旧 getuserinfo 近似的结构。"""
    # 统一落到 dashboard，并重试等待前端渲染出积分
    last_url = ""
    balance = None
    level_num = None
    scraped = {
        "pointsText": None,
        "levelText": None,
        "searchDone": None,
        "searchTotal": None,
        "title": "",
        "url": "",
    }

    for attempt in range(5):
        try:
            cur = page.url or ""
        except Exception:
            cur = ""
        if "/dashboard" not in cur or "rewards.bing.com" not in cur:
            await page.goto(
                "https://rewards.bing.com/dashboard",
                wait_until=DOMCONTENTLOADED,
                timeout=60000,
            )
        elif attempt > 0:
            try:
                await page.reload(wait_until=DOMCONTENTLOADED, timeout=60000)
            except Exception:
                pass

        await page.wait_for_timeout(1000 + attempt * 700)

        html = ""
        try:
            html = await page.content()
        except Exception:
            html = ""
        balance, level_num = _parse_balance_level_from_html(html)

        scraped = await page.evaluate(
            """() => {
              const body = (document.body && document.body.innerText) || '';
              const num = (s) => (s == null ? null : Number(String(s).replace(/,/g, '')));
              const norm = (s) => (s || '').trim().replace(/\\s+/g, ' ');
              const yOf = (el) => {
                try { return el.getBoundingClientRect().top + window.scrollY; }
                catch (e) { return 0; }
              };
              // 统计某分区内：带 +分 的待做卡 / 已完成卡
              const countSection = (titles, nextReSource) => {
                const titleSet = (titles || []).map(norm).filter(Boolean);
                let heading = null;
                for (const el of document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,span,p')) {
                  const own = norm(el.innerText || '');
                  if (!own || own.length > 24) continue;
                  if (titleSet.some((t) => own === t || (own.startsWith(t) && own.length <= t.length + 6))) {
                    heading = el;
                    break;
                  }
                }
                if (!heading) return { pending: 0, done: 0, pointsPending: 0 };
                const headingY = yOf(heading);
                let nextRe;
                try { nextRe = new RegExp(nextReSource, 'i'); } catch (e) { nextRe = /^(更多|探索|游戏)/i; }
                let nextY = Infinity;
                for (const el of document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,span,p')) {
                  const own = norm(el.innerText || '');
                  if (!own || own.length > 28) continue;
                  if (titleSet.some((t) => own === t || own.startsWith(t))) continue;
                  if (!nextRe.test(own)) continue;
                  const y = yOf(el);
                  if (y > headingY + 20 && y < nextY) nextY = y;
                }
                const seen = new Set();
                let pending = 0, done = 0, pointsPending = 0;
                for (const el of document.querySelectorAll('a[href], button, [role="link"], [role="button"]')) {
                  const y = yOf(el);
                  if (y < headingY - 4 || y >= nextY) continue;
                  let scope = el;
                  for (let i = 0; i < 5; i++) {
                    const p = scope.parentElement;
                    if (!p) break;
                    const pt = norm(p.innerText || '');
                    if (pt.length > 0 && pt.length <= 220 && !titleSet.some((t) => pt.startsWith(t))) scope = p;
                    else break;
                  }
                  const nearby = norm(scope.innerText || '').slice(0, 220);
                  if (!nearby || nearby.length < 4) continue;
                  const key = nearby.slice(0, 80);
                  if (seen.has(key)) continue;
                  seen.add(key);
                  const m = nearby.match(/\\+\\s*(\\d+)/);
                  if (m) {
                    pending += 1;
                    pointsPending += Number(m[1]) || 0;
                  } else if (/(已完成|completed|已领取)/i.test(nearby)) {
                    done += 1;
                  }
                }
                return { pending, done, pointsPending };
              };

              const pointsMatch =
                body.match(/可用积分[^\\d]{0,40}([\\d,]{2,})/) ||
                body.match(/([\\d,]{2,})\\s*兑换/);
              const levelMatch = body.match(/金牌会员|银牌会员|会员/);
              const searchBlock = body.match(/必应[\\s\\S]{0,80}?搜索:\\s*(\\d+)\\s*\\/\\s*(\\d+)[\\s\\S]{0,40}?(\\d{1,4})/);
              const searchMatch = searchBlock || body.match(/搜索:\\s*(\\d+)\\s*\\/\\s*(\\d+)/);
              const activityBlock = body.match(/每日活动[\\s\\S]{0,80}?活动:\\s*(\\d+)\\s*\\/\\s*(\\d+)[\\s\\S]{0,40}?(\\d{1,4})/);
              const activityMatch = activityBlock || body.match(/活动:\\s*(\\d+)\\s*\\/\\s*(\\d+)/);
              const todayPointsMatch = body.match(/今日积分\\s*([\\d,]+)/);
              const claimableMatch = body.match(/可领取\\s*([\\d,]+)/);
              // 首页「每日任务」
              const dailySet = countSection(
                ['每日任务', 'Daily set', 'Daily Set', 'Daily tasks'],
                '^(日常任务|每日活动|更多活动|探索|游戏|必应搜索|邀请|More activities|Earn|Redeem)'
              );
              return {
                pointsText: pointsMatch ? pointsMatch[1].replace(/,/g, '') : null,
                levelText: levelMatch ? levelMatch[0] : null,
                searchDone: searchMatch ? Number(searchMatch[1]) : null,
                searchTotal: searchMatch ? Number(searchMatch[2]) : null,
                searchPoints: searchBlock ? num(searchBlock[3]) : null,
                activityDone: activityMatch ? Number(activityMatch[1]) : null,
                activityTotal: activityMatch ? Number(activityMatch[2]) : null,
                activityPoints: activityBlock ? num(activityBlock[3]) : null,
                dailySetPending: dailySet.pending,
                dailySetDone: dailySet.done,
                dailySetPointsPending: dailySet.pointsPending,
                todayPoints: todayPointsMatch ? num(todayPointsMatch[1]) : null,
                claimable: claimableMatch ? num(claimableMatch[1]) : null,
                title: document.title || '',
                url: location.href || '',
                hasRewardsTitle: /Microsoft Rewards|积分商城/.test(document.title || ''),
              };
            }"""
        )
        last_url = scraped.get("url") or cur

        if balance is None and scraped.get("pointsText"):
            try:
                balance = int(str(scraped["pointsText"]).replace(",", ""))
            except Exception:
                balance = None

        if balance is not None:
            break

    if balance is None:
        raise RuntimeError(f"页面已打开但未能解析可用积分 ({last_url or 'unknown'})")

    # 再扫赚取页：日常任务 + 连续打卡 + 印章
    earn_routine = {"pending": 0, "done": 0, "pointsPending": 0}
    earn_streaks: list = []
    earn_stamps = {"current": None, "total": None, "reward": None}
    try:
        earn_extra = await scrape_earn_page_progress(page, force=True)
        earn_routine = {
            "pending": int(earn_extra.get("pending") or 0),
            "done": int(earn_extra.get("done") or 0),
            "pointsPending": int(earn_extra.get("pointsPending") or 0),
        }
        earn_streaks = earn_extra.get("streaks") or []
        earn_stamps = earn_extra.get("stamps") or earn_stamps
    except Exception:
        pass

    level_name = scraped.get("levelText") or "未知"
    if not scraped.get("levelText") and level_num is not None:
        level_name = {1: "会员", 2: "银牌会员", 3: "金牌会员"}.get(level_num, f"Level{level_num}")

    search_done = scraped.get("searchDone") if isinstance(scraped.get("searchDone"), int) else 0
    search_total = scraped.get("searchTotal") if isinstance(scraped.get("searchTotal"), int) else 0
    activity_done = scraped.get("activityDone") if isinstance(scraped.get("activityDone"), int) else 0
    activity_total = scraped.get("activityTotal") if isinstance(scraped.get("activityTotal"), int) else 0

    daily_set_pending = int(scraped.get("dailySetPending") or 0)
    daily_set_done = int(scraped.get("dailySetDone") or 0)
    daily_set_points_pending = int(scraped.get("dailySetPointsPending") or 0)
    daily_set_total = daily_set_pending + daily_set_done

    routine_pending = int((earn_routine or {}).get("pending") or 0)
    routine_done = int((earn_routine or {}).get("done") or 0)
    routine_points_pending = int((earn_routine or {}).get("pointsPending") or 0)
    routine_total = routine_pending + routine_done

    points_per_search = int(SEARCH_CONFIG.get("points_per_search") or 3)
    search_points_max = scraped.get("searchPoints")
    if not isinstance(search_points_max, int):
        search_points_max = max(0, search_total) * points_per_search
    search_points_cur = 0
    if search_total > 0:
        search_points_cur = int(round(search_points_max * (search_done / search_total)))

    activity_points_max = scraped.get("activityPoints") if isinstance(scraped.get("activityPoints"), int) else 0
    activity_points_cur = 0
    if activity_total > 0 and activity_points_max > 0:
        activity_points_cur = int(round(activity_points_max * (activity_done / activity_total)))

    # 面板「任务」：搜索 + 每日活动 + 首页每日任务 + 赚取页日常任务
    task_current = (
        max(0, search_done)
        + max(0, activity_done)
        + max(0, daily_set_done)
        + max(0, routine_done)
    )
    task_max = (
        max(0, search_total)
        + max(0, activity_total)
        + max(0, daily_set_total)
        + max(0, routine_total)
    )

    # 面板「积分」：优先今日积分；上限含可领取与各分区待领分
    today_points = scraped.get("todayPoints") if isinstance(scraped.get("todayPoints"), int) else None
    claimable = scraped.get("claimable") if isinstance(scraped.get("claimable"), int) else 0
    points_current = today_points if today_points is not None else (search_points_cur + activity_points_cur)
    points_max = max(
        points_current + max(0, claimable),
        search_points_max + activity_points_max + daily_set_points_pending + routine_points_pending,
        points_current + daily_set_points_pending + routine_points_pending,
        points_current,
    )

    # 兼容旧搜索逻辑（仍以搜索积分为主）
    pc_current = search_points_cur
    pc_max = search_points_max

    active_level = f"newLevel{level_num}" if level_num else level_name
    return {
        "dashboard": {
            "userStatus": {
                "availablePoints": balance,
                "isRewardsUser": True,
                "levelInfo": {
                    "activeLevel": active_level,
                    "activeLevelName": level_name,
                    "level": level_num,
                },
                "counters": {
                    "pcSearch": [
                        {
                            "pointProgress": pc_current,
                            "pointProgressMax": pc_max,
                            "pointMax": pc_max,
                        }
                    ],
                    "mobileSearch": [],
                },
            }
        },
        "isRewardsUser": True,
        "_source": "page_scrape",
        "_page": {
            "title": scraped.get("title"),
            "url": scraped.get("url") or last_url,
        },
        "_progress": {
            "search_done": search_done,
            "search_total": search_total,
            "activity_done": activity_done,
            "activity_total": activity_total,
            "daily_set_done": daily_set_done,
            "daily_set_total": daily_set_total,
            "routine_done": routine_done,
            "routine_total": routine_total,
            "today_points": today_points,
            "claimable": claimable,
            "task_current": task_current,
            "task_max": task_max,
            "points_current": points_current,
            "points_max": points_max,
            "streaks": _normalize_streaks(earn_streaks),
            "stamps": _normalize_stamps(earn_stamps),
        },
    }


def _sum_counter_points(counter_items):
    current = 0
    maximum = 0
    if not counter_items:
        return 0, 0
    for item in counter_items:
        # JS: pointProgress / pointMax 或 pointProgressMax
        current += int(item.get("pointProgress") or 0)
        maximum += int(item.get("pointMax") or item.get("pointProgressMax") or 0)
    return current, maximum


def compute_remaining_searches(userinfo: dict, device_type: str) -> dict:
    """根据 Rewards counters 计算当前设备类型剩余搜索次数。

    device_type: "windows" (PC) 或 "iphone" (Mobile)
    """
    dashboard = userinfo.get("dashboard") or userinfo
    user_status = (dashboard or {}).get("userStatus") or {}
    counters = user_status.get("counters") or {}

    pc_current, pc_max = _sum_counter_points(counters.get("pcSearch") or [])
    mobile_current, mobile_max = _sum_counter_points(counters.get("mobileSearch") or [])

    if device_type == "windows":
        current_points = pc_current
        max_points = pc_max
        device_label = "电脑"
    else:
        current_points = mobile_current
        max_points = mobile_max
        device_label = "手机"

    # 若 counters 完全为空（无法获取数据），就用一个保守的兜底值
    # 但如果 counters 存在但 max_points 为 0，说明该设备不需要做任务，保持为 0
    if max_points <= 0 and not counters:
        max_points = 90

    remaining_points = max(0, max_points - current_points)
    points_per_search = SEARCH_CONFIG["points_per_search"]
    remaining_searches = (remaining_points + points_per_search - 1) // points_per_search if max_points > 0 else 0
    total_searches = (max_points + points_per_search - 1) // points_per_search if max_points > 0 else 0

    return {
        "device_label": device_label,
        "pc": {"current": pc_current, "max": pc_max},
        "mobile": {"current": mobile_current, "max": mobile_max},
        "current_points": current_points,
        "max_points": max_points,
        "remaining_points": remaining_points,
        "remaining_searches": int(remaining_searches),
        "total_searches": int(total_searches),
    }


def _clear_console() -> None:
    """清空控制台输出（在 Windows Terminal/PowerShell 下尽量兼容）。"""
    if not sys.stdout.isatty():
        return

    # 优先 ANSI（Windows Terminal/新 PowerShell 支持）
    try:
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()
        return
    except Exception:
        pass

    # 兜底：cls
    try:
        os.system("cls")
    except Exception:
        pass


def _supports_ansi() -> bool:
    """判断当前控制台是否大概率支持 ANSI 光标控制。"""
    if not sys.stdout.isatty():
        return False
    # Windows Terminal / VS Code terminal / 新版 PowerShell 通常支持
    # 这里不做过度检测，失败时会 fallback 到 clear。
    return True


def _bar(pct: float, width: int = 20) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * (pct / 100.0)))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def _pct(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        # 当分母为0时，如果分子也为0，表示任务不需要做，返回100%
        return 100.0 if numerator <= 0 else 0.0
    return max(0.0, min(100.0, (numerator / denominator) * 100.0))


def _fmt_duration(seconds: float) -> str:
    """格式化运行时长为易读字符串"""
    seconds = int(max(0, seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_console_dashboard(
    *,
    userinfo: dict | None,
    stats: dict | None,
    status_text: str,
    search_index: int | None,
    search_total: int | None,
) -> str:
    now_str = time.strftime("%H:%M:%S")
    runtime_str = _fmt_duration(time.time() - APP_START_TS)

    def _search_progress(current: int, maximum: int) -> tuple[int, int]:
        points_per_search = SEARCH_CONFIG["points_per_search"]
        maximum = max(0, int(maximum))
        current = max(0, int(current))
        total = (maximum + points_per_search - 1) // points_per_search if points_per_search > 0 else 0
        remaining = max(0, maximum - current)
        remaining_searches = (remaining + points_per_search - 1) // points_per_search if points_per_search > 0 else 0
        done = max(0, total - remaining_searches)
        return done, total

    if userinfo is None or stats is None:
        lines = [
            f"当前时间:{now_str}  运行时长: {runtime_str}",
            "=" * 60,
            f"状态: {status_text}",
            "=" * 60,
        ]
        return "\n".join(lines)

    dashboard = userinfo.get("dashboard") or userinfo
    user_status = (dashboard or {}).get("userStatus") or {}
    level_info = user_status.get("levelInfo") or {}
    level_name = level_info.get("activeLevel") or level_info.get("level") or "未知"
    total_points = int(user_status.get("availablePoints") or 0)

    pc_cur = stats["pc"]["current"]
    pc_max = stats["pc"]["max"]

    pc_pct = _pct(pc_cur, pc_max)
    pc_done, pc_total = _search_progress(pc_cur, pc_max)

    def _status_line(done: int, total: int) -> str:
        # 统一展示为“状态: 剩余 N 次”，不显示 (进度) 或其它文案
        remaining_count = max(0, int(total) - int(done))
        return f"状态: 剩余 {remaining_count} 次"

    lines = [
        f"当前时间:{now_str}  运行时长: {runtime_str}",
        "=" * 60,
        f"等级: {level_name}",
        f"总积分: {total_points}",
        f"今日获取: {pc_cur} / {pc_max}  {_bar(pc_pct)} {pc_pct:6.1f}%",
        _status_line(pc_done, pc_total),
        "=" * 60,
    ]
    return "\n".join(lines)


def _render_console_dashboard(text: str, *, use_ansi: bool, first_paint: bool) -> None:
    """把仪表盘文本渲染到控制台（原地更新）。"""
    if not sys.stdout.isatty():
        print(text, end="")
        return

    if use_ansi:
        # 首次输出前清屏一次，后续用“回到左上角 + 清到屏末”实现无闪烁刷新
        if first_paint:
            sys.stdout.write("\x1b[2J\x1b[H")
        else:
            sys.stdout.write("\x1b[H\x1b[J")
        sys.stdout.write(text)
        sys.stdout.flush()
        return

    # fallback：不支持 ANSI 时就用清屏（可能闪一下）
    _clear_console()
    print(text, end="")


async def console_dashboard_refresh_loop(context, device_type: str, state: dict):
    """定时刷新 Rewards 数据，供控制台仪表盘显示。"""
    while True:
        try:
            userinfo = await fetch_rewards_userinfo(context)
            stats = compute_remaining_searches(userinfo, device_type)
            state["userinfo"] = userinfo
            state["stats"] = stats
            state["error"] = None
            state["last_fetch_ts"] = time.time()
        except Exception as e:
            state["error"] = str(e)

        await asyncio.sleep(DASHBOARD_REFRESH_SECONDS)


async def run_rewards_auto_search(page, context, device_type: str):
    """自动完成 Bing 搜索任务（等价于 userscript 的 startSearchTask）。"""
    from activities import claim_available_rewards, complete_daily_activities

    # 控制台仪表盘：启动后台刷新
    state = {"userinfo": None, "stats": None, "error": None}
    refresh_task = asyncio.create_task(console_dashboard_refresh_loop(context, device_type, state))

    status_text = "准备..."
    use_ansi = _supports_ansi()
    first_paint = True
    print("\n正在获取 Rewards 搜索进度...")
    try:
        userinfo = await fetch_rewards_userinfo(context, page=page)
        stats = compute_remaining_searches(userinfo, device_type)
        state["userinfo"] = userinfo
        state["stats"] = stats
    except Exception as e:
        state["error"] = str(e)
        userinfo = None
        stats = None

    # 先领取 + 每日活动（即使搜索已完成也执行）
    try:
        await claim_available_rewards(page, log=lambda m: print(m))
    except Exception as e:
        print(f"自动领取失败: {e}")
    try:
        await complete_daily_activities(page, context, log=lambda m: print(m))
    except Exception as e:
        print(f"每日活动失败: {e}")

    _render_console_dashboard(
        _format_console_dashboard(
            userinfo=state.get("userinfo"),
            stats=state.get("stats"),
            status_text=status_text if not state.get("error") else f"错误: {state['error']}",
            search_index=None,
            search_total=None,
        ),
        use_ansi=use_ansi,
        first_paint=first_paint,
    )
    first_paint = False

    if stats is None:
        refresh_task.cancel()
        try:
            await refresh_task
        except Exception:
            pass
        raise RuntimeError(f"无法获取 Rewards 数据: {state.get('error')}")

    remaining = stats["remaining_searches"]
    total = stats["total_searches"]
    # 用控制台仪表盘展示（不再额外 print 一行）

    # 检查当前设备是否需要执行任务
    if total <= 0 or remaining <= 0:
        status_text = "剩余 0 次"
        _render_console_dashboard(
            _format_console_dashboard(
                userinfo=state.get("userinfo"),
                stats=state.get("stats"),
                status_text=status_text,
                search_index=None,
                search_total=None,
            ),
            use_ansi=use_ansi,
            first_paint=first_paint,
        )
        refresh_task.cancel()
        try:
            await refresh_task
        except Exception:
            pass
        return

    if stats["max_points"] <= 0:
        status_text = "剩余 0 次"
        _render_console_dashboard(
            _format_console_dashboard(
                userinfo=state.get("userinfo"),
                stats=state.get("stats"),
                status_text=status_text,
                search_index=None,
                search_total=None,
            ),
            use_ansi=use_ansi,
            first_paint=first_paint,
        )
        refresh_task.cancel()
        try:
            await refresh_task
        except Exception:
            pass
        return

    delay_cfg = SEARCH_CONFIG["pc"] if device_type == "windows" else SEARCH_CONFIG["mobile"]
    min_ms = delay_cfg["min_delay_ms"]
    max_ms = delay_cfg["max_delay_ms"]

    # 先确保能打开 Bing（有时直接搜索 URL 会被重定向）
    if page.url == "about:blank":
        await page.goto("https://www.bing.com", wait_until=DOMCONTENTLOADED)

    status_text = "开始自动搜索（Ctrl+C 可中断）"
    _render_console_dashboard(
        _format_console_dashboard(
            userinfo=state.get("userinfo"),
            stats=state.get("stats"),
            status_text=status_text,
            search_index=None,
            search_total=None,
        ),
        use_ansi=use_ansi,
        first_paint=first_paint,
    )

    # 对齐 userscript：首次延迟 2 秒再开始（避免一打开就“秒搜”）
    await asyncio.sleep(2)

    for i in range(1, remaining + 1):
        query = generate_random_query()
        search_url = f"https://www.bing.com/search?q={quote_plus(query)}"

        try:
            status_text = f"搜索中... 关键词: {query}"
            _render_console_dashboard(
                _format_console_dashboard(
                    userinfo=state.get("userinfo"),
                    stats=state.get("stats"),
                    status_text=status_text if not state.get("error") else f"{status_text} | 错误: {state['error']}",
                    search_index=None,
                    search_total=None,
                ),
                use_ansi=use_ansi,
                first_paint=first_paint,
            )

            # 更像 userscript：在搜索框里输入并提交（失败才 fallback 到 goto 搜索 URL）
            ok = False
            search_coro = _perform_bing_search_like_human(page, query)
            try:
                ok = await asyncio.wait_for(search_coro, timeout=25)
            except asyncio.TimeoutError:
                status_text = "搜索超时，刷新页面重试"
                _render_console_dashboard(
                    _format_console_dashboard(
                        userinfo=state.get("userinfo"),
                        stats=state.get("stats"),
                        status_text=status_text,
                        search_index=None,
                        search_total=None,
                    ),
                    use_ansi=use_ansi,
                    first_paint=first_paint,
                )
                try:
                    await page.goto("https://www.bing.com", wait_until=DOMCONTENTLOADED, timeout=20000)
                except Exception:
                    pass
                ok = False
            except Exception:
                ok = False

            if not ok:
                await page.goto(search_url, wait_until=DOMCONTENTLOADED)

            # 轻量随机交互：滚动/偶尔点开一个结果再返回
            await _maybe_human_scroll(page)
            await _maybe_click_one_result(page)
        except Exception as e:
            # 网络波动/跳转异常时，尝试回到首页再继续
            status_text = f"搜索跳转失败: {e}"
            _render_console_dashboard(
                _format_console_dashboard(
                    userinfo=state.get("userinfo"),
                    stats=state.get("stats"),
                    status_text=status_text,
                        search_index=None,
                        search_total=None,
                ),
                use_ansi=use_ansi,
                first_paint=first_paint,
            )
            try:
                await page.goto("https://www.bing.com", wait_until=DOMCONTENTLOADED)
            except Exception:
                pass

        # 用 monotonic 做倒计时：更稳定（系统时间调整/睡眠恢复时不容易“看起来卡住”）
        delay_ms = random.randint(min_ms, max_ms)
        end_ts = time.monotonic() + (delay_ms / 1000.0)
        while True:
            remaining_seconds = int((end_ts - time.monotonic()) + 0.999)
            if remaining_seconds <= 0:
                break

            status_text = f"等待 {remaining_seconds} 秒"
            _render_console_dashboard(
                _format_console_dashboard(
                    userinfo=state.get("userinfo"),
                    stats=state.get("stats"),
                    status_text=status_text if not state.get("error") else f"{status_text} | 错误: {state['error']}",
                    search_index=None,
                    search_total=None,
                ),
                use_ansi=use_ansi,
                first_paint=first_paint,
            )

            # 让 UI 每秒刷新；末尾阶段用更短 sleep 以更平滑
            await asyncio.sleep(min(1.0, max(0.1, end_ts - time.monotonic())))

    status_text = "剩余 0 次"
    _render_console_dashboard(
        _format_console_dashboard(
            userinfo=state.get("userinfo"),
            stats=state.get("stats"),
            status_text=status_text,
            search_index=None,
            search_total=None,
        ),
        use_ansi=use_ansi,
        first_paint=first_paint,
    )

    # 可选：结束后再拉一次数据，展示最新进度（失败也不影响主流程）
    try:
        userinfo2 = await fetch_rewards_userinfo(context)
        stats2 = compute_remaining_searches(userinfo2, device_type)
        state["userinfo"] = userinfo2
        state["stats"] = stats2

        # 验证伪装是否成功：如果是 iPhone 模式，检查是否在"移动搜索"计数上增加了
        if device_type == "iphone":
            before_mobile = stats["mobile"]["current"]
            after_mobile = stats2["mobile"]["current"]
            if after_mobile > before_mobile:
                spoof_result = f"✓ 手机伪装成功（移动搜索：{before_mobile} → {after_mobile}）"
            else:
                spoof_result = f"✗ 伪装可能失败（移动搜索未增加：{before_mobile} / {stats2['mobile']['max']}）"
        else:
            spoof_result = ""

        _render_console_dashboard(
            _format_console_dashboard(
                userinfo=state.get("userinfo"),
                stats=state.get("stats"),
                status_text=f"剩余 0 次",
                search_index=None,
                search_total=None,
            ),
            use_ansi=use_ansi,
            first_paint=first_paint,
        )
    except Exception as e:
        # 只在控制台提示，不抛出
        _render_console_dashboard(
            _format_console_dashboard(
                userinfo=state.get("userinfo"),
                stats=state.get("stats"),
                status_text=f"剩余 0 次",
                search_index=None,
                search_total=None,
            ),
            use_ansi=use_ansi,
            first_paint=first_paint,
        )

    # 结束后台刷新
    refresh_task.cancel()
    try:
        await refresh_task
    except Exception:
        pass


def get_cookie_files():
    """获取所有已保存的cookie文件"""
    # 优先从可写目录读取（exe 同目录），其次从打包资源目录读取（onefile 临时解压）
    exe_cookies_dir = _get_exe_dir() / "Assets" / "cookies"
    bundle_cookies_dir = _get_bundle_dir() / "Assets" / "cookies"

    cookie_files: list[Path] = []
    seen: set[Path] = set()
    for d in [exe_cookies_dir, bundle_cookies_dir]:
        try:
            if d.exists():
                for p in d.glob("*.json"):
                    rp = p.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        cookie_files.append(p)
        except Exception:
            # 目录不可访问就跳过
            continue

    return cookie_files


def select_cookie_file():
    """让用户选择cookie文件"""
    cookie_files = get_cookie_files()

    # 去重：同名账号优先选择 exe 同目录下的（可写、可更新）
    by_name: dict[str, Path] = {}
    exe_dir = _get_exe_dir().resolve()
    for p in cookie_files:
        name = p.stem
        if name not in by_name:
            by_name[name] = p
            continue
        try:
            # 若当前是 exe 同目录，覆盖掉 bundle 中的同名文件
            if p.resolve().is_relative_to(exe_dir):
                by_name[name] = p
        except Exception:
            # Python < 3.9 或路径解析失败时，保持现状
            pass

    cookie_files = sorted(by_name.values(), key=lambda x: x.stem.lower())
    
    if not cookie_files:
        print("\n未找到已保存的cookie文件!")
        return None
    
    print("\n可用的账号:")
    for i, file in enumerate(cookie_files, 1):
        account_name = file.stem  # 获取不带扩展名的文件名
        print(f"{i}. {account_name}")
    
    while True:
        try:
            choice = input("\n请选择账号 (输入序号): ").strip()
            index = int(choice) - 1
            if 0 <= index < len(cookie_files):
                return cookie_files[index]
            else:
                print("无效的选择，请重新输入!")
        except ValueError:
            print("请输入有效的数字!")
        except KeyboardInterrupt:
            return None




async def login_and_save():
    """登录并保存cookie"""
    # 注意：onefile 打包下 __file__ 在临时目录，写入会丢失；所以固定写到 exe 同目录。
    cookies_dir = _get_exe_dir() / "Assets" / "cookies"
    cookies_dir.mkdir(parents=True, exist_ok=True)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel=BROWSER_CHANNEL,
            headless=False
        )
        
        context = await browser.new_context()
        page = await context.new_page()
        
        try:
            print("正在打开 Bing Rewards 页面...")
            await page.goto(
                "https://rewards.bing.com/?ref=rewardspanel",
                wait_until=DOMCONTENTLOADED,
                timeout=60000,
            )
            
            print("\n请在浏览器中登录您的Microsoft账户...")
            print("登录完成后，按回车键保存Cookie...")
            
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, input)
            
            # 获取账户名
            account_name = None
            
            try:
                account_xpath = '//*[@id="mectrl_currentAccount_secondary"]'
                account_element = await page.query_selector(f"xpath={account_xpath}")
                if account_element:
                    text = await account_element.text_content()
                    if text and text.strip():
                        account_name = text.strip()
                        print(f"\n检测到账户: {account_name}")
            except Exception as e:
                print(f"方法1失败: {e}")
            
            if not account_name:
                try:
                    account_element = await page.query_selector("#mectrl_currentAccount_secondary")
                    if account_element:
                        text = await account_element.text_content()
                        if text and text.strip():
                            account_name = text.strip()
                            print(f"\n检测到账户: {account_name}")
                except Exception as e:
                    print(f"方法2失败: {e}")
            
            if not account_name:
                try:
                    account_name = await page.evaluate('''() => {
                        const el = document.getElementById("mectrl_currentAccount_secondary");
                        return el ? el.textContent.trim() : null;
                    }''')
                    if account_name:
                        print(f"\n检测到账户: {account_name}")
                except Exception as e:
                    print(f"方法3失败: {e}")
            
            if not account_name:
                account_name = "未知账户"
                print("\n警告: 未能检测到账户名，使用默认名称")

            # 保存前确认 Rewards API/页面抓取可用，避免写入半成品会话
            try:
                await fetch_rewards_userinfo(context, timeout_ms=20000, page=page)
            except Exception as e:
                print(f"\n错误: Rewards API 不可用，未保存会话: {e}")
                return

            account = await resolve_account_identity(page, context)
            cookie_file, removed = await save_account_session(context, cookies_dir, account)
            print(f"会话已保存到: {cookie_file}")
            if removed:
                print("已删除同账号重复文件:", ", ".join(p.name for p in removed))
            await asyncio.sleep(1)
            
        except Exception as e:
            print(f"\n错误: {str(e)}")
        finally:
            try:
                await browser.close()
            except Exception as e:
                print(f"关闭浏览器时出现异常（可忽略）: {e}")
            print("浏览器已关闭")


async def use_saved_cookie():
    """使用已保存的cookie访问Bing"""
    # 选择cookie文件
    cookie_file = select_cookie_file()
    if not cookie_file:
        print("\n操作已取消")
        return
    
    # 加载会话（兼容旧 cookie 数组与 storage_state）
    try:
        storage_state = load_auth_state(cookie_file)
    except Exception as e:
        print(f"\n读取会话失败: {e}")
        return
    
    device_type = "windows"
    user_agent = USER_AGENTS[device_type]
    
    print(f"\n已选择账号: {cookie_file.stem}")
    print(f"设备类型: Windows")
    print(f"User-Agent: {user_agent}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel=BROWSER_CHANNEL,
            headless=False
        )
        
        # 创建上下文时设置 user agent
        # 说明：在 Windows 上用 Chromium(Edge) 不能“真正变成 iOS Safari/WebKit”，但可以：
        # - 让站点看到 iPhone UA
        # - 同时启用 is_mobile/has_touch/device_scale_factor 等移动特征
        # 这比只改 UA 更一致。

        if device_type == "windows":
            context_kwargs = {
                "user_agent": user_agent,
                "viewport": {"width": 1920, "height": 1080},
                "screen": {"width": 1920, "height": 1080},
                # 可选：更贴近中文用户环境（不影响 cookie 身份）
                "locale": "zh-CN",
            }
        else:
            # 优先使用 Playwright 内置设备配置（能一次性带上 viewport/is_mobile/has_touch 等）
            # 设备名会随 Playwright 版本略有差异，所以做 KeyError 兜底。
            device_profile = None
            for name in ["iPhone 15", "iPhone 14", "iPhone 13", "iPhone 12"]:
                try:
                    device_profile = p.devices[name]
                    break
                except Exception:
                    continue

            context_kwargs = dict(device_profile or {})
            # 保持你自定义的 iPhone UA（更贴近你设定的版本），覆盖 profile 默认 UA
            context_kwargs["user_agent"] = user_agent

            # 视口与屏幕（screen 不一定必须，但有助于减少“桌面窗口”味道）
            context_kwargs.setdefault("viewport", {"width": 390, "height": 844})
            context_kwargs.setdefault("screen", {"width": 390, "height": 844})

            # 显式打开移动特征
            context_kwargs["is_mobile"] = True
            context_kwargs["has_touch"] = True
            context_kwargs["device_scale_factor"] = 3

            # 移动端本地化设置
            context_kwargs["locale"] = "zh-CN"
            context_kwargs["timezone_id"] = "Asia/Shanghai"

        context_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**context_kwargs)
        
        page = await context.new_page()
        
        try:
            print("\n正在打开 Bing 页面...")
            await page.goto("https://www.bing.com", wait_until=DOMCONTENTLOADED)

            # 选择 UA 后自动开始（移植自 Microsoft Rewards Dashboard.js）
            await run_rewards_auto_search(page, context, device_type)

            print("\n脚本执行结束，按回车键关闭浏览器...")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, input)
            
        except Exception as e:
            print(f"\n错误: {str(e)}")
        finally:
            try:
                await browser.close()
            except Exception as e:
                print(f"关闭浏览器时出现异常（可忽略）: {e}")
            print("浏览器已关闭")


async def main():
    """主函数"""
    print("\n请选择操作:")
    print("1. 登录并保存Cookie")
    print("2. 使用并开始每日任务")
    
    while True:
        try:
            choice = input("\n请选择操作 (输入序号): ").strip()
            if choice == "1":
                await login_and_save()
                break
            elif choice == "2":
                await use_saved_cookie()
                break
            else:
                print("无效的选择，请重新输入!")
        except KeyboardInterrupt:
            print("\n\n操作已取消")
            break


if __name__ == "__main__":
    print("=" * 60)
    print("My QQ 3183670554")
    print("=" * 60)
    asyncio.run(main())
