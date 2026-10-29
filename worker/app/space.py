"""腾讯文档空间「自动工作流」：目录、文档、三张智能表格。

10-29 实测结论：
  - 「自动工作流」是知识库空间，query_space_list 能看到（V1）
  - 智能表格可以建表、加字段、增改查记录（V2）；加字段必须带对应的 property_*，否则报 22019
  - 智能文档用 Markdown 建，外链图片由腾讯文档下载转存（V3），不需要对象存储
布局（各节点 ID）存在 kv['tdocs_layout']，第一次 setup 时创建，之后复用。
腾讯文档失败不影响主流程：调用方用 safe() 包一层，只记日志。
"""
from __future__ import annotations

import logging
from typing import Any

from . import config, db
from .clients.tdocs import TDocs, TDocsError, pick_id, pick_url

log = logging.getLogger("tdocs")

FOLDERS = ["素材文件夹", "素材文件夹/订阅", "素材文件夹/手工", "素材文件夹/官方原件", "选题卡", "发稿文件夹", "规则备忘"]

SRC_TYPES = ["官方", "媒体", "公众号", "X", "关键词", "链接入表", "手工转存"]
DTYPES = ["快讯", "长文", "图文"]
STYLES = ["官方资讯", "观点金句"]
DRAFT_STATUS = ["待确认", "待修改", "修改中", "确认", "已定稿", "放弃", "已放弃", "失败"]

# 字段类型：text / number / checkbox / single / multi / url
F = tuple[str, str] | tuple[str, str, list[str]]
WORKBOOKS: dict[str, dict[str, list[F]]] = {
    "素材收集表": {
        "素材收集": [("素材ID", "text"), ("来源类型", "single", SRC_TYPES), ("原文链接", "url"), ("平台", "text"),
                 ("发布人", "text"), ("发布时间", "text"), ("标题", "text"), ("摘要", "text"), ("事实", "text"),
                 ("观点/金句", "text"), ("重点素材", "checkbox"), ("品牌", "text"), ("主题", "text"),
                 ("事实密度分", "number"), ("热度", "text"), ("素材文档", "url"), ("本地备份", "text"),
                 ("状态", "single", ["待补全", "候选", "已用", "失效"]), ("选题ID", "text"), ("备注", "text")],
        "采集统计": [("场次", "text"), ("组", "text"), ("源", "text"), ("方式", "text"),
                 ("状态", "single", ["成功", "失败", "无更新"]), ("条目数", "number"), ("本窗口新条目", "number"),
                 ("入库数", "number"), ("失败原因", "text"), ("耗时(秒)", "number")],
    },
    "选题登记表": {
        "选题登记": [("选题ID", "text"), ("场次", "text"), ("选题来源", "single", ["热点", "指定", "日历"]),
                 ("选题标题", "text"), ("理由", "text"), ("素材链接", "text"), ("选题分", "number"),
                 ("等级", "single", ["A", "B", "C"]), ("是否上卡", "checkbox"), ("确认", "checkbox"),
                 ("生文类型", "multi", DTYPES), ("文风", "single", STYLES), ("我的观点", "text"),
                 ("官方原件", "url"), ("状态", "single", ["推荐", "生成中", "已出稿", "已作废", "失败"]),
                 ("推送时间", "text"), ("备注", "text")],
        "选题日历": [("事件日期", "text"), ("事件", "text"), ("品牌·产品", "text"), ("官方链接", "url"),
                 ("备注", "text")],
    },
    "发文登记表": {
        "发文登记": [("稿ID", "text"), ("选题ID", "text"), ("标题", "text"), ("类型", "single", DTYPES),
                 ("文风", "single", STYLES), ("版本", "single", ["草稿1", "草稿2", "草稿3", "定稿"]),
                 ("墨问链接", "url"), ("墨问笔记ID", "text"), ("字数", "number"), ("配图位数", "number"),
                 ("核对结果", "text"), ("修改意见", "text"), ("状态", "single", DRAFT_STATUS),
                 ("生成时间", "text"), ("过期时间", "text"), ("定稿链接", "url"), ("发布渠道", "text"),
                 ("发布时间", "text"), ("发布链接", "url"), ("24h阅读", "number")],
    },
}
TABLE_BOOK = {t: book for book, tables in WORKBOOKS.items() for t in tables}


def _ftype(f: F) -> str:
    return f[1]


