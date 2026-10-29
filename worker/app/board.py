"""活页上的三个固定页面（都在活页文件夹「内容工厂」里，私有，只有你能打开）：

  「内容工厂 · 选题卡」 每天 08:00 / 18:00 发一个新版本 → 自动推到服务号
                        页面上的「确认选题」表单：选题 + 生文类型 + 文风 + 我的观点
  「内容工厂 · 改稿台」 新草稿、改稿完成、定稿、出错时发一个新版本 → 自动推到服务号
                        页面上的「改稿」表单：稿件 + 操作（待修改 / 确认 / 放弃）+ 整体意见
  「内容工厂 · 收素材」 固定页面，不推送。在微信里看到好文章，粘贴链接提交 → 10 分钟内入素材库

同一版本每人只推一次（活页自动去重）。布局和读取游标存在 kv['lp_layout']。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from . import db
from .clients.livepage import LivePage, as_list, row_id, row_ok, row_values
from .util import fmt_time, now, short

log = logging.getLogger("board")

FOLDER = "内容工厂"
CARD_TITLE = "内容工厂 · 选题卡"
DESK_TITLE = "内容工厂 · 改稿台"
INBOX_TITLE = "内容工厂 · 收素材"
TID_RE = re.compile(r"T\d{8}-\d{2,3}")
DID_RE = re.compile(r"T\d{8}-\d{2,3}-(?:快讯|长文|图文)(?:-\d+)?")
ACTIONS = ["待修改", "确认", "放弃"]
URL_RE = re.compile(r"https?://[^\s<>\"'，。、）)】]+")

INBOX_MD = """# 收素材

在微信里看到顺眼的文章：右上角「…」→「复制链接」，回到这里粘贴提交。

- 一次一条；链接前后带着标题、评论文字也没关系，系统只取链接。
- 公众号、网页、X 链接都行。短链（t.cn、b23.tv 这类）请先点开，复制打开后的地址。
- 没有链接的内容（截图里的文字、群聊里的一段话），直接贴进「内容」，会按「手工转存」入库。
- 勾「重点素材」的，写观点金句稿时会优先引用。

