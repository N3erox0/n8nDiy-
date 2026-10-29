"""WF-01 分组采集：拉取订阅源 → 四必填 → 规范化 → 时间窗 → 去重 → 暂存；每个源写一行采集统计。"""
from __future__ import annotations

import json
import re
import time
from calendar import timegm
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote, urljoin, urlsplit

import feedparser
import httpx
import yaml
from bs4 import BeautifulSoup

from . import config, db
from .config import TZ
from .extract import fetch_article
from .util import http, is_wechat_permalink, normalize_url, now, platform_of, snowflake_time, window_of

X_STATUS = re.compile(r"^https://x\.com/([A-Za-z0-9_]+)/status/(\d+)")


@dataclass
class Item:
    url: str
    title: str = ""
    summary: str = ""
    author: str = ""
    published_at: datetime | None = None
    date_only: bool = False
    platform: str = ""
    content_md: str = ""


@dataclass
class SourceResult:
    source: dict
    status: str = "成功"
    reason: str = ""
    items: list[Item] = field(default_factory=list)
    raw_count: int = 0
    secs: float = 0.0


def load_sources() -> dict:
    return yaml.safe_load((config.CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8"))


def _entry_time(e) -> tuple[datetime | None, bool]:
    for k in ("published_parsed", "updated_parsed", "created_parsed"):
        v = e.get(k)
        if v:
            return datetime.fromtimestamp(timegm(v), TZ), False
    return None, False


def _clean(html: str | None) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)[:600]


def parse_feed(text: str, default_author: str = "") -> list[Item]:
    f = feedparser.parse(text)
    out = []
    for e in f.entries:
        link = e.get("link") or ""
        t, d_only = _entry_time(e)
        author = e.get("author") or default_author or (f.feed.get("title") if f.get("feed") else "") or ""
        out.append(Item(url=link, title=(e.get("title") or "").strip(), summary=_clean(e.get("summary")),
                        author=author.strip(), published_at=t, date_only=d_only))
    return out


def _get(client: httpx.Client, url: str) -> str:
    r = client.get(url)
    if r.status_code == 403:
        raise PermissionError("403 被拒")
    r.raise_for_status()
    return r.text


def fetch_source(src: dict, client: httpx.Client, deadline: float) -> SourceResult:
    t0 = time.monotonic()
    res = SourceResult(src)
    m = src.get("method")
    try:
        if m == "none":
            res.status, res.reason = "失败", "无订阅方式"
        elif m in ("rss", "wechat2rss"):
            res.items = parse_feed(_get(client, src["url"]), src.get("author", ""))
        elif m == "rsshub":
            res.items = parse_feed(_get(client, config.RSSHUB_URL + src["route"]), src.get("author", ""))
        elif m == "x":
            res.items = _x_items(client, src)
        elif m == "kw_bing":
            res.items = _bing(client, src["query"])
        elif m == "kw_gnews":
            res.items = _gnews(client, src["query"], deadline)
        elif m == "kw_hn":
            res.items = parse_feed(_get(client, "https://hnrss.org/newest?q=" + quote(src["query"]) + "&points=30"))
        elif m == "page":
            res.items = _page(client, src, deadline)
        else:
            res.status, res.reason = "失败", f"未知方式 {m}"
        if res.status == "成功" and not res.items and src.get("alt"):
            alt = dict(src, **src["alt"])
            alt.pop("alt", None)
            return fetch_source(alt, client, deadline)
    except PermissionError:
        res.status, res.reason = "失败", "403 被拒"
    except httpx.ConnectError:
        res.status, res.reason = "失败", "网络不可达"
    except httpx.TimeoutException:
        res.status, res.reason = "失败", "超时"
    except httpx.HTTPStatusError as e:
        res.status, res.reason = "失败", f"HTTP {e.response.status_code}"
    except Exception as e:  # noqa: BLE001
        res.status, res.reason = "失败", f"解析失败：{type(e).__name__}"
    res.raw_count = len(res.items)
    res.secs = round(time.monotonic() - t0, 1)
    return res


def _bing_rss(client: httpx.Client, q: str) -> list[Item]:
    # 10-29 实测：本机默认市场下 Bing 会忽略查询词；带 mkt=en-US 才返回相关结果和 pubDate
    url = "https://www.bing.com/search?format=rss&mkt=en-US&cc=US&q=" + quote(q)
    return parse_feed(_get(client, url))


def _x_items(client: httpx.Client, src: dict) -> list[Item]:
    items: list[Item] = []
    raw = 0
    for handle in [src["handle"]] + ([src["alt_handle"]] if src.get("alt_handle") else []):
        res = _bing_rss(client, f"site:x.com/{handle}")
        raw += len(res)
        for it in res:
            n = normalize_url(it.url) or ""
            m = X_STATUS.match(n)
            if not m or m.group(1).lower() != handle.lower():
                continue
            it.url = f"https://x.com/{m.group(1)}/status/{m.group(2)}"
            it.published_at = snowflake_time(m.group(2))
            it.author = "@" + m.group(1)
            items.append(it)
        if items:
            break
    if not items:
        # 搜不到该账号的帖子（site: 被忽略或无结果）：算失败而不是「无更新」，统计才真实
        raise RuntimeError("Bing 未检索到该账号帖子" if not raw else "Bing site: 检索失效")
    return items


def _bing(client: httpx.Client, q: str) -> list[Item]:
    out = []
    for it in _bing_rss(client, q):
        if not it.url:
            continue
        host = urlsplit(it.url).netloc.lower().removeprefix("www.")
        it.author = host  # 搜索结果没有作者，用站点域名
        out.append(it)
    return out


def _gnews_decode(client: httpx.Client, link: str) -> str | None:
    """Google News 跳转链接 → 原文链接（10-29 实测可用：页面签名 + batchexecute）。"""
    try:
        aid = link.split("/articles/")[1].split("?")[0]
        html = client.get(f"https://news.google.com/rss/articles/{aid}").text
        sig = re.search(r'data-n-a-sg="([^"]+)"', html)
        ts = re.search(r'data-n-a-ts="([^"]+)"', html)
        if not (sig and ts):
            return None
        inner = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None,
                                None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                 aid, int(ts.group(1)), sig.group(1)]
        r = client.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                        data={"f.req": json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]])},
                        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        body = r.text.split("\n\n", 1)[1] if "\n\n" in r.text else r.text
        url = json.loads(json.loads(body)[0][2])[1]
        return url if isinstance(url, str) and url.startswith("http") else None
    except Exception:  # noqa: BLE001
        return None


