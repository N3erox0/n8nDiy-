"""各工作流的编排（n8n 只负责定时触发，逻辑都在这里）。

  collect   WF-01 分组采集            06:30/06:45/07:00、16:30/16:45/17:00
  manual    WF-02 手工补全            每 10 分钟
  process   WF-03 选题加工            07:30 / 17:30
  card      WF-04 选题卡（活页）       08:00 / 18:00
  generate  WF-05 图文生成（墨问草稿）  每 5 分钟
  revise    WF-06 改稿与定稿           每 5 分钟
  cleanup   WF-07 清理                每天 00:30
  setup     初始化腾讯文档空间和活页页面（部署时跑一次，可重复跑）
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from rapidfuzz import fuzz

from . import collect, config, db, generate, notify, prompts, revise, topics
from .board import Board, topic_option
from .clients.deepseek import LLM, LLMError
from .clients.mowen import Mowen, MowenError, extract_ast, note_url, updated_at
from .extract import fetch_article, save_assets
from .mowen_doc import atom_to_md, md_to_atom, validate
from .space import Space, as_bool, as_list, first, safe
from .util import fmt_time, normalize_url, now, platform_of, session_of, short, window_of, ymd

log = logging.getLogger("jobs")

PASS_DENSITY = 7.0
CARD_SIZE = 3
MAX_VERSION = 3
KEEP_HOURS = 72
VIDEO_RE = re.compile(r"https?://(?:www\.)?(?:youtube\.com/watch\?v=[\w-]+|youtu\.be/[\w-]+|"
                      r"bilibili\.com/video/[\w]+|player\.vimeo\.com/video/\d+|[^\s\"'<>]+\.mp4)")
MOWEN_IMG_RE = re.compile(r'!\[([^\]]*)\]\(mowen:([^\s)]+)(?:\s+"\w+")?\)')


# ====================================================================== 上下文

class Ctx:
    def __init__(self, c, at: datetime | None = None):
        self.c = c
        self.at = at or now()
        self._llm: LLM | None = None
        self._mowen: Mowen | None = None
        self._space: Space | None = None
        self._board: Board | None = None

    @property
    def llm(self) -> LLM:
        if self._llm is None:
            self._llm = LLM()
            if not self._llm.ready:
                raise LLMError("DEEPSEEK_API_KEY 未填写")
        return self._llm

    @property
    def mowen(self) -> Mowen:
        if self._mowen is None:
            self._mowen = Mowen(quota=quota_hook(self.c))
        return self._mowen

    @property
    def space(self) -> Space:
        if self._space is None:
            self._space = Space(self.c)
        return self._space

    @property
    def board(self) -> Board:
        if self._board is None:
            self._board = Board(self.c)
        return self._board

    def sheet_add(self, table: str, values: dict) -> str | None:
        if not self.space.sheets_ok:
            return None
        return safe(self.space.add_record, table, values)

    def sheet_update(self, table: str, rid: str | None, values: dict) -> None:
        if rid and self.space.sheets_ok:
            safe(self.space.update_record, table, rid, values)

    def mowen_failed(self, e: MowenError) -> None:
        """墨问额度用完：一天只提醒一次（按接口区分，读笔记的额度用完不影响建笔记）。"""
        if e.reason != "QUOTA":
            return
        api = re.search(r"api=(\w+)", str(e))
        key = f"{self.at.date().isoformat()}:{api.group(1) if api else str(e)[:30]}"
        seen = db.kv_get(self.c, "mowen_quota_notified", []) or []
        if key not in seen:
            db.kv_set(self.c, "mowen_quota_notified", (seen + [key])[-20:])
            notify.add(self.c, "错误", f"墨问今日额度用完（{api.group(1) if api else '某接口'}），明天自动恢复")


def quota_hook(c) -> Callable[[str, int], bool]:
    def hook(api: str, limit: int) -> bool:
        row = c.execute(
            "INSERT INTO quota(day,api,n) VALUES(%s,%s,1) ON CONFLICT(day,api) DO UPDATE SET n=quota.n+1 "
            "WHERE quota.n < %s RETURNING n", (now().date(), api, limit)).fetchone()
        return bool(row)
    return hook


def _sources() -> dict[str, dict]:
    return {s["id"]: s for s in collect.load_sources()["sources"]}


def _official_hosts(srcs: dict[str, dict]) -> set[str]:
    hosts = set()
    for s in srcs.values():
        if s.get("official") and s.get("url"):
            h = urlsplit(s["url"]).netloc.lower().removeprefix("www.")
            if h and h not in ("hnrss.org",):
                hosts.add(h)
    return hosts


def _official_handles(srcs: dict[str, dict]) -> set[str]:
    return {("@" + s["handle"]).lower() for s in srcs.values() if s.get("official") and s.get("handle")}


def is_first_hand(m: dict, srcs: dict[str, dict], hosts: set[str], handles: set[str]) -> bool:
    if m.get("source_type") == "手工转存":
        return False
    src = srcs.get(m.get("source_id") or "")
    if src and src.get("official"):
        return True
    host = urlsplit(m.get("url") or "").netloc.lower().removeprefix("www.")
    if host in hosts or any(host.endswith("." + h) for h in hosts):
        return True
    return (m.get("author") or "").lower() in handles


# ====================================================================== WF-01 采集

def job_collect(ctx: Ctx, group: str = "G1") -> dict:
    summary = collect.collect_group(group, ctx.at)
    if ctx.space.sheets_ok:
        rows = ctx.c.execute("SELECT * FROM collect_stats WHERE session=%s AND grp=%s ORDER BY id",
                             (summary["session"], group)).fetchall()
        safe(ctx.space.add_records, "采集统计", [{
            "场次": r["session"], "组": r["grp"], "源": r["source_name"], "方式": r["method"], "状态": r["status"],
            "条目数": r["items"], "本窗口新条目": r["new_items"], "入库数": r["stored"],
            "失败原因": r["reason"] or "", "耗时(秒)": r["secs"]} for r in rows])
    return summary


# ====================================================================== 素材打分与入库

def _title_with_original(orig: str, zh: str) -> str:
    orig, zh = (orig or "").strip(), (zh or "").strip()
    if not zh or zh == orig:
        return orig
    if re.search(r"[\u4e00-\u9fff]", orig):
        return orig
    return f"{zh}（{orig}）"


def score_batch(llm: LLM, items: list[dict]) -> dict[str, dict]:
    payload = [{"id": m["id"], "title": m.get("title") or "", "author": m.get("author") or "",
                "platform": m.get("platform") or "", "published_at": fmt_time(m.get("published_at")),
                "summary": (m.get("summary") or "")[:500], "content": (m.get("content_md") or "")[:1500]}
               for m in items]
    res = llm.chat_json(prompts.load("score"), json.dumps(payload, ensure_ascii=False), temperature=0.1,
                        max_tokens=8000)
    out = {}
    for it in (res.get("items") if isinstance(res, dict) else res) or []:
        if isinstance(it, dict) and it.get("id"):
            out[str(it["id"])] = it
    return out


def apply_score(c, m: dict, s: dict) -> dict:
    dens = topics.density_score(s.get("density"))
    upd = {
        "in_scope": bool(s.get("in_scope", True)), "density": dens,
        "brand": (s.get("brand") or "")[:60], "theme": (s.get("theme") or "")[:30],
        "event_key": (s.get("event_key") or "")[:80], "facts": s.get("facts") or "",
        "quotes": s.get("quotes") or "", "title": _title_with_original(m.get("title"), s.get("title_zh")),
        "summary": (s.get("summary") or m.get("summary") or "")[:300],
    }
    upd["is_key"] = bool(upd["quotes"]) or bool(m.get("is_key"))
    sets = ", ".join(f"{k}=%s" for k in upd)
    c.execute(f"UPDATE materials SET {sets}, updated_at=now() WHERE id=%s", (*upd.values(), m["id"]))
    m.update(upd)
    return m


def material_md(m: dict) -> str:
    lines = [f"原文链接：{m.get('url') or '（手工转存，无链接）'}",
             f"发布人：{m.get('author') or '—'}｜平台：{m.get('platform') or '—'}｜"
             f"发布时间：{fmt_time(m.get('published_at'), bool(m.get('date_only')))}", ""]
    if m.get("facts"):
        lines += ["事实：" + m["facts"], ""]
    if m.get("quotes"):
        lines += ["观点/金句：" + m["quotes"], ""]
    lines += ["---", "", m.get("content_md") or m.get("summary") or ""]
    return "\n".join(lines)


def material_row(m: dict) -> dict:
    return {"素材ID": m["id"], "来源类型": m.get("source_type") or "媒体",
            "原文链接": m.get("url") or None, "平台": m.get("platform") or "", "发布人": m.get("author") or "",
            "发布时间": fmt_time(m.get("published_at"), bool(m.get("date_only"))), "标题": m.get("title") or "",
            "摘要": m.get("summary") or "", "事实": m.get("facts") or "", "观点/金句": m.get("quotes") or "",
            "重点素材": bool(m.get("is_key")), "品牌": m.get("brand") or "", "主题": m.get("theme") or "",
            "事实密度分": m.get("density") or 0, "热度": "—" if m.get("heat") is None else str(m["heat"]),
            "素材文档": (m.get("doc_url"), m.get("doc_url")) if m.get("doc_url") else None,
            "本地备份": m.get("local_dir") or "", "状态": m.get("status") or "候选",
            "选题ID": m.get("topic_id") or "", "备注": m.get("note") or ""}


def _backup(m: dict) -> tuple[str, dict]:
    """抓全文并备份到 assets/<素材ID>/（连图带文）。在线程里跑，不碰数据库。"""
    out: dict[str, Any] = {}
    try:
        art = fetch_article(m["url"])
        d = save_assets(m["id"], art)
        out = {"local_dir": str(d.relative_to(config.ASSETS_DIR.parent)) if d.is_relative_to(config.ASSETS_DIR.parent)
               else str(d), "content_md": art.text_md or m.get("content_md")}
    except Exception as e:  # noqa: BLE001
        out = {"error": f"{type(e).__name__}: {str(e)[:80]}"}
    return m["id"], out


def store_library(ctx: Ctx, mats: list[dict], folder: str = "素材文件夹/订阅") -> None:
    """入库：本地备份 → 腾讯文档素材文档 → 素材收集表。"""
    c = ctx.c
    need = [m for m in mats if m.get("url")]
    with ThreadPoolExecutor(max_workers=6) as ex:
        for mid, res in ex.map(_backup, need):
            m = next(x for x in mats if x["id"] == mid)
            if res.get("local_dir"):
                m["local_dir"] = res["local_dir"]
                if res.get("content_md") and len(res["content_md"]) > len(m.get("content_md") or ""):
                    m["content_md"] = res["content_md"]
                c.execute("UPDATE materials SET local_dir=%s, content_md=%s WHERE id=%s",
                          (m["local_dir"], m.get("content_md"), mid))
    for m in mats:
        if ctx.space.ready and not m.get("doc_id"):
            r = safe(ctx.space.create_doc, folder, f"素材｜{m['id']}｜{short(m.get('title'), 20)}", material_md(m))
            if r and r[0]:
                m["doc_id"], m["doc_url"] = r
                c.execute("UPDATE materials SET doc_id=%s, doc_url=%s WHERE id=%s", (r[0], r[1], m["id"]))
        if not m.get("sheet_record_id"):
            rid = ctx.sheet_add("素材收集", material_row(m))
            if rid:
                m["sheet_record_id"] = rid
                c.execute("UPDATE materials SET sheet_record_id=%s WHERE id=%s", (rid, m["id"]))


# ====================================================================== WF-03 选题加工

def job_process(ctx: Ctx) -> dict:
    c, at = ctx.c, ctx.at
    _, skey = session_of(at)
    srcs = _sources()
    hosts, handles = _official_hosts(srcs), _official_handles(srcs)
    summary: dict[str, Any] = {"session": skey}

    # 1. 打分（本场暂存里还没打过分的）
    staged = c.execute("SELECT * FROM materials WHERE stage='staged' AND session=%s AND density IS NULL "
                       "ORDER BY published_at DESC", (skey,)).fetchall()
    scored = 0
    for i in range(0, len(staged), 12):
        batch = staged[i:i + 12]
        try:
            res = score_batch(ctx.llm, batch)
        except LLMError as e:
            log.warning("打分失败：%s", e)
            continue
        for m in batch:
            if m["id"] in res:
                apply_score(c, m, res[m["id"]])
                scored += 1
    summary["scored"] = scored

    # 2. 过线入库，其余只计数
    rows = c.execute("SELECT * FROM materials WHERE stage='staged' AND session=%s AND density IS NOT NULL",
                     (skey,)).fetchall()
    passed = [m for m in rows if m["in_scope"] and (m["density"] or 0) >= PASS_DENSITY and (m["facts"] or "").strip()]
    dropped = [m["id"] for m in rows if m not in passed]
    if dropped:
        c.execute("UPDATE materials SET stage='dropped' WHERE id = ANY(%s)", (dropped,))
    if passed:
        c.execute("UPDATE materials SET stage='library' WHERE id = ANY(%s)", ([m["id"] for m in passed],))
        store_library(ctx, passed)
    summary.update(passed=len(passed), dropped=len(dropped))

    # 3. 聚类出题（本场入库、还没归到选题的）
    lib = c.execute("SELECT * FROM materials WHERE stage='library' AND session=%s AND topic_id IS NULL "
                    "AND source_type NOT IN ('手工转存')", (skey,)).fetchall()
    recent = c.execute("SELECT title, cluster_key FROM topics WHERE created_at > now() - interval '7 days'").fetchall()
    groups = topics.cluster(lib)
    heats = [max([m["heat"] for m in g if m["heat"] is not None], default=None) for g in groups]
    heats, mode = topics.fill_heat(heats)
    new_topics: list[dict] = []
    clues = 0
    for g, heat in zip(groups, heats):
        # 有一手信源（官网/官方账号）的以它为主素材；没有的也出题，但标「媒体信源」、最高 B 级、不存官方原件
        fh = [m for m in g if is_first_hand(m, srcs, hosts, handles)]
        media_only = not fh
        if media_only:
            clues += 1
        primary = max(fh or g, key=lambda m: (m["density"] or 0))
        others = sorted([m for m in g if m is not primary], key=lambda m: -(m["density"] or 0))
        ck = primary.get("event_key") or primary.get("title") or ""
        try:
            tt = ctx.llm.chat_json(prompts.load("topic"), "\n\n".join(
                f"[{m['id']}] {m.get('title')}\n事实：{m.get('facts')}\n摘要：{m.get('summary')}" for m in [primary] + others[:5]),
                temperature=0.3, max_tokens=1500)
        except LLMError:
            tt = {}
        title = short((tt.get("title") or primary.get("title") or "").strip(), 30)
        if topics.is_duplicate_topic(title, ck, recent):
            continue
        latest = max((m["published_at"] for m in g if m["published_at"]), default=None)
        dens = max(m["density"] or 0 for m in g)
        sc = topics.topic_score(dens, heat, topics.timeliness(latest, at), mode)
        grade = topics.grade(sc)
        if media_only and grade == "A":
            grade = "B"
        t = {"title": title, "reasons": [short(r, 40) for r in (tt.get("reasons") or [])][:2],
             "material_ids": [m["id"] for m in [primary] + others], "cluster_key": ck,
             "links": [topics.link_entry(m) for m in [primary] + others[:2]], "score": sc,
             "grade": grade, "origin": "热点", "primary": None if media_only else primary,
             "media_only": media_only}
        new_topics.append(t)
        recent.append({"title": title, "cluster_key": ck})
    summary["media_only_topics"] = clues

    # 4. 指定选题 + 日历选题
    designated = sync_designated(ctx)
    cal = calendar_topics(ctx, recent)

    # 5. 写库 + 官方原件 + 选题登记表
    created = []
    for t in sorted(new_topics, key=lambda x: -x["score"]) + cal:
        tid = db.next_id(c, "T" + ymd(at), 2, "topics")
        c.execute("""INSERT INTO topics(id,session,origin,title,reasons,material_ids,links,cluster_key,score,grade,
                     status,event_date) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'推荐',%s)""",
                  (tid, skey, t["origin"], t["title"], db.J(t["reasons"]), db.J(t["material_ids"]),
                   db.J(t["links"]), t["cluster_key"], t["score"], t["grade"], t.get("event_date")))
        if t["material_ids"]:
            c.execute("UPDATE materials SET topic_id=%s WHERE id = ANY(%s) AND topic_id IS NULL",
                      (tid, t["material_ids"]))
        t["id"] = tid
        if t.get("media_only"):
            c.execute("UPDATE topics SET official=%s, note=%s WHERE id=%s",
                      (db.J({"media_only": True}), "媒体信源，无官方一手来源", tid))
        else:
            save_official(ctx, t)
        rid = ctx.sheet_add("选题登记", topic_row(c, tid))
        if rid:
            c.execute("UPDATE topics SET sheet_record_id=%s WHERE id=%s", (rid, tid))
        created.append(tid)
    summary.update(topics=created, designated=designated, calendar=len(cal))
    return summary


def topic_row(c, tid: str) -> dict:
    t = c.execute("SELECT * FROM topics WHERE id=%s", (tid,)).fetchone()
    links = "\n".join(f"{ln.get('author')}｜{ln.get('time')}｜{ln.get('url')}" for ln in t["links"] or [])
    off = t["official"] or {}
    return {"选题ID": t["id"], "场次": t["session"] or "", "选题来源": t["origin"], "选题标题": t["title"],
            "理由": "；".join(t["reasons"] or []), "素材链接": links, "选题分": t["score"] or 0,
            "等级": t["grade"] or "", "是否上卡": bool(t["on_card"]), "状态": t["status"],
            "官方原件": (off.get("doc_title") or "官方原件", off["doc_url"]) if off.get("doc_url") else None,
            "推送时间": fmt_time(t["pushed_at"]) if t["pushed_at"] else "", "备注": t["note"] or ""}


def save_official(ctx: Ctx, t: dict) -> None:
    """命中选题的官方原文和图片：服务器 assets/official/<选题ID-短名>/ + 腾讯文档 官方原件/<选题ID-短名>/。视频只登记链接。"""
    c = ctx.c
    m = t.get("primary")
    url = (m or {}).get("url") or t.get("official_url")
    if not url:
        return
    name = f"{t['id']}-{topics.short_name(t['title'])}"
    try:
        art = fetch_article(url)
    except Exception as e:  # noqa: BLE001
        c.execute("UPDATE topics SET note=coalesce(note,'') || %s WHERE id=%s", (f"官方原文抓取失败：{type(e).__name__}；", t["id"]))
        return
    d = save_assets(f"official/{name}", art)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    videos = sorted(set(VIDEO_RE.findall(art.html or "")))[:10]
    src_name = (m or {}).get("author") or art.site or platform_of(url)
    off: dict[str, Any] = {"dir": f"assets/official/{name}", "source_url": url, "title": art.title,
                           "images": [i["file"] for i in meta.get("images", [])],
                           "image_srcs": [i["src"] for i in meta.get("images", [])], "videos": videos,
                           "first_hand": bool(m) and m.get("layer") in ("L1", "L2")}
    if ctx.space.ready:
        doc_title = f"原文｜{short(src_name, 8)}｜{short(art.title, 20)}"
        md = (f"原文链接：{url}\n\n抓取时间：{fmt_time(now())}\n\n"
              + (("官方视频：\n\n" + "\n\n".join(videos) + "\n\n") if videos else "") + "---\n\n" + (art.text_md or ""))
        r = safe(ctx.space.create_doc, f"素材文件夹/官方原件/{name}", doc_title, md)
        if r and r[0]:
            off.update(doc_id=r[0], doc_url=r[1], doc_title=doc_title)
    c.execute("UPDATE topics SET official=%s WHERE id=%s", (db.J(off), t["id"]))
    t["official"] = off


def sync_designated(ctx: Ctx) -> list[str]:
    """选题登记表里你新加的「指定」行：补选题ID、素材和理由，状态「推荐」。"""
    if not ctx.space.sheets_ok:
        return []
    c = ctx.c
    out = []
    for r in safe(ctx.space.list_records, "选题登记") or []:
        v = r["values"]
        if first(v.get("选题来源")) != "指定" or (v.get("选题ID") or "").strip() or not (v.get("选题标题") or "").strip():
            continue
        title = v["选题标题"].strip()
        lib = c.execute("SELECT * FROM materials WHERE stage='library' AND created_at > now() - interval '7 days'").fetchall()
        hits = sorted([m for m in lib if fuzz.token_set_ratio(title, m.get("title") or "") >= 60],
                      key=lambda m: -(m["density"] or 0))[:4]
        reasons = [x for x in re.split(r"[；;\n]", v.get("理由") or "") if x.strip()][:2]
        if not reasons and hits:
            try:
                tt = ctx.llm.chat_json(prompts.load("topic"), "\n\n".join(
                    f"[{m['id']}] {m.get('title')}\n事实：{m.get('facts')}" for m in hits), temperature=0.3, max_tokens=1500)
                reasons = [short(x, 40) for x in tt.get("reasons") or []][:2]
            except LLMError:
                pass
        tid = db.next_id(c, "T" + ymd(ctx.at), 2, "topics")
        c.execute("""INSERT INTO topics(id,session,origin,title,reasons,material_ids,links,cluster_key,score,grade,
                     status,sheet_record_id) VALUES(%s,'指定','指定',%s,%s,%s,%s,%s,1,'A','推荐',%s)""",
                  (tid, title, db.J(reasons), db.J([m["id"] for m in hits]),
                   db.J([topics.link_entry(m) for m in hits[:3]]), "指定:" + title, r["record_id"]))
        t = {"id": tid, "title": title, "primary": hits[0] if hits else None}
        if hits:
            save_official(ctx, t)
        ctx.sheet_update("选题登记", r["record_id"], {"选题ID": tid, "场次": "指定", "理由": "；".join(reasons),
                                                    "素材链接": "\n".join(m["url"] for m in hits if m.get("url")),
                                                    "状态": "推荐"})
        # 你直接勾了确认的，交给 WF-05 处理
        out.append(tid)
    return out


def calendar_topics(ctx: Ctx, recent: list[dict]) -> list[dict]:
    if not ctx.space.sheets_ok:
        return []
    today = ctx.at.date()
    out = []
    for r in safe(ctx.space.list_records, "选题日历") or []:
        v = r["values"]
        raw = (v.get("事件日期") or "").strip().replace("/", "-").replace(".", "-")
        try:
            d = datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if d != today or not v.get("事件"):
            continue
        ck = f"日历:{d}:{v['事件']}"
        if any(x.get("cluster_key") == ck for x in recent):
            continue
        out.append({"title": short(v["事件"], 30), "reasons": [x for x in [v.get("品牌·产品"), v.get("备注")] if x][:2],
                    "material_ids": [], "cluster_key": ck, "links": [{"author": v.get("品牌·产品") or "—",
                                                                      "time": str(d), "url": v.get("官方链接") or ""}],
                    "score": 0.7, "grade": "B", "origin": "日历", "event_date": d,
                    "official_url": v.get("官方链接") or None})
        if len(out) >= 2:
            break
    return out


# ====================================================================== WF-04 选题卡

def _wait_lock(c, name: str, minutes: int = 25) -> None:
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        if db.try_lock(c, name):
            db.unlock(c, name)
            return
        time.sleep(20)


def failing_sources_line(c) -> str | None:
    rows = c.execute("""SELECT source_name, count(*) n, bool_and(status='失败') all_fail FROM collect_stats
                        WHERE created_at > now() - interval '7 days' GROUP BY source_name""").fetchall()
    bad = [r["source_name"] for r in rows if r["all_fail"] and r["n"] >= 7]
    if not bad:
        return None
    return "连续 7 天失败的源：" + "、".join(sorted(bad)[:30])


def job_card(ctx: Ctx) -> dict:
    c, at = ctx.c, ctx.at
    _wait_lock(c, "job:process")
    _, skey = session_of(at)
    cands = c.execute("""SELECT * FROM topics WHERE status='推荐' AND NOT on_card AND
                         (session=%s OR origin='指定' OR (origin='日历' AND event_date=%s))""",
                      (skey, at.date())).fetchall()
    order = {"指定": 0, "日历": 1, "热点": 2}
    cands.sort(key=lambda t: (order.get(t["origin"], 3), -(t["score"] or 0)))
    picked, n_cal = [], 0
    for t in cands:
        if t["origin"] == "日历":
            if n_cal >= 2:
                continue
            n_cal += 1
        if t["origin"] == "热点" and t["grade"] == "C":
            continue
        picked.append(t)
        if len(picked) >= CARD_SIZE:
            break
    others = len(cands) - len(picked)
    extra = []
    if at.weekday() == 6 and at.hour >= 12:
        if (ln := failing_sources_line(c)):
            extra.append(ln)
    md = topics.card_markdown(at.replace(minute=0 if at.minute < 30 else 30, second=0), picked, others,
                              collect.stats_line(c, skey), ctx.space.sheet_url("选题登记"), extra)
    title = f"选题卡｜{at.strftime('%Y/%m/%d %H:%M')}"
    archive = None
    if ctx.space.ready:
        archive = safe(ctx.space.create_doc, "选题卡", title, md)
    # 表单可选：本卡选题 + 72 小时内其他推荐选题
    pool = c.execute("SELECT id,title FROM topics WHERE status='推荐' AND created_at > now() - interval '72 hours' "
                     "ORDER BY created_at DESC LIMIT 40").fetchall()
    ids = [t["id"] for t in picked]
    options = [topic_option(t) for t in picked] + [topic_option(t) for t in pool if t["id"] not in ids]
    pub = None
    if ctx.board.ready:
        pub = ctx.board.publish_card(md, options[:50], note=f"{len(picked)} 个选题")
    if picked:
        c.execute("UPDATE topics SET on_card=TRUE, pushed_at=now() WHERE id = ANY(%s)", (ids,))
        for t in picked:
            ctx.sheet_update("选题登记", t["sheet_record_id"], {"是否上卡": True, "推送时间": fmt_time(now())})
    return {"session": skey, "picked": ids, "others": others, "archive": archive and archive[1],
            "card_url": ctx.board.url("card") if ctx.board.ready else None,
            "publish_status": (pub or {}).get("status")}


# ====================================================================== WF-05 生文

def collect_confirms(ctx: Ctx) -> list[dict]:
    out: list[dict] = []
    if ctx.board.ready:
        out += safe(ctx.board.read_confirms) or []
    if ctx.space.sheets_ok:
        sync_designated(ctx)
        for r in safe(ctx.space.list_records, "选题登记") or []:
            v = r["values"]
            if not as_bool(v.get("确认")) or first(v.get("状态")) != "推荐" or not (v.get("选题ID") or "").strip():
                continue
            types = [t for t in ("快讯", "长文", "图文") if t in as_list(v.get("生文类型"))]
            out.append({"tid": v["选题ID"].strip(), "types": types, "style": first(v.get("文风")) or "官方资讯",
                        "opinion": (v.get("我的观点") or "").strip(), "record_id": r["record_id"]})
    return out


def _quotes_for(c, topic: dict) -> str:
    rows = c.execute("""SELECT quotes, brand, theme FROM materials WHERE is_key AND coalesce(quotes,'')<>''
                        AND created_at > now() - interval '14 days' ORDER BY created_at DESC LIMIT 60""").fetchall()
    mats = c.execute("SELECT brand, theme FROM materials WHERE id = ANY(%s)", (topic["material_ids"] or [],)).fetchall()
    keys = {x for m in mats for x in re.split(r"[、,，]", (m["brand"] or "") + "、" + (m["theme"] or "")) if x}
    hit = [r["quotes"] for r in rows if keys & set(re.split(r"[、,，]", (r["brand"] or "") + "、" + (r["theme"] or "")))]
    return "\n".join((hit or [r["quotes"] for r in rows])[:5])


def _official_image(ctx: Ctx, topic: dict) -> str | None:
    """快讯用的官方图：从官方原件备份里取第一张，上传墨问，返回 fileId。"""
    off = topic.get("official") or {}
    if not (off.get("first_hand") and off.get("images")):
        return None
    path = config.ASSETS_DIR / "official" / Path(off["dir"]).name / "images" / off["images"][0]
    try:
        return ctx.mowen.upload_file(path)
    except Exception as e:  # noqa: BLE001
        log.info("本地上传官方图失败，改用链接上传：%s", e)
    try:
        return ctx.mowen.upload_url(off["image_srcs"][0])
    except Exception as e:  # noqa: BLE001
        log.info("官方图上传失败：%s", e)
        return None


def job_generate(ctx: Ctx) -> dict:
    c = ctx.c
    done: list[str] = []
    skipped: list[str] = []
    for cf in collect_confirms(ctx):
        t = c.execute("SELECT * FROM topics WHERE id=%s", (cf["tid"],)).fetchone()
        if not t:
            skipped.append(f"{cf['tid']}：找不到选题")
            continue
        if t["status"] != "推荐":
            if cf.get("record_id") is None:  # 活页表单重复提交
                notify.add(c, "提示", f"{t['id']} 已是「{t['status']}」，这次确认没有重复出稿")
            continue
        types = cf["types"]
        style = cf["style"] if cf["style"] in ("官方资讯", "观点金句") else "官方资讯"
        if not types:
            ctx.sheet_update("选题登记", cf.get("record_id") or t["sheet_record_id"], {"备注": "缺生文类型"})
            skipped.append(f"{t['id']}：缺生文类型")
            continue
        if style == "观点金句" and not cf["opinion"] and any(x != "快讯" for x in types):
            c.execute("UPDATE topics SET note='缺观点' WHERE id=%s", (t["id"],))
            ctx.sheet_update("选题登记", cf.get("record_id") or t["sheet_record_id"], {"备注": "缺观点", "状态": "推荐"})
            notify.add(c, "提示", f"{short(t['title'], 20)}：选了观点金句但没写观点，未出稿")
            skipped.append(f"{t['id']}：缺观点")
            continue
        # 抢占：先改成生成中，防止重复出稿
        got = c.execute("UPDATE topics SET status='生成中', confirmed=TRUE, types=%s, style=%s, opinion=%s, "
                        "updated_at=now() WHERE id=%s AND status='推荐' RETURNING id",
                        (db.J(types), style, cf["opinion"], t["id"])).fetchone()
        if not got:
            continue
        rid = cf.get("record_id") or t["sheet_record_id"]
        ctx.sheet_update("选题登记", rid, {"状态": "生成中", "确认": True, "生文类型": types, "文风": style,
                                         "我的观点": cf["opinion"]})
        t = c.execute("SELECT * FROM topics WHERE id=%s", (t["id"],)).fetchone()
        ok = 0
        for dtype in [x for x in ("快讯", "长文", "图文") if x in types]:
            try:
                did = make_draft(ctx, t, dtype, "官方资讯" if dtype == "快讯" else style, cf["opinion"])
                done.append(did)
                ok += 1
            except MowenError as e:
                ctx.mowen_failed(e)
                notify.add(c, "错误", f"{short(t['title'], 16)}·{dtype} 写墨问失败：{e.reason}")
            except Exception as e:  # noqa: BLE001
                log.exception("生成失败")
                notify.add(c, "错误", f"{short(t['title'], 16)}·{dtype} 生成失败：{type(e).__name__}")
        status = "已出稿" if ok else "失败"
        c.execute("UPDATE topics SET status=%s, updated_at=now() WHERE id=%s", (status, t["id"]))
        ctx.sheet_update("选题登记", rid, {"状态": status})
        if ok:
            c.execute("UPDATE materials SET status='已用' WHERE id = ANY(%s)", (t["material_ids"] or [],))
    return {"drafts": done, "skipped": skipped}


def make_draft(ctx: Ctx, t: dict, dtype: str, style: str, opinion: str) -> str:
    c = ctx.c
    mats = c.execute("SELECT * FROM materials WHERE id = ANY(%s) ORDER BY density DESC NULLS LAST",
                     (t["material_ids"] or [],)).fetchall()
    off = t["official"] or {}
    official_md = None
    if off.get("dir"):
        p = config.ASSETS_DIR / "official" / Path(off["dir"]).name / "content.md"
        if p.exists():
            official_md = p.read_text(encoding="utf-8")
    quotes = _quotes_for(c, t) if style == "观点金句" else ""
    has_img = bool(off.get("first_hand") and off.get("images")) and dtype == "快讯"
    res = generate.run_pipeline(ctx.llm, t, dtype, style, mats, official_md, opinion, quotes, has_img)
    body = res.body_md
    if "【官方图】" in body:
        fid = _official_image(ctx, t) if has_img else None
        body = body.replace("【官方图】", f'![官方图](mowen:{fid} "center")' if fid else "")
        body = re.sub(r"\n{3,}", "\n\n", body)
    full_body = f"# {res.title}\n\n{body.strip()}\n"
    md = revise.compose(full_body, res.appendix_md)
    atom = md_to_atom(md)
    if (probs := validate(atom)):
        raise ValueError("正文格式检查没通过：" + "；".join(probs))
    nid = ctx.mowen.create(atom)
    safe(ctx.mowen.tags, nid, add=["草稿"])
    url = note_url(nid)
    did = f"{t['id']}-{dtype}"
    if c.execute("SELECT 1 FROM drafts WHERE id=%s", (did,)).fetchone():
        n = c.execute("SELECT count(*) n FROM drafts WHERE id LIKE %s", (did + "%",)).fetchone()["n"]
        did = f"{did}-{n + 1}"
    exp = now() + timedelta(hours=KEEP_HOURS)
    c.execute("""INSERT INTO drafts(id,topic_id,dtype,style,title,mowen_note_id,mowen_url,version,version_label,status,
                 words,image_slots,check_result,last_seen_updated,expires_at)
                 VALUES(%s,%s,%s,%s,%s,%s,%s,1,'草稿1','待确认',%s,%s,%s,%s,%s)""",
              (did, t["id"], dtype, style, res.title, nid, url, res.words, res.image_slots, res.check["line"],
               0, exp))
    c.execute("""INSERT INTO draft_versions(draft_id,version,label,md,appendix_md,atom,fact_base,check_json)
                 VALUES(%s,1,'草稿1',%s,%s,%s,%s,%s)""",
              (did, full_body, res.appendix_md, db.J(atom), res.fact_base, db.J(res.check)))
    rid = ctx.sheet_add("发文登记", draft_row(c, did))
    if rid:
        c.execute("UPDATE drafts SET sheet_record_id=%s WHERE id=%s", (rid, did))
    notify.add(c, "新草稿", f"新草稿·{dtype}：{short(res.title, 24)}", url)
    return did


def draft_row(c, did: str) -> dict:
    d = c.execute("SELECT * FROM drafts WHERE id=%s", (did,)).fetchone()
    return {"稿ID": d["id"], "选题ID": d["topic_id"], "标题": d["title"] or "", "类型": d["dtype"],
            "文风": d["style"] or "", "版本": d["version_label"], "墨问链接": (d["mowen_url"], d["mowen_url"]),
            "墨问笔记ID": d["mowen_note_id"] or "", "字数": d["words"] or 0, "配图位数": d["image_slots"] or 0,
            "核对结果": d["check_result"] or "", "状态": d["status"], "生成时间": fmt_time(d["updated_at"]),
            "过期时间": fmt_time(d["expires_at"]) if d["expires_at"] else "",
            "定稿链接": (d["final_url"], d["final_url"]) if d["final_url"] else None}


# ====================================================================== WF-06 改稿与定稿

def collect_signals(ctx: Ctx) -> dict[str, dict]:
    """两个入口合并：发文登记表「状态」列 < 活页改稿台表单（同一稿以活页为准）。

    不轮询墨问：note/info 每天只有 10 次免费额度（10-29 实测），只在收到信号后读一次笔记。
    """
    sig: dict[str, dict] = {}
    if ctx.space.sheets_ok:
        for r in safe(ctx.space.list_records, "发文登记") or []:
            v = r["values"]
            st, did = first(v.get("状态")), (v.get("稿ID") or "").strip()
            if st in ("待修改", "确认", "放弃") and did:
                sig[did] = {"action": st, "note": (v.get("修改意见") or "").strip(), "via": "表格"}
    if ctx.board.ready:
        for s in safe(ctx.board.read_desk) or []:
            sig[s["draft_id"]] = {"action": s["action"], "note": s["note"], "via": "活页"}
    return sig


def job_revise(ctx: Ctx) -> dict:
    """墨问额度用完时，信号存进 kv['revise_pending']，第二天额度恢复后自动接着处理，不用你重点。"""
    c = ctx.c
    pending: dict[str, dict] = db.kv_get(c, "revise_pending", {}) or {}
    sig = {**pending, **collect_signals(ctx)}
    if not sig:
        return {}
    active = {d["id"]: d for d in c.execute(
        "SELECT * FROM drafts WHERE status IN ('待确认','待修改') AND id = ANY(%s)", (list(sig),)).fetchall()}
    out: dict[str, str] = {}
    keep: dict[str, dict] = {}
    for did, s in sig.items():
        d = active.get(did)
        if not d:
            continue  # 稿件不存在或已定稿/已放弃/正在改
        try:
            if s["action"] == "待修改":
                out[did] = do_revise(ctx, d, s.get("note") or "")
            elif s["action"] == "确认":
                out[did] = do_finalize(ctx, d)
            elif s["action"] == "放弃":
                out[did] = do_abandon(ctx, d)
        except MowenError as e:
            c.execute("UPDATE drafts SET status=%s WHERE id=%s AND status='修改中'", (d["status"], did))
            if e.reason == "QUOTA":
                keep[did] = s
                out[did] = "墨问额度用完，明天自动处理"
                if did not in pending:
                    notify.add(c, "提示", f"{short(d['title'], 16)}「{s['action']}」已收到，墨问今天额度用完，明天自动处理",
                               d["mowen_url"])
                continue
            out[did] = f"墨问失败：{e}"
            _set_draft(ctx, did, status="待确认", note=f"墨问失败：{e.reason}")
            notify.add(c, "错误", f"{short(d['title'], 16)}「{s['action']}」没处理：墨问 {e.reason}", d["mowen_url"])
        except Exception as e:  # noqa: BLE001
            log.exception("改稿失败")
            out[did] = f"失败：{type(e).__name__}"
            _set_draft(ctx, did, status="待确认", note=f"处理失败：{e}"[:200])
            notify.add(c, "错误", f"{short(d['title'], 16)}「{s['action']}」处理失败：{type(e).__name__}", d["mowen_url"])
    if keep != pending:
        db.kv_set(c, "revise_pending", keep)
    # 改稿台表单的稿件选项跟着刷新
    if any(not v.startswith("墨问额度") for v in out.values()) and ctx.board.ready:
        safe(ctx.board.update_desk_form, notify.active_drafts(c))
    return out


def _set_draft(ctx: Ctx, did: str, **kw) -> None:
    sets = ", ".join(f"{k}=%s" for k in kw)
    ctx.c.execute(f"UPDATE drafts SET {sets}, updated_at=now() WHERE id=%s", (*kw.values(), did))
    d = ctx.c.execute("SELECT sheet_record_id FROM drafts WHERE id=%s", (did,)).fetchone()
    row = draft_row(ctx.c, did)
    row["修改意见"] = ""
    ctx.sheet_update("发文登记", d["sheet_record_id"], row)


def _latest_version(c, did: str) -> dict:
    return c.execute("SELECT * FROM draft_versions WHERE draft_id=%s ORDER BY version DESC, id DESC LIMIT 1",
                     (did,)).fetchone() or {}


def do_revise(ctx: Ctx, d: dict, overall: str, info: dict | None = None) -> str:
    c = ctx.c
    if d["version"] >= MAX_VERSION:
        _set_draft(ctx, d["id"], status="待确认", note=f"已到第 {MAX_VERSION} 版，请手动修改后确认")
        notify.add(c, "提示", f"{short(d['title'], 20)} 已到第 {MAX_VERSION} 版，请在墨问里手动改完后选「确认」",
                   d["mowen_url"])
        return "已到上限"
    _set_draft(ctx, d["id"], status="修改中", revise_note=overall or None)
    info = info or ctx.mowen.info(d["mowen_note_id"])
    ast = extract_ast(info)
    if (probs := validate(ast or {})):
        _set_draft(ctx, d["id"], status="待确认", note="墨问正文里有系统写不回去的内容：" + "；".join(probs))
        notify.add(c, "提示", f"{short(d['title'], 16)}：正文里有多图/视频等写不回的内容，请删掉后再改", d["mowen_url"])
        return "含不支持节点"
    prev = _latest_version(c, d["id"])
    ri = revise.analyze(prev.get("md") or "", atom_to_md(ast))
    if not ri.has_changes and not overall:
        _set_draft(ctx, d["id"], status="待确认", note="未发现修改内容")
        notify.add(c, "提示", f"{short(d['title'], 20)}：没读到修改内容，状态改回待确认", d["mowen_url"])
        return "未发现修改"
    n = len(ri.body_blocks)
    protected = set(ri.user_edited)
    allowed = (set(range(n)) - protected) if overall else {i.idx for i in ri.instructions}
    plan = generate.revise_with_llm(ctx.llm, prev.get("fact_base") or "", revise.numbered(ri.body_blocks),
                                    [{"idx": i.idx, "kind": i.kind, "text": i.text} for i in ri.instructions],
                                    sorted(protected), overall)
    new_blocks, changed = revise.apply_plan(ri.body_blocks, plan, allowed)
    for i in changed:
        if i < len(new_blocks) and not revise.LOCKED_RE.match(new_blocks[i]):
            try:
                chk = generate.check_blocks(ctx.llm, prev.get("fact_base") or "", new_blocks[i])
                if (txt := (chk.get("text") or "").strip()):
                    new_blocks[i] = txt
            except LLMError:
                pass
    log_items = list(plan.get("log") or [])
    if protected:
        log_items.append({"status": "保留", "what": f"你直接改过的 {len(protected)} 处原样保留"})
    if ri.images_added:
        log_items.append({"status": "保留", "what": f"你插入的 {ri.images_added} 张图片"})
    ver = d["version"] + 1
    label = f"草稿{ver}"
    body = revise.join_blocks(new_blocks)
    md = revise.compose(body, prev.get("appendix_md") or "", revise.changelog_md(label, log_items))
    atom = md_to_atom(md)
    if (probs := validate(atom)):
        raise ValueError("改后正文格式检查没通过：" + "；".join(probs))
    ctx.mowen.edit(d["mowen_note_id"], atom)
    c.execute("""INSERT INTO draft_versions(draft_id,version,label,md,appendix_md,atom,fact_base,check_json,change_log)
                 VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
              (d["id"], ver, label, body, prev.get("appendix_md"), db.J(atom), prev.get("fact_base"),
               db.J(prev.get("check_json") or {}), db.J(log_items)))
    _set_draft(ctx, d["id"], version=ver, version_label=label, status="待确认", revise_note=None, note=None,
               expires_at=now() + timedelta(hours=KEEP_HOURS))
    notify.add(c, "改稿", f"{label} 已更新：{short(d['title'], 22)}", d["mowen_url"])
    return label


