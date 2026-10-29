"""活页（LivePage）MCP 客户端：选题卡页面、改稿工作台页面、表单、服务号推送。

工具和参数 10-29 按 tools/list 实测（共 82 个工具，清单存在 vendor/probe/）：
  publish_file     传 Markdown 发布新页面或新版本；有内容审核，审核通过自动上线
  set_form         给页面挂表单（最多 20 个字段），幂等：再次调用整体替换字段，已收的行保留
  list_data_rows   读表单提交，since 传上次的 next_since 只取新行
  push_to_wechat   把页面当前上线版本推到服务号；同一版本每人只推一次
  set_publish_notify 查询/设置自动推送（实测：auto_push=on，你已绑定并关注服务号）
"""
from __future__ import annotations

from typing import Any

import httpx

from .tdocs import TDocs

URL = "https://space.24haowan.com/mcp"


class LivePage(TDocs):
    label = "活页"
    token_name = "LIVEPAGE_TOKEN"

    def __init__(self, token: str | None = None, client: httpx.Client | None = None):
        super().__init__(token=token, url=URL, client=client)

    def _auth(self) -> str:
        return f"Bearer {self.token}"

    # ---------- 文件夹与页面 ----------
    def ensure_folder(self, name: str, intro: str = "") -> str:
        for f in self.call("list_folders", {}).get("folders") or []:
            if f.get("name") == name:
                return f["folder_id"]
        return self.call("create_folder", {"name": name, "intro": intro})["folder_id"]

    def find_page(self, title: str) -> dict | None:
        for p in self.call("list_proposals", {"query": title}).get("proposals") or []:
            if p.get("title") == title:
                return p
        return None

    def publish(self, markdown: str, *, proposal_id: str | None = None, title: str | None = None,
                folder_id: str | None = None, note: str = "", quiet: bool = False) -> dict:
        """发布一个新版本。返回 {proposal_id, version_id, status, url, ...}。
        quiet=False 时，审核通过上线后会按自动推送开关推到服务号。"""
        args: dict[str, Any] = {"filename": "index.md", "content": markdown, "quiet": quiet}
        if proposal_id:
            args["proposal_id"] = proposal_id
            if title:
                args["title"] = title
        else:
            args["title"] = title or "未命名"
            if folder_id:
                args["folder_id"] = folder_id
        if note:
            args["note"] = note[:20]
        return self.call("publish_file", args)

    def set_form(self, proposal_id: str, table: str, title: str, fields: list[dict],
                 members_only: bool = True) -> dict:
        return self.call("set_form", {"proposal_id": proposal_id, "table": table, "title": title[:40],
                                      "fields": fields, "members_only": members_only})

    def rows(self, proposal_id: str, table: str, since: str | None = None, limit: int = 200) -> tuple[list[dict], str | None]:
        """读表单新提交（since 之后，时间正序）。返回（行列表, 下次用的 since）。"""
        args: dict[str, Any] = {"proposal_id": proposal_id, "table": table, "limit": limit}
        if since:
            args["since"] = since
        res = self.call("list_data_rows", args)
        return list(res.get("rows") or []), res.get("next_since") or since

    def push(self, proposal_id: str) -> dict:
        """推当前上线版本给你自己。已推过（含自动推送）的版本不会重复发。"""
        return self.call("push_to_wechat", {"proposal_id": proposal_id, "to": "self"})

    def notify_state(self) -> dict:
        return self.call("set_publish_notify", {})


def row_values(row: dict) -> dict:
    """表单行的字段值。兼容 {values:{..}} / {data:{..}} / 平铺 三种形式（以首次真实提交为准）。"""
    for k in ("values", "data", "row", "fields"):
        v = row.get(k)
        if isinstance(v, dict):
            return v
    return {k: v for k, v in row.items() if not k.startswith("_") and k not in (
        "row_id", "id", "status", "by", "device", "at", "created_at", "table", "version_id",
        "data_revision", "withdrawn_at", "form_rev")}


def row_id(row: dict) -> str:
    return str(row.get("row_id") or row.get("_row_id") or row.get("id") or "")


def row_ok(row: dict) -> bool:
    """只认通过审核、没撤回、由工作区成员（你）提交的行。"""
    if row.get("withdrawn_at"):
        return False
    status = row.get("status") or row.get("_status") or "approved"
    if status not in ("approved", "held"):
        return False
    by = row.get("by") or row.get("_by")
    if isinstance(by, dict) and by.get("kind") == "visitor":
        return False
    return True


def as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).replace("，", "、").replace(",", "、").split("、") if x.strip()]