def field_def(f: F) -> dict:
    title, kind = f[0], f[1]
    opts = [{"text": o, "style": (i % 7) + 1} for i, o in enumerate(f[2])] if len(f) > 2 else []  # type: ignore[misc]
    return {
        "text": {"field_title": title, "field_type": "text", "property_text": {}},
        "number": {"field_title": title, "field_type": "number",
                   "property_number": {"decimal_places": 2, "use_separate": False}},
        "checkbox": {"field_title": title, "field_type": "checkbox", "property_checkbox": {"checked": False}},
        "single": {"field_title": title, "field_type": "singleSelect", "property_single_select": {"options": opts}},
        "multi": {"field_title": title, "field_type": "select", "property_select": {"options": opts}},
        "url": {"field_title": title, "field_type": "url", "property_url": {"type": 1}},
    }[kind]


def schema_of(table: str) -> dict[str, str]:
    return {f[0]: _ftype(f) for f in WORKBOOKS[TABLE_BOOK[table]][table]}


def encode(table: str, values: dict[str, Any]) -> list[dict]:
    """{"字段": 值} → field_values。未知字段忽略；None 跳过；空字符串表示清空文本。"""
    sch = schema_of(table)
    out = []
    for k, v in values.items():
        kind = sch.get(k)
        if kind is None or v is None:
            continue
        if kind == "text":
            s = str(v)[:5000]
            out.append({"field": k, "text_value": {"items": [{"text": s, "type": "text"}] if s else []}})
        elif kind == "number":
            try:
                out.append({"field": k, "number_value": float(v)})
            except (TypeError, ValueError):
                continue
        elif kind == "checkbox":
            out.append({"field": k, "bool_value": bool(v)})
        elif kind in ("single", "multi"):
            items = v if isinstance(v, list) else ([v] if v != "" else [])
            out.append({"field": k, "option_value": {"items": [{"text": str(x)} for x in items if x]}})
        elif kind == "url":
            if isinstance(v, (list, tuple)):
                text, link = (v[0], v[1]) if len(v) > 1 else (v[0], v[0])
            else:
                text = link = str(v)
            if link:
                out.append({"field": k, "url_value": {"items": [{"text": str(text)[:60], "type": "url",
                                                                  "link": str(link)}]}})
    return out


