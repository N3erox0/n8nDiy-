"""抓原文：正文抽取 + 本地备份（原始 HTML、正文 Markdown、图片）。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import trafilatura
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

from . import config
from .config import TZ
from .util import http


@dataclass
class Article:
    url: str
    title: str = ""
    author: str = ""
    published_at: datetime | None = None
    date_only: bool = False
    text_md: str = ""
    images: list[str] = field(default_factory=list)
    html: str = ""
    site: str = ""


def _parse_dt(s: str | None) -> tuple[datetime | None, bool]:
    if not s:
        return None, False
    s = s.strip()
    try:
        if re.fullmatch(r"\d{10}", s):
            return datetime.fromtimestamp(int(s), TZ), False
        d = dtparser.parse(s)
        date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))
        if d.tzinfo is None:
            d = d.replace(tzinfo=TZ)
        return d.astimezone(TZ), date_only
    except (ValueError, OverflowError):
        return None, False


def fetch_html(url: str, client: httpx.Client | None = None, timeout: float = 20) -> str:
    own = client is None
    client = client or http(timeout)
    try:
        r = client.get(url)
        r.raise_for_status()
        r.encoding = r.encoding or "utf-8"
        return r.text
    finally:
        if own:
            client.close()


def _wechat(html: str, url: str) -> Article:
    soup = BeautifulSoup(html, "lxml")
    title = (soup.select_one("#activity-name") or soup.select_one("h1"))
    author = soup.select_one("#js_name")
    ct = re.search(r'var\s+ct\s*=\s*"(\d{10})"', html) or re.search(r"create_time\s*[:=]\s*['\"]?(\d{10})", html)
    pub, _ = _parse_dt(ct.group(1)) if ct else (None, False)
    body = soup.select_one("#js_content")
    images: list[str] = []
    if body:
        for img in body.select("img"):
            src = img.get("data-src") or img.get("src")
            if src and src.startswith("http"):
                images.append(src)
                img["src"] = src
        text = trafilatura.extract(str(body), output_format="markdown", include_images=True,
                                   include_links=True) or body.get_text("\n", strip=True)
    else:
        text = ""
    return Article(url=url, title=title.get_text(strip=True) if title else "",
                   author=author.get_text(strip=True) if author else "", published_at=pub,
                   text_md=text, images=images, html=html, site="公众号")


def extract(html: str, url: str) -> Article:
    if "mp.weixin.qq.com" in url:
        return _wechat(html, url)
    meta = trafilatura.extract_metadata(html, default_url=url)
    text = trafilatura.extract(html, url=url, output_format="markdown", include_images=True,
                               include_links=True, favor_precision=True) or ""
    images = re.findall(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", text)
    if not images:
        soup = BeautifulSoup(html, "lxml")
        og = soup.find("meta", property="og:image")
        if og and og.get("content", "").startswith("http"):
            images.append(og["content"])
    pub, date_only = _parse_dt(meta.date if meta else None)
    if meta and meta.date and len(meta.date) == 10:
        date_only = True
    return Article(url=url, title=(meta.title if meta else "") or "", author=(meta.author if meta else "") or "",
                   published_at=pub, date_only=date_only, text_md=text, images=images[:12], html=html,
                   site=(meta.sitename if meta else "") or "")


def fetch_article(url: str, client: httpx.Client | None = None) -> Article:
    return extract(fetch_html(url, client), url)


def save_assets(item_id: str, art: Article, with_images: bool = True, client: httpx.Client | None = None,
                max_images: int = 12) -> Path:
    d = config.ASSETS_DIR / item_id
    (d / "images").mkdir(parents=True, exist_ok=True)
    if art.html:
        (d / "source.html").write_text(art.html, encoding="utf-8")
    (d / "content.md").write_text(f"# {art.title}\n\n原文：{art.url}\n\n{art.text_md}", encoding="utf-8")
    saved = []
    if with_images and art.images:
        own = client is None
        client = client or http(30, headers={"Referer": art.url})
        try:
            for i, src in enumerate(art.images[:max_images], 1):
                try:
                    r = client.get(src, headers={"Referer": art.url})
                    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
                        continue
                    ext = r.headers["content-type"].split("/")[-1].split(";")[0].replace("jpeg", "jpg")
                    ext = ext if ext in {"jpg", "png", "gif", "webp"} else "jpg"
                    name = f"{i:02d}-{hashlib.md5(src.encode()).hexdigest()[:8]}.{ext}"
                    (d / "images" / name).write_bytes(r.content)
                    saved.append({"src": src, "file": name, "bytes": len(r.content)})
                except httpx.HTTPError:
                    continue
        finally:
            if own:
                client.close()
    (d / "meta.json").write_text(json.dumps({
        "url": art.url, "title": art.title, "author": art.author,
        "published_at": art.published_at.isoformat() if art.published_at else None,
        "images": saved}, ensure_ascii=False, indent=2), encoding="utf-8")
    return d