提交后 10 分钟内入库：原文和图片备份到服务器，腾讯文档「素材文件夹/手工」里建素材文档，并登记到素材收集表。入库结果会推到「改稿台」。
"""


def inbox_form() -> list[dict]:
    return [
        {"key": "link", "label": "链接", "type": "textarea", "max": 500},
        {"key": "text", "label": "内容（没有链接时贴这里）", "type": "textarea", "max": 500},
        {"key": "author", "label": "发布人（可不填，抓不到时用）", "type": "text", "max": 40},
        {"key": "key", "label": "重点素材", "type": "select", "options": ["否", "是"]},
        {"key": "note", "label": "备注（可不填）", "type": "text", "max": 200},
    ]


def parse_inbox(values: dict) -> dict | None:
    raw = str(values.get("link") or "")
    m = URL_RE.search(raw)
    text = str(values.get("text") or "").strip()
    if not m and not text:
        return None
    return {"url": m.group(0) if m else "", "text": text, "author": str(values.get("author") or "").strip(),
            "is_key": str(values.get("key") or "") == "是", "note": str(values.get("note") or "").strip()}


def clean_options(options: list[str]) -> list[str]:
    """活页选项：不能含「、」（多选分隔符，10-29 实测报 field_option_invalid），≤40 字，去重，≤50 个。"""
    out: list[str] = []
    for o in options:
        o = re.sub(r"\s+", " ", str(o).replace("、", "，")).strip()[:40]
        if o and o not in out:
            out.append(o)
    return out[:50]


def card_form(options: list[str]) -> list[dict]:
    return [
        {"key": "topic", "label": "选题", "type": "select", "required": True,
         "options": clean_options(options) or ["（暂无选题）"]},
        {"key": "types", "label": "生文类型", "type": "multiselect", "required": True, "options": ["快讯", "长文", "图文"]},
        {"key": "style", "label": "文风", "type": "select", "required": True, "options": ["官方资讯", "观点金句"]},
        {"key": "opinion", "label": "我的观点（选观点金句必填）", "type": "textarea", "max": 500},
    ]


def desk_form(options: list[str]) -> list[dict]:
    return [
        {"key": "draft", "label": "稿件", "type": "select", "required": True,
         "options": clean_options(options) or ["（暂无稿件）"]},
        {"key": "action", "label": "操作", "type": "select", "required": True, "options": ACTIONS},
        {"key": "note", "label": "整体意见（可不填）", "type": "textarea", "max": 500},
    ]


def topic_option(t: dict) -> str:
    return f"{t['id']} {short(t.get('title') or '', 26)}"[:40]


def draft_option(d: dict) -> str:
    return f"{d['id']} {short(d.get('title') or '', 18)}"[:40]


def parse_confirm(values: dict) -> dict | None:
    m = TID_RE.search(str(values.get("topic") or ""))
    if not m:
        return None
    types = [t for t in ("快讯", "长文", "图文") if t in as_list(values.get("types"))]
    if not types:
        return None
    style = str(values.get("style") or "官方资讯").strip()
    style = style if style in ("官方资讯", "观点金句") else "官方资讯"
    return {"tid": m.group(0), "types": types, "style": style, "opinion": str(values.get("opinion") or "").strip()}


def parse_desk(values: dict) -> dict | None:
    m = DID_RE.search(str(values.get("draft") or ""))
    action = str(values.get("action") or "").strip()
    if not m or action not in ACTIONS:
        return None
    return {"draft_id": m.group(0), "action": action, "note": str(values.get("note") or "").strip()}


def desk_markdown(notices: list[dict], drafts: list[dict], at: datetime) -> str:
    lines = [f"# 改稿台", "", f"更新于 {at.strftime('%Y/%m/%d %H:%M')}", "", "## 最新提醒", ""]
    if not notices:
        lines.append("暂无。")
    for n in notices:
        t = fmt_time(n.get("created_at"))
        link = f"　[打开]({n['url']})" if n.get("url") else ""
        lines.append(f"- {t[5:]}｜{n.get('title')}{link}")
    lines += ["", "## 进行中的稿件", ""]
    if not drafts:
        lines.append("暂无。")
    else:
        lines += ["| 稿ID | 标题 | 版本 | 状态 | 墨问 |", "|---|---|---|---|---|"]
        for d in drafts:
            mw = f"[打开]({d['mowen_url']})" if d.get("mowen_url") else "—"
            lines.append(f"| {d['id']} | {short(d.get('title') or '', 20)} | {d.get('version_label')} | "
                         f"{d.get('status')} | {mw} |")
    lines += ["", "## 怎么改稿", "",
              "1. 打开墨问链接，直接改字，或在句子后面写【改：…】【删】【补：…】；也可以插单张图片。",
              "2. 改完回到这里，点下面的「改稿」，选稿件和「待修改」，有整体意见就写上。",
              "3. 满意了选「确认」，系统把定稿存进腾讯文档发稿文件夹；不要了选「放弃」。",
              "", "也可以在腾讯文档「发文登记表」里把「状态」改成 待修改 / 确认 / 放弃。系统 5 分钟内处理。",
              "（墨问读笔记每天有额度，系统不会自己去墨问里看，一定要在这里或表格里点一下。）"]
    return "\n".join(lines) + "\n"


class Board:
    def __init__(self, c, lp: LivePage | None = None):
        self.c = c
        self.lp = lp or LivePage()
        self.layout: dict[str, Any] = db.kv_get(c, "lp_layout", {}) or {}

    @property
    def ready(self) -> bool:
        return self.lp.ready and bool(self.layout.get("card") and self.layout.get("desk"))

    def save(self) -> None:
        db.kv_set(self.c, "lp_layout", self.layout)

    def setup(self) -> dict:
        fid = self.layout.get("folder_id") or self.lp.ensure_folder(FOLDER, "选题卡与改稿台（系统自动维护）")
        self.layout["folder_id"] = fid
        pages = (("card", CARD_TITLE, "# 选题卡\n\n每天 08:00、18:00 自动更新。\n"),
                 ("desk", DESK_TITLE, desk_markdown([], [], now())),
                 ("inbox", INBOX_TITLE, INBOX_MD))
        for key, title, body in pages:
            if self.layout.get(key):
                continue
            page = self.lp.find_page(title)
            if not page:
                self.lp.publish(body, title=title, folder_id=fid, note="初始化", quiet=True)
                page = self.lp.find_page(title) or {}
            self.layout[key] = {"proposal_id": page["proposal_id"], "url": page.get("default_url")}
            self.save()
        for key, title, _ in pages:
            if not self.layout[key].get("url"):
                self.layout[key]["url"] = (self.lp.find_page(title) or {}).get("default_url")
        self.lp.set_form(self.layout["card"]["proposal_id"], "confirm", "确认选题", card_form([]))
        self.lp.set_form(self.layout["desk"]["proposal_id"], "revise", "改稿", desk_form([]))
        self.lp.set_form(self.layout["inbox"]["proposal_id"], "inbox", "存进素材库", inbox_form())
        self.save()
        state = {}
        try:
            state = self.lp.notify_state()
        except Exception as e:  # noqa: BLE001
            log.warning("读推送开关失败：%s", e)
        return {k: self.layout[k].get("url") for k, _, _ in pages} | {"notify": state}

    def url(self, key: str) -> str | None:
        return (self.layout.get(key) or {}).get("url")

    def _pid(self, key: str) -> str:
        return self.layout[key]["proposal_id"]

    # ---------- 选题卡 ----------
    def publish_card(self, md: str, options: list[str], note: str) -> dict:
        pid = self._pid("card")
        self.lp.set_form(pid, "confirm", "确认选题", card_form(options))
        r = self.lp.publish(md, proposal_id=pid, note=note, quiet=False)
        if r.get("url"):
            self.layout["card"]["url"] = r["url"]
            self.save()
        self._push(pid, r)
        return r

    def read_confirms(self) -> list[dict]:
        return [x for x in (parse_confirm(v) for v in self._new_rows("card", "confirm")) if x]

    # ---------- 改稿台 ----------
    def publish_desk(self, notices: list[dict], drafts: list[dict], note: str, quiet: bool = False) -> dict:
        pid = self._pid("desk")
        self.lp.set_form(pid, "revise", "改稿", desk_form([draft_option(d) for d in drafts]))
        r = self.lp.publish(desk_markdown(notices, drafts, now()), proposal_id=pid, note=note, quiet=quiet)
        if not quiet:
            self._push(pid, r)
        return r

    def update_desk_form(self, drafts: list[dict]) -> None:
        self.lp.set_form(self._pid("desk"), "revise", "改稿", desk_form([draft_option(d) for d in drafts]))

    def read_desk(self) -> list[dict]:
        return [x for x in (parse_desk(v) for v in self._new_rows("desk", "revise")) if x]

    # ---------- 收素材 ----------
    def read_inbox(self) -> list[dict]:
        if not self.layout.get("inbox"):
            return []
        return [x for x in (parse_inbox(v) for v in self._new_rows("inbox", "inbox")) if x]

    # ---------- 内部 ----------
    def _push(self, pid: str, publish_reply: dict) -> None:
        if publish_reply.get("status") not in ("published", "live", None):
            return  # 还在审核，审核通过后按自动推送开关推送
        try:
            self.lp.push(pid)
        except Exception as e:  # noqa: BLE001
            log.info("push_to_wechat：%s", e)

    def _new_rows(self, key: str, table: str) -> list[dict]:
        cursors = self.layout.setdefault("cursors", {})
        ck = f"{key}:{table}"
        since = cursors.get(ck)
        rows, nxt = self.lp.rows(self._pid(key), table, since=since)
        seen = set(self.layout.setdefault("seen", {}).get(ck, []))
        out = []
        for r in rows:
            rid = row_id(r)
            if rid and rid in seen:
                continue
            if row_ok(r):
                out.append(row_values(r))
            if rid:
                seen.add(rid)
        if since is None and not rows:
            nxt = now().isoformat()
        cursors[ck] = nxt
        self.layout["seen"][ck] = list(seen)[-200:]
        self.save()
        return out