def _final_markdown(md: str, info: dict, mowen_url: str) -> str:
    body, appendix = revise.split_sections(md)
    body = "\n\n".join(b for b in revise.blocks(body) if not revise.STATUS_RE.match(b))
    html = (((info.get("info") or {}).get("content") or {}).get("html")) or ""
    srcs = re.findall(r"<img[^>]+src=\"([^\"]+)\"", html)
    k = iter(srcs)

    def img(m: re.Match) -> str:
        src = next(k, None)
        if src and src.startswith("http"):
            return f"![{m.group(1)}]({src})"
        return f"【配图见墨问：{mowen_url}】"
    body = MOWEN_IMG_RE.sub(img, body)
    tail = f"\n\n---\n\n墨问原稿：{mowen_url}（配图以墨问那一版为准）\n"
    if appendix:
        tail += "\n" + revise.APPENDIX_H + "\n\n" + appendix + "\n"
    return body.strip() + tail


def do_finalize(ctx: Ctx, d: dict, info: dict | None = None) -> str:
    c = ctx.c
    info = info or ctx.mowen.info(d["mowen_note_id"])
    md = atom_to_md(extract_ast(info))
    final_md = _final_markdown(md, info, d["mowen_url"])
    t = c.execute("SELECT title FROM topics WHERE id=%s", (d["topic_id"],)).fetchone() or {"title": d["title"]}
    title = f"{ymd(d['created_at'])}-{topics.short_name(t['title'])}-{d['dtype']}-定稿"
    doc = None
    if ctx.space.ready:
        doc = safe(ctx.space.create_doc, "发稿文件夹", title, re.sub(r"^# .*\n+", "", final_md, count=1))
    safe(ctx.mowen.tags, d["mowen_note_id"], add=["定稿"], remove=["草稿"])
    body, appendix = revise.split_sections(md)
    c.execute("""INSERT INTO draft_versions(draft_id,version,label,md,appendix_md,fact_base)
                 SELECT %s, %s, '定稿', %s, %s, fact_base FROM draft_versions WHERE draft_id=%s
                 ORDER BY version DESC LIMIT 1""", (d["id"], d["version"], body, appendix, d["id"]))
    _set_draft(ctx, d["id"], status="已定稿", version_label="定稿", final_doc_id=doc and doc[0],
               final_url=doc and doc[1], last_seen_updated=updated_at(info), expires_at=None)
    notify.add(c, "定稿", f"已定稿：{short(d['title'], 24)}", (doc and doc[1]) or d["mowen_url"])
    return "已定稿"