def _gnews(client: httpx.Client, q: str, deadline: float, max_items: int = 12) -> list[Item]:
    loc = ("hl=zh-CN&gl=CN&ceid=CN:zh-Hans" if re.search(r"[\u4e00-\u9fff]", q) else "hl=en-US&gl=US&ceid=US:en")
    url = "https://news.google.com/rss/search?q=" + quote(q) + "+when:1d&" + loc
    f = feedparser.parse(_get(client, url))
    out = []
    for e in f.entries[:max_items]:
        if time.monotonic() > deadline:
            break
        real = _gnews_decode(client, e.get("link") or "")
        if not real:
            continue
        t, _ = _entry_time(e)
        src = e.get("source") or {}
        title = re.sub(r"\s+-\s+[^-]+$", "", (e.get("title") or "").strip())  # 去掉标题末尾的「 - 媒体名」
        out.append(Item(url=real, title=title, summary=_clean(e.get("summary")),
                        author=(src.get("title") or urlsplit(real).netloc).strip(), published_at=t))
    return out


def _page(client: httpx.Client, src: dict, deadline: float, max_articles: int = 6) -> list[Item]:
    html = _get(client, src["url"])
    soup = BeautifulSoup(html, "lxml")
    pat = re.compile(src["link_pattern"])
    seen: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(src["url"], a["href"]).split("#")[0].rstrip("/")
        if pat.match(href) or pat.match(href + "/"):
            seen.setdefault(href, a.get_text(" ", strip=True))
    out = []
    for href, text in list(seen.items())[:max_articles]:
        if time.monotonic() > deadline:
            break
        try:
            art = fetch_article(href, client)
        except Exception:  # noqa: BLE001
            continue
        out.append(Item(url=href, title=art.title or text, summary=art.text_md[:300],
                        author=art.author or src.get("author", ""), published_at=art.published_at,
                        date_only=art.date_only, content_md=art.text_md))
    return out


def normalize_item(it: Item, src: dict, excl: list[str]) -> tuple[Item | None, str]:
    """返回（规范化后的条目, 丢弃原因）。"""
    url = normalize_url(it.url)
    if not url:
        return None, "非法或跳转链接"
    if src.get("method") == "wechat2rss" and not is_wechat_permalink(url):
        if "mp.weixin.qq.com" in url:
            return None, "非公众号永久链接"
    it.url = url
    it.platform = platform_of(url) if src.get("layer") not in ("L4",) else "公众号"
    if not it.author:
        it.author = src.get("author") or src.get("name", "")
    if not (it.url and it.platform and it.author and it.published_at):
        return None, "缺必填"
    text = f"{it.title} {it.summary}"
    if any(w in text for w in excl):
        return None, "范围外"
    return it, ""


