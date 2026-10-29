"""WF-03 选题加工：打分、聚类、去重、选题分与等级；WF-04 选题卡正文。"""
from __future__ import annotations

import math
import re
from datetime import datetime
from statistics import median

from rapidfuzz import fuzz

from .util import fmt_time, short

GRADE_A, GRADE_B = 0.8, 0.6


def density_score(d: dict | None) -> float:
    if not d:
        return 0.0
    total = 0.0
    for k in ("subject", "numbers", "conditions", "traceable"):
        try:
            total += max(0.0, min(2.5, float(d.get(k, 0))))
        except (TypeError, ValueError):
            pass
    return round(total, 2)


def heat_norm(raw: float | None) -> float | None:
    """真实互动数 → 0–100（对数刻度：10≈21，100≈40，1万≈80，10万封顶）。没有就返回 None，不估算。"""
    if raw is None:
        return None
    return round(min(100.0, 20 * math.log10(1 + max(0.0, raw))), 1)


def timeliness(published: datetime | None, at: datetime) -> float:
    if not published:
        return 0.0
    hours = max(0.0, (at - published).total_seconds() / 3600)
    return max(0.0, 1 - hours / 24)


def fill_heat(heats: list[float | None]) -> tuple[list[float | None], str]:
    """本批全部没有热度 → mode=none；部分没有 → 用同批中位数补。"""
    present = [h for h in heats if h is not None]
    if not present:
        return heats, "none"
    med = median(present)
    return [h if h is not None else med for h in heats], "filled" if len(present) < len(heats) else "all"


def topic_score(density: float, heat: float | None, t: float, mode: str) -> float:
    if mode == "none" or heat is None:
        return round((0.5 * density / 10 + 0.2 * t) / 0.7, 3)
    return round(0.5 * density / 10 + 0.3 * heat / 100 + 0.2 * t, 3)


def grade(score: float) -> str:
    return "A" if score >= GRADE_A else "B" if score >= GRADE_B else "C"


def norm_key(s: str | None) -> str:
    return re.sub(r"[\s·・,，。:：\-—_|｜]+", "", (s or "").lower())


def cluster(items: list[dict], threshold: int = 70) -> list[list[dict]]:
    """按「品牌 + 核心事件」聚类：event_key 相同，或同品牌且标题相似。"""
    groups: list[list[dict]] = []
    for it in items:
        key = norm_key(it.get("event_key"))
        brand = set(filter(None, re.split(r"[、,，/]", it.get("brand") or "")))
        placed = False
        for g in groups:
            head = g[0]
            hkey = norm_key(head.get("event_key"))
            hbrand = set(filter(None, re.split(r"[、,，/]", head.get("brand") or "")))
            same_event = key and key == hkey
            similar = (brand & hbrand) and fuzz.token_set_ratio(it.get("title") or "", head.get("title") or "") >= threshold
            if same_event or similar:
                g.append(it)
                placed = True
                break
        if not placed:
            groups.append([it])
    return groups


def is_duplicate_topic(title: str, cluster_key: str, recent: list[dict], threshold: int = 75) -> bool:
    ck = norm_key(cluster_key)
    for r in recent:
        if ck and ck == norm_key(r.get("cluster_key")):
            return True
        if fuzz.token_set_ratio(title or "", r.get("title") or "") >= threshold:
            return True
    return False


def card_markdown(when: datetime, topics: list[dict], others: int, stats_line: str,
                  sheet_url: str | None = None, extra_lines: list[str] | None = None) -> str:
    lines = [f"# 选题卡｜{when.strftime('%Y/%m/%d %H:%M')}", ""]
    if not topics:
        lines += ["本场没有达到上卡标准的选题。", ""]
    for i, t in enumerate(topics, 1):
        tag = t.get("origin", "热点")
        if tag == "日历" and t.get("event_date"):
            tag = f"日历·{t['event_date']}"
        if (t.get("official") or {}).get("media_only"):
            tag += "·媒体信源"
        lines.append(f"## + topic{i}　{t['title']}　〔{t.get('grade') or 'B'}｜{tag}〕")
        lines.append("")
        lines.append(f"选题ID：{t['id']}")
        reasons = t.get("reasons") or []
        if reasons:
            lines.append("")
            lines.append("理由：" + "；".join(reasons[:2]))
        for j, ln in enumerate((t.get("links") or [])[:3], 1):
            lines.append("")
            lines.append(f">> link {j}　{ln.get('author', '—')}｜{ln.get('time', '—')}｜{ln.get('url', '')}")
        off = t.get("official") or {}
        if off.get("doc_url"):
            lines.append("")
            lines.append(f">> 官方原件：[{off.get('doc_title') or '打开'}]({off['doc_url']})")
        elif off.get("dir"):
            lines.append("")
            lines.append(f">> 官方原件：已保存（服务器 {off['dir']}）")
        lines.append("")
    lines.append("---")
    lines.append("")
    sheet = f"[选题登记表]({sheet_url})" if sheet_url else "选题登记表"
    lines.append(f"others：本场另有 {others} 个候选 >> {sheet}")
    lines.append("")
    lines.append(stats_line)
    for ln in extra_lines or []:
        lines.append("")
        lines.append(ln)
    lines.append("")
    lines.append("怎么确认：点页面上的「确认选题」，选选题、生文类型（可多选）和文风；选了「观点金句」就写上你的观点。"
                 f"也可以在{sheet}里勾「确认」。不确认就不出稿。")
    return "\n".join(lines) + "\n"


CONFIRM_RE = re.compile(r"【\s*确认\s*[｜|]\s*([^｜|】]+?)\s*(?:[｜|]\s*(官方资讯|观点金句)\s*)?"
                        r"(?:[｜|]\s*我的观点\s*[:：]\s*([^】]*))?】")
TID_RE = re.compile(r"T\d{8}-\d{2,3}")
TYPES = ("快讯", "长文", "图文")


def parse_card_confirmations(md: str) -> dict[str, dict]:
    """从选题卡笔记里读出你写的【确认｜…】，按所在选题归类。"""
    out: dict[str, dict] = {}
    cur: str | None = None
    for ln in (md or "").split("\n"):
        if ln.strip().startswith("怎么确认"):
            break
        if (m := TID_RE.search(ln)) and "【确认" not in ln:
            cur = m.group(0)
        for m in CONFIRM_RE.finditer(ln):
            tid = (TID_RE.search(ln).group(0) if TID_RE.search(ln) else cur)
            if not tid:
                continue
            types = [t for t in TYPES if t in m.group(1)]
            if not types:
                continue
            opinion = (m.group(3) or "").strip()
            if opinion in ("……", "…", "..."):
                opinion = ""
            out[tid] = {"types": types, "style": m.group(2) or "官方资讯", "opinion": opinion}
    return out


def link_entry(m: dict) -> dict:
    return {"author": m.get("author") or "—", "time": fmt_time(m.get("published_at"), bool(m.get("date_only"))),
            "url": m.get("url") or ""}


def short_name(title: str) -> str:
    t = re.sub(r"[\s《》<>“”\"'：:，,。.!！?？|｜/\\]+", "", title or "")
    return short(t, 12).rstrip("…")