def do_abandon(ctx: Ctx, d: dict) -> str:
    ctx.mowen.edit(d["mowen_note_id"], md_to_atom(f"# {d['title']}（已放弃）\n\n本稿已放弃。原稿在系统里保留 72 小时。\n"))
    safe(ctx.mowen.tags, d["mowen_note_id"], add=["已放弃"], remove=["草稿"])
    _set_draft(ctx, d["id"], status="已放弃", expires_at=now() + timedelta(hours=KEEP_HOURS))
    notify.add(ctx.c, "提示", f"已放弃：{short(d['title'], 24)}")
    return "已放弃"


# ====================================================================== WF-02 手工补全

def job_inbox(ctx: Ctx) -> dict:
    """活页「收素材」页面的提交：链接 → 链接入表；只有内容 → 手工转存。结果推到改稿台。"""
    if not ctx.board.ready:
        return {}
    c = ctx.c
    done, failed = [], []
    for s in safe(ctx.board.read_inbox) or []:
        v = {"原文链接": s["url"], "发布人": s["author"], "备注": s["text"], "标题": ""}
        mid = db.next_id(c, "S" + ymd(ctx.at), 3, "materials")
        label = short(s["url"] or s["text"], 30)
        ex = None
        if s["url"] and (u := normalize_url(s["url"])):
            ex = c.execute("SELECT * FROM materials WHERE url_norm=%s", (u,)).fetchone()
        if ex and ex["stage"] == "library":
            notify.add(c, "素材", f"已在素材库里：{short(ex['title'], 26)}（{ex['id']}）", ex.get("doc_url") or ex["url"])
            continue
        try:
            if ex:  # 订阅采到过但没过线：你点名要的，直接入库
                c.execute("UPDATE materials SET stage='library', source_type='链接入表' WHERE id=%s", (ex["id"],))
                m = c.execute("SELECT * FROM materials WHERE id=%s", (ex["id"],)).fetchone()
                if m["density"] is None and (res := score_batch(ctx.llm, [m])).get(m["id"]):
                    apply_score(c, m, res[m["id"]])
            else:
                m = _manual_link(ctx, mid, v) if s["url"] else _manual_text(ctx, mid, v)
        except Exception as e:  # noqa: BLE001
            m = f"抓取失败：{type(e).__name__}"
        if isinstance(m, str):
            failed.append(label)
            if m.startswith("缺发布时间") and ctx.space.sheets_ok:
                ctx.sheet_add("素材收集", {"原文链接": s["url"], "来源类型": "链接入表", "状态": "待补全",
                                         "发布人": s["author"], "重点素材": s["is_key"],
                                         "备注": "抓不到发布时间：填上「发布时间」（YYYY-MM-DD HH:MM），状态改回「待补全」"})
                m = "抓不到发布时间，已放进素材收集表，补上时间即可"
            notify.add(c, "提示", f"素材没存上：{m[:34]}｜{label}", ctx.space.sheet_url("素材收集") or s["url"] or None)
            continue
        c.execute("UPDATE materials SET is_key=%s, note=%s WHERE id=%s",
                  (s["is_key"] or bool(m.get("quotes")), s["note"] or None, m["id"]))
        m = c.execute("SELECT * FROM materials WHERE id=%s", (m["id"],)).fetchone()
        store_library(ctx, [m], "素材文件夹/手工")
        m = c.execute("SELECT * FROM materials WHERE id=%s", (m["id"],)).fetchone()
        done.append(m["id"])
        notify.add(c, "素材", f"已入素材库：{short(m['title'], 26)}（密度 {m['density'] or 0:g}）",
                   m.get("doc_url") or m.get("url"))
    return {"done": done, "failed": failed}