def collect_group(group: str, at: datetime | None = None, dsn: str | None = None,
                  client: httpx.Client | None = None, workers: int = 8) -> dict:
    cfg = load_sources()
    gcfg = cfg["groups"][group]
    excl = cfg.get("exclude_words", [])
    srcs = [s for s in cfg["sources"] if s.get("group") == group]
    at = at or now()
    w0, w1 = window_of(at)
    from .util import session_of
    sname, skey = session_of(at)
    deadline = time.monotonic() + gcfg.get("timeout_min", 15) * 60
    own = client is None
    client = client or http(20)
    results: list[SourceResult] = []
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_source, s, client, deadline): s for s in srcs}
            for f in as_completed(futs):
                results.append(f.result())
    finally:
        if own:
            client.close()
    summary = {"group": group, "session": skey, "window": [w0.isoformat(), w1.isoformat()],
               "sources": len(srcs), "ok": 0, "failed": 0, "new": 0, "stored": 0}
    with db.conn(dsn) as c:
        for r in sorted(results, key=lambda r: r.source["id"]):
            stored = new_in_window = 0
            drop_reasons: dict[str, int] = {}
            for it in r.items:
                n, why = normalize_item(it, r.source, excl)
                if not n:
                    drop_reasons[why] = drop_reasons.get(why, 0) + 1
                    continue
                if not (w0 <= n.published_at < w1) and not (n.date_only and n.published_at.date() == w1.date()):
                    continue
                new_in_window += 1
                if c.execute("SELECT 1 FROM materials WHERE url_norm=%s", (n.url,)).fetchone():
                    continue
                mid = db.next_id(c, "S" + at.strftime("%Y%m%d"), 3, "materials")
                c.execute(
                    """INSERT INTO materials(id,stage,source_id,source_type,layer,url,url_norm,platform,author,
                       published_at,date_only,title,summary,content_md,status,session)
                       VALUES(%s,'staged',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'候选',%s)
                       ON CONFLICT DO NOTHING""",
                    (mid, r.source["id"], _stype(r.source), r.source.get("layer"), n.url, n.url, n.platform,
                     n.author, n.published_at, n.date_only, n.title, n.summary, n.content_md or None, skey))
                stored += 1
            status = r.status
            reason = r.reason
            if status == "成功" and r.raw_count and not new_in_window and drop_reasons.get("缺必填") == r.raw_count:
                status, reason = "失败", "缺必填"
            elif status == "成功" and new_in_window == 0:
                status = "无更新"
            c.execute(
                """INSERT INTO collect_stats(session,grp,source_id,source_name,method,status,items,new_items,stored,
                   reason,secs) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (skey, group, r.source["id"], r.source.get("name"), r.source.get("method"), status, r.raw_count,
                 new_in_window, stored, reason or None, r.secs))
            summary["ok" if status != "失败" else "failed"] += 1
            summary["new"] += new_in_window
            summary["stored"] += stored
    return summary


def _stype(src: dict) -> str:
    layer = src.get("layer")
    return {"L1": "官方", "L2": "X", "L3": "媒体", "L4": "公众号", "L5": "媒体", "L6": "媒体", "KW": "关键词"}.get(
        layer, "媒体")


def stats_line(c, session: str) -> str:
    rows = c.execute(
        "SELECT grp, status, reason FROM collect_stats WHERE session=%s", (session,)).fetchall()
    if not rows:
        return "本场采集：无记录"
    groups: dict[str, list[int]] = {}
    reasons: dict[str, int] = {}
    for r in rows:
        g = groups.setdefault(r["grp"], [0, 0])
        g[1] += 1
        if r["status"] != "失败":
            g[0] += 1
        else:
            key = (r["reason"] or "其他").split("：")[0]
            if key.startswith("HTTP"):
                key = "HTTP 错误"
            reasons[key] = reasons.get(key, 0) + 1
    gs = " · ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(groups.items()))
    fail = sum(reasons.values())
    dist = "、".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
    return f"本场采集：{gs} ｜ 失败 {fail}" + (f"：{dist}" if dist else "")