def decode(field_values: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for fv in field_values or []:
        k = fv.get("field")
        if not k:
            continue
        if "text_value" in fv:
            out[k] = "".join(i.get("text", "") for i in (fv["text_value"] or {}).get("items") or [])
        elif "option_value" in fv:
            out[k] = [i.get("text", "") for i in (fv["option_value"] or {}).get("items") or [] if i.get("text")]
        elif "url_value" in fv:
            items = (fv["url_value"] or {}).get("items") or []
            out[k] = items[0].get("link") or items[0].get("text") if items else ""
        elif "number_value" in fv:
            out[k] = fv["number_value"]
        elif "bool_value" in fv:
            out[k] = bool(fv["bool_value"])
        elif "string_value" in fv:
            out[k] = fv["string_value"]
    return out


class Space:
    def __init__(self, c, td: TDocs | None = None):
        self.c = c
        self.td = td or TDocs()
        self.layout: dict[str, Any] = db.kv_get(c, "tdocs_layout", {}) or {}

    @property
    def ready(self) -> bool:
        return self.td.ready and bool(self.layout.get("space_id"))

    @property
    def space_id(self) -> str:
        return self.layout.get("space_id") or ""

    def save(self) -> None:
        db.kv_set(self.c, "tdocs_layout", self.layout)

    # ---------- 初始化 ----------
    def setup(self) -> dict:
        name = (config.secret("TDOCS_SPACE_NAME", "自动工作流") or "自动工作流").strip()
        sid = self.td.find_space(name)
        if not sid and name != "自动工作流":
            log.warning("TDOCS_SPACE_NAME=%s 既不是空间名也不是空间 ID，改用默认空间「自动工作流」", name)
            name = "自动工作流"
            sid = self.td.find_space(name)
        if not sid:
            raise TDocsError(f"没找到知识库空间「{name}」，请在腾讯文档里建一个同名空间")
        if self.layout.get("space_id") != sid:
            self.layout = {"space_id": sid}
        for path in FOLDERS:
            self.ensure_folder(path)
        for book in WORKBOOKS:
            self._ensure_workbook(book)
        self.layout["sheets_ok"] = True
        self.save()
        return {"space_id": sid, "folders": len(self.layout.get("folders", {})),
                "sheets": {t: v.get("url") for t, v in (self.layout.get("sheets") or {}).items()}}

    def _child(self, parent: str, title: str) -> dict | None:
        for n in self.td.list_children(self.space_id, parent):
            if n.get("title") == title:
                return n
        return None

    def ensure_folder(self, path: str) -> str:
        folders = self.layout.setdefault("folders", {})
        if path in folders:
            return folders[path]
        parent_path, _, name = path.rpartition("/")
        parent = self.ensure_folder(parent_path) if parent_path else ""
        node = self._child(parent, name) or self.td.create_folder(self.space_id, name, parent)
        nid = node.get("node_id") or pick_id(node)
        if not nid:
            raise TDocsError(f"建文件夹「{path}」没返回 ID")
        folders[path] = nid
        self.save()
        return nid

    def _ensure_workbook(self, book: str) -> None:
        sheets = self.layout.setdefault("sheets", {})
        tables = WORKBOOKS[book]
        if all(t in sheets for t in tables):
            return
        node = self._child("", book)
        if node:
            fid, url = node.get("node_id"), node.get("url")
        else:
            res = self.td.create_file(self.space_id, book, "smartsheet")
            fid, url = res.get("file_id"), res.get("url")
        existing = {t["title"]: t["sheet_id"] for t in self.td.sheet_tables(fid)}
        for t, fields in tables.items():
            sheet_id = existing.get(t) or self.td.sheet_add_table(fid, t)
            have = {f["field_title"]: f for f in self.td.sheet_fields(fid, sheet_id)}
            # 新表自带一个「智能表列」，改名成第一列
            first = fields[0]
            if first[0] not in have and "智能表列" in have and _ftype(first) == "text":
                f0 = have.pop("智能表列")
                self.td.call("smartsheet.update_fields", {"file_id": fid, "sheet_id": sheet_id, "fields": [
                    {"field_id": f0["field_id"], "field_title": first[0], "field_type": "text", "property_text": {}}]})
                have[first[0]] = f0
            for f in fields:
                if f[0] not in have:
                    self.td.sheet_add_fields(fid, sheet_id, [field_def(f)])
            sheets[t] = {"file_id": fid, "sheet_id": sheet_id, "url": url}
            self.save()
        # 删掉新建表格自带的默认子表（智能表1）
        for title, sid in existing.items():
            if title not in tables and title.startswith("智能表"):
                try:
                    self.td.sheet_delete_table(fid, sid)
                except TDocsError as e:
                    log.info("默认子表删不掉：%s", e)

    # ---------- 文档 ----------
    def create_doc(self, folder_path: str, title: str, markdown: str) -> tuple[str | None, str | None]:
        parent = self.ensure_folder(folder_path) if folder_path else ""
        res = self.td.create_doc(title[:36], markdown, self.space_id, parent)
        return res.get("file_id") or pick_id(res), res.get("url") or pick_url(res)

    def sheet_url(self, table: str) -> str | None:
        return ((self.layout.get("sheets") or {}).get(table) or {}).get("url")

    # ---------- 记录 ----------
    @property
    def sheets_ok(self) -> bool:
        return self.ready and bool(self.layout.get("sheets_ok"))

    def _sh(self, table: str) -> dict | None:
        return (self.layout.get("sheets") or {}).get(table)

    def add_records(self, table: str, rows: list[dict]) -> list[str]:
        sh = self._sh(table)
        if not (self.sheets_ok and sh and rows):
            return []
        ids: list[str] = []
        for i in range(0, len(rows), 50):
            ids += self.td.sheet_add_records(sh["file_id"], sh["sheet_id"], [encode(table, r) for r in rows[i:i + 50]])
        return ids

    def add_record(self, table: str, values: dict) -> str | None:
        ids = self.add_records(table, [values])
        return ids[0] if ids else None

    def update_record(self, table: str, record_id: str | None, values: dict) -> None:
        sh = self._sh(table)
        if not (self.sheets_ok and sh and record_id):
            return
        self.td.sheet_update_records(sh["file_id"], sh["sheet_id"], [(record_id, encode(table, values))])

    def list_records(self, table: str, fields: list[str] | None = None) -> list[dict]:
        sh = self._sh(table)
        if not (self.sheets_ok and sh):
            return []
        recs = self.td.sheet_list_records(sh["file_id"], sh["sheet_id"], fields)
        return [{"record_id": r.get("record_id"), "values": decode(r.get("field_values") or [])} for r in recs]


def as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v or "").strip().lower() in {"true", "1", "是", "yes", "✓", "√", "checked"}


def as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return [x.strip() for x in str(v).replace("，", ",").replace("、", ",").split(",") if x.strip()]


def first(v: Any) -> str:
    if isinstance(v, list):
        return str(v[0]) if v else ""
    return str(v or "")


def safe(fn, *a, **kw):
    """腾讯文档 / 活页同步失败不影响主流程，只记日志。"""
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        log.warning("外部文档操作失败：%s", e)
        return None