def job_manual(ctx: Ctx) -> dict:
    c = ctx.c
    done, failed = [], []
    inbox = job_inbox(ctx)
    if not ctx.space.sheets_ok:
        return {"inbox": inbox, "skipped": "素材收集表未初始化"}
    for r in safe(ctx.space.list_records, "素材收集") or []:
        v = r["values"]
        if first(v.get("状态")) != "待补全":
            continue
        kind = first(v.get("来源类型")) or ("链接入表" if v.get("原文链接") else "手工转存")
        mid = db.next_id(c, "S" + ymd(ctx.at), 3, "materials")
        try:
            if kind == "链接入表":
                m = _manual_link(ctx, mid, v)
            else:
                m = _manual_text(ctx, mid, v)
        except Exception as e:  # noqa: BLE001
            ctx.sheet_update("素材收集", r["record_id"], {"状态": "失效", "备注": f"{v.get('备注') or ''} 抓取失败：{str(e)[:60]}"})
            failed.append(r["record_id"])
            continue
        if isinstance(m, str):
            ctx.sheet_update("素材收集", r["record_id"], {"状态": "失效", "备注": m})
            failed.append(r["record_id"])
            continue
        m["sheet_record_id"] = r["record_id"]
        c.execute("UPDATE materials SET sheet_record_id=%s WHERE id=%s", (r["record_id"], m["id"]))
        store_library(ctx, [m], "素材文件夹/手工")
        m = c.execute("SELECT * FROM materials WHERE id=%s", (m["id"],)).fetchone()
        row = material_row(m)
        row["来源类型"] = kind
        ctx.sheet_update("素材收集", r["record_id"], row)
        done.append(m["id"])
    return {"inbox": inbox, "done": done, "failed": len(failed)}


