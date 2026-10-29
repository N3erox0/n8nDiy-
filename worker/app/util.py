"""通用工具：时间、场次、链接规范化、HTTP。"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .config import TZ

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def now() -> datetime:
    return datetime.now(TZ)


def http(timeout: float = 20.0, **kw) -> httpx.Client:
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    headers.update(kw.pop("headers", {}))
    return httpx.Client(timeout=timeout, follow_redirects=True, headers=headers, **kw)


# ---------- 场次与时间窗 ----------

def session_of(t: datetime | None = None) -> tuple[str, str]:
    """返回 (场次名, 场次键)。07:30 前后到 17:30 之间算早场后的“晚场窗口”。

    早场：前一天 17:30 ~ 今天 07:30，选题卡 08:00 推送
    晚场：今天 07:30 ~ 17:30，选题卡 18:00 推送
    任务运行时刻决定场次：12:00 之前跑的算早场，之后算晚场。
    """
    t = t or now()
    name = "早场" if t.hour < 12 else "晚场"
    return name, f"{t.date().isoformat()}-{name}"


def window_of(t: datetime | None = None) -> tuple[datetime, datetime]:
    t = t or now()
    d = t.date()
    if t.hour < 12:
        start = datetime(d.year, d.month, d.day, 17, 30, tzinfo=TZ) - timedelta(days=1)
        end = datetime(d.year, d.month, d.day, 7, 30, tzinfo=TZ)
    else:
        start = datetime(d.year, d.month, d.day, 7, 30, tzinfo=TZ)
        end = datetime(d.year, d.month, d.day, 17, 30, tzinfo=TZ)
    return start, end


def ymd(d: date | datetime | None = None) -> str:
    d = d or now()
    return d.strftime("%Y%m%d")


def fmt_time(t: datetime | None, date_only: bool = False) -> str:
    if not t:
        return "—"
    t = t.astimezone(TZ)
    return t.strftime("%Y-%m-%d") if date_only else t.strftime("%Y-%m-%d %H:%M")


# ---------- X 帖子 ID 换算时间 ----------

def snowflake_time(tweet_id: int | str) -> datetime:
    ms = (int(tweet_id) >> 22) + 1288834974657
    return datetime.fromtimestamp(ms / 1000, TZ)


# ---------- 链接规范化 ----------

DROP_PARAMS = re.compile(r"^(utm_|spm|from|share|scene|src|ref|fbclid|gclid|mc_|igshid|si$|s$|t$)", re.I)
KEEP_WECHAT = {"__biz", "mid", "idx", "sn"}
SHORT_HOSTS = {
    "t.co", "bit.ly", "tinyurl.com", "goo.gl", "ow.ly", "buff.ly", "dwz.cn", "url.cn",
    "t.cn", "b23.tv", "lnkd.in", "is.gd", "rebrand.ly",
}
JUMP_PATTERNS = (
    "bing.com/ck/", "bing.com/news/apiclick", "bing.com/aclick", "google.com/url",
    "news.google.com/rss/articles", "link.zhihu.com", "weibo.cn/sinaurl", "/redirect?",
)


def normalize_url(url: str) -> str | None:
    """规范化链接；短链、跳转链接返回 None（不收）。"""
    if not url:
        return None
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        return None
    low = url.lower()
    if any(p in low for p in JUMP_PATTERNS):
        return None
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host in {"twitter.com", "mobile.twitter.com", "mobile.x.com"}:
        host = "x.com"
    if host in SHORT_HOSTS:
        return None
    if host == "mp.weixin.qq.com":
        q = [(k, v) for k, v in parse_qsl(parts.query) if k in KEEP_WECHAT]
    else:
        q = [(k, v) for k, v in parse_qsl(parts.query) if not DROP_PARAMS.match(k)]
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit(("https", host, path, urlencode(q), ""))


def platform_of(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    mapping = {
        "x.com": "X", "mp.weixin.qq.com": "公众号", "news.ycombinator.com": "Hacker News",
        "huggingface.co": "Hugging Face", "arxiv.org": "arXiv", "github.com": "GitHub",
        "weibo.com": "微博", "zhihu.com": "知乎", "zhuanlan.zhihu.com": "知乎",
    }
    return mapping.get(host, host)


def is_wechat_permalink(url: str) -> bool:
    u = url.lower()
    return "mp.weixin.qq.com/s/" in u or ("mp.weixin.qq.com/s?" in u and "__biz=" in u)


def short(text: str | None, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def sleep_min_interval(last: dict, key: str, gap: float) -> None:
    t = last.get(key)
    if t is not None:
        wait = gap - (time.monotonic() - t)
        if wait > 0:
            time.sleep(wait)
    last[key] = time.monotonic()