def _manual_link(ctx: Ctx, mid: str, v: dict) -> dict | str:
    c = ctx.c
    url = normalize_url(v.get("原文链接") or "")
    if not url:
        return "链接无效（短链或跳转链接请换成原文链接）"
    if (ex := c.execute("SELECT id FROM materials WHERE url_norm=%s", (url,)).fetchone()):
        return f"重复：已有 {ex['id']}"
    art = fetch_article(url)
    pub = art.published_at
    if not pub and (v.get("发布时间") or "").strip():
        try:
            pub = datetime.strptime(v["发布时间"].strip()[:16], "%Y-%m-%d %H:%M").replace(tzinfo=config.TZ)
        except ValueError:
            pass
    if not pub:
        return "缺发布时间：请在「发布时间」填 YYYY-MM-DD HH:MM，再把状态改回待补全"
    author = art.author or (v.get("发布人") or "").strip() or art.site or platform_of(url)
    c.execute("""INSERT INTO materials(id,stage,source_id,source_type,layer,url,url_norm,platform,author,published_at,
                 date_only,title,summary,content_md,status,session,is_key)
                 VALUES(%s,'library','manual','链接入表','MANUAL',%s,%s,%s,%s,%s,%s,%s,%s,%s,'候选',%s,TRUE)""",
              (mid, url, url, platform_of(url), author, pub, art.date_only, art.title, (art.text_md or "")[:300],
               art.text_md, session_of(ctx.at)[1]))
    m = c.execute("SELECT * FROM materials WHERE id=%s", (mid,)).fetchone()
    res = score_batch(ctx.llm, [m])
    if mid in res:
        apply_score(c, m, res[mid])
    return m


def _manual_text(ctx: Ctx, mid: str, v: dict) -> dict | str:
    c = ctx.c
    text = (v.get("备注") or "").strip()
    if not text:
        return "手工转存需要把内容贴在「备注」里"
    c.execute("""INSERT INTO materials(id,stage,source_id,source_type,layer,platform,author,published_at,title,summary,
                 content_md,status,session,is_key) VALUES(%s,'library','manual','手工转存','MANUAL','手工',%s,%s,%s,%s,%s,
                 '候选',%s,TRUE)""",
              (mid, (v.get("发布人") or "我").strip(), now(), short(v.get("标题") or text.split("\n")[0], 40),
               text[:300], text, session_of(ctx.at)[1]))
    m = c.execute("SELECT * FROM materials WHERE id=%s", (mid,)).fetchone()
    res = score_batch(ctx.llm, [m])
    if mid in res:
        apply_score(c, m, res[mid])
    return m


# ====================================================================== WF-07 清理

def job_cleanup(ctx: Ctx) -> dict:
    c = ctx.c
    out: dict[str, Any] = {}
    out["versions_deleted"] = c.execute(
        """DELETE FROM draft_versions v WHERE v.created_at < now() - interval '72 hours'
           AND v.label <> '定稿'
           AND v.id <> (SELECT id FROM draft_versions x WHERE x.draft_id=v.draft_id ORDER BY version DESC, id DESC LIMIT 1)
        """).rowcount
    stale = c.execute("""UPDATE topics SET status='已作废', updated_at=now() WHERE status='推荐' AND NOT confirmed
                         AND created_at < now() - interval '72 hours' RETURNING id, sheet_record_id, material_ids""").fetchall()
    for t in stale:
        c.execute("UPDATE materials SET topic_id=NULL, status='候选' WHERE topic_id=%s", (t["id"],))
        ctx.sheet_update("选题登记", t["sheet_record_id"], {"状态": "已作废"})
    out["topics_expired"] = len(stale)
    out["staged_deleted"] = c.execute("DELETE FROM materials WHERE stage IN ('staged','dropped') "
                                      "AND created_at < now() - interval '7 days'").rowcount
    c.execute("DELETE FROM notices WHERE created_at < now() - interval '30 days'")
    c.execute("DELETE FROM job_runs WHERE started_at < now() - interval '30 days'")
    c.execute("DELETE FROM collect_stats WHERE created_at < now() - interval '60 days'")
    c.execute("DELETE FROM quota WHERE day < current_date - 30")
    # assets：没入选的素材保留 30 天；官方原件永久保留
    keep = {r["id"] for r in c.execute("SELECT id FROM materials WHERE stage='library'").fetchall()}
    removed = 0
    cutoff = time.time() - 30 * 86400
    if config.ASSETS_DIR.exists():
        for d in config.ASSETS_DIR.iterdir():
            if d.is_dir() and d.name.startswith("S") and d.name not in keep and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
    out["assets_removed"] = removed
    return out


# ====================================================================== 初始化与自检

def job_setup(ctx: Ctx) -> dict:
    out: dict[str, Any] = {}
    try:
        out["tdocs"] = ctx.space.setup()
    except Exception as e:  # noqa: BLE001
        out["tdocs_error"] = str(e)[:300]
    try:
        out["livepage"] = ctx.board.setup()
    except Exception as e:  # noqa: BLE001
        out["livepage_error"] = str(e)[:300]
    return out


def job_selftest(ctx: Ctx) -> dict:
    """连通性自检，不调墨问（墨问每个接口每天都有额度）。"""
    out: dict[str, Any] = {"secrets": config.secrets_status()}
    try:
        out["deepseek_model"] = LLM().model()
    except Exception as e:  # noqa: BLE001
        out["deepseek_error"] = str(e)[:200]
    out["mowen_quota_today"] = {r["api"]: r["n"] for r in ctx.c.execute(
        "SELECT api, n FROM quota WHERE day=%s", (ctx.at.date(),)).fetchall()}
    out["tdocs_ready"] = ctx.space.ready
    out["sheets_ok"] = ctx.space.sheets_ok
    out["livepage_ready"] = ctx.board.ready
    return out


def job_test_push(ctx: Ctx) -> dict:
    notify.add(ctx.c, "测试", "测试提醒：内容工厂已接通服务号", ctx.board.url("desk"))
    return {"queued": True}


JOBS: dict[str, Callable[..., dict]] = {
    "collect": job_collect, "process": job_process, "card": job_card, "manual": job_manual,
    "generate": job_generate, "revise": job_revise, "cleanup": job_cleanup, "setup": job_setup,
    "selftest": job_selftest, "test_push": job_test_push,
}
