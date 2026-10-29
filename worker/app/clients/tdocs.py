"""腾讯文档 MCP 客户端（直接发 JSON-RPC，不经过 n8n MCP 节点，避免输出 schema 校验失败）。

工具名和参数 10-29 按服务端 tools/list 实测校正（清单存在 vendor/probe/tdocs_tools.json）。
"""
from __future__ import annotations

import itertools
import json
import re
import time
from typing import Any

import httpx

from .. import config

URL = "https://docs.qq.com/openapi/mcp"
_BAD_TITLE = re.compile(r'[\\/:*?"<>|\r\n\t]')


def safe_title(title: str) -> str:
    """文件名里不能有 / \\ : * ? " < > |（10-29 实测带 / 会报 12977 file Service Error），最长 36 字。"""
    t = _BAD_TITLE.sub("-", title or "未命名").strip() or "未命名"
    return t[:36]


class TDocsError(RuntimeError):
    pass


def _parse_body(r: httpx.Response) -> dict:
    ctype = r.headers.get("content-type", "")
    text = r.text
    if "text/event-stream" in ctype or text.lstrip().startswith(("event:", "data:")):
        last = None
        for line in text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    try:
                        last = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
        if last is None:
            raise TDocsError(f"无法解析事件流：{text[:200]}")
        return last
    try:
        return r.json()
    except json.JSONDecodeError as e:
        raise TDocsError(f"返回不是 JSON（HTTP {r.status_code}）：{text[:200]}") from e


def unwrap(result: dict) -> Any:
    """tools/call 的结果：优先 structuredContent，其次 content[].text（多为 JSON 字符串）。"""
    if result.get("isError"):
        msg = " ".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
        raise TDocsError(msg or "工具返回错误")
    if "structuredContent" in result and result["structuredContent"] is not None:
        return result["structuredContent"]
    texts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict) and c.get("type") == "text"]
    joined = "\n".join(texts).strip()
    if not joined:
        return result
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return {"text": joined}


class TDocs:
    label = "腾讯文档"
    token_name = "TDOCS_MCP_TOKEN"

    def __init__(self, token: str | None = None, url: str = URL, client: httpx.Client | None = None):
        self.token = token if token is not None else config.secret(self.token_name)
        self.url = url
        self.client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))
        self._ids = itertools.count(1)
        self._session: str | None = None
        self._tools: dict[str, dict] | None = None
        self._inited = False

    @property
    def ready(self) -> bool:
        return bool(self.token)

    def _auth(self) -> str:
        return self.token

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        if not self.token:
            raise TDocsError(f"{self.token_name} 未填写")
        if not self._inited and method not in ("initialize", "notifications/initialized"):
            self.initialize()
        headers = {"Authorization": self._auth(), "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}}
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = self.client.post(self.url, json=body, headers=headers)
            except httpx.TransportError as e:
                last = e
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code in (502, 503, 504) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            break
        else:
            raise TDocsError(f"{self.label}连接失败：{last}")
        if sid := r.headers.get("mcp-session-id"):
            self._session = sid
        if r.status_code in (401, 403):
            raise TDocsError(f"{self.label}鉴权失败 HTTP {r.status_code}：{r.text[:200]}")
        data = _parse_body(r)
        if "error" in data and data["error"]:
            err = data["error"]
            raise TDocsError(f"{err.get('code')}: {err.get('message')}")
        return data.get("result", {})

    def initialize(self) -> dict:
        self._inited = True
        try:
            res = self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                           "clientInfo": {"name": "cf-worker", "version": "1.0"}})
        except TDocsError:
            return {}
        try:
            self._rpc("notifications/initialized")
        except TDocsError:
            pass
        return res

    def tools(self, refresh: bool = False) -> dict[str, dict]:
        if self._tools is None or refresh:
            res = self._rpc("tools/list")
            self._tools = {t["name"]: t for t in res.get("tools", [])}
        return self._tools

    def has(self, name: str) -> bool:
        return name in self.tools()

    def call(self, name: str, args: dict | None = None) -> Any:
        return unwrap(self._rpc("tools/call", {"name": name, "arguments": args or {}}))

    # ---------- 空间与文档（工具名和参数 10-29 按 tools/list 实测校正） ----------
    def spaces(self) -> list[dict]:
        out: list[dict] = []
        for page in range(0, 10):
            res = self.call("query_space_list", {"num": page, "query_by": 0})
            out += res.get("spaces") or []
            if not res.get("has_next"):
                break
        return out

    def find_space(self, title_or_id: str) -> str | None:
        """按空间名或空间 ID 找。"""
        key = title_or_id.strip()
        for s in self.spaces():
            if (s.get("title") or "").strip() == key or s.get("space_id") == key:
                return s.get("space_id")
        return None

    def list_children(self, space_id: str, parent_id: str = "") -> list[dict]:
        out: list[dict] = []
        for page in range(0, 50):
            args: dict[str, Any] = {"space_id": space_id, "num": page}
            if parent_id:
                args["parent_id"] = parent_id
            res = self.call("query_space_node", args)
            out += res.get("children") or []
            if not res.get("has_next"):
                break
        return out

    def create_folder(self, space_id: str, title: str, parent_id: str = "") -> dict:
        args: dict[str, Any] = {"space_id": space_id, "title": title, "node_type": "wiki_folder",
                                "wiki_folder_node": {"title": title}}
        if parent_id:
            args["parent_node_id"] = parent_id
        res = self.call("create_space_node", args)
        return res.get("node_info") or res

    def create_file(self, space_id: str, title: str, file_type: str, parent_id: str = "") -> dict:
        args: dict[str, Any] = {"space_id": space_id, "title": safe_title(title), "file_type": file_type}
        if parent_id:
            args["parent_id"] = parent_id
        return self.call("manage.create_file", args)

    def create_doc(self, title: str, markdown: str, space_id: str | None = None, parent_id: str = "") -> dict:
        """建智能文档（Markdown）。外链图片由腾讯文档下载转存（10-29 实测）。
        create_smartcanvas_by_mdx 只能建在个人首页，建好后移进空间。"""
        res = self.call("create_smartcanvas_by_mdx", {"title": safe_title(title), "content_format": "markdown",
                                                      "mdx": markdown or "（空）"})
        fid = res.get("file_id")
        if fid and space_id:
            args: dict[str, Any] = {"file_id": fid, "space_id": space_id}
            if parent_id:
                args["target_parent_id"] = parent_id
            self.call("manage.move_file_to_space", args)
        return res

    def get_content(self, file_id: str) -> Any:
        return self.call("get_content", {"file_id": file_id})

    def delete_node(self, space_id: str, node_id: str) -> Any:
        return self.call("delete_space_node", {"space_id": space_id, "node_id": node_id, "remove_type": "current"})

    # ---------- 智能表格 ----------
    def sheet_tables(self, file_id: str) -> list[dict]:
        return self.call("smartsheet.list_tables", {"file_id": file_id}).get("sheets") or []

    def sheet_add_table(self, file_id: str, title: str) -> str:
        res = self.call("smartsheet.add_table", {"file_id": file_id, "properties": {"title": title}})
        return (res.get("properties") or {}).get("sheet_id") or pick_id(res) or ""

    def sheet_delete_table(self, file_id: str, sheet_id: str) -> Any:
        return self.call("smartsheet.delete_table", {"file_id": file_id, "sheet_id": sheet_id})

    def sheet_fields(self, file_id: str, sheet_id: str) -> list[dict]:
        return self.call("smartsheet.list_fields", {"file_id": file_id, "sheet_id": sheet_id}).get("fields") or []

    def sheet_add_fields(self, file_id: str, sheet_id: str, fields: list[dict]) -> Any:
        return self.call("smartsheet.add_fields", {"file_id": file_id, "sheet_id": sheet_id, "fields": fields})

    def sheet_delete_fields(self, file_id: str, sheet_id: str, field_ids: list[str]) -> Any:
        return self.call("smartsheet.delete_fields", {"file_id": file_id, "sheet_id": sheet_id,
                                                      "field_ids": field_ids})

    def sheet_add_records(self, file_id: str, sheet_id: str, records: list[list[dict]]) -> list[str]:
        res = self.call("smartsheet.add_records", {"file_id": file_id, "sheet_id": sheet_id,
                                                   "records": [{"field_values": fv} for fv in records]})
        return [r.get("record_id") for r in res.get("records") or [] if r.get("record_id")]

    def sheet_update_records(self, file_id: str, sheet_id: str, updates: list[tuple[str, list[dict]]]) -> Any:
        return self.call("smartsheet.update_records", {"file_id": file_id, "sheet_id": sheet_id, "records": [
            {"record_id": rid, "field_values": fv} for rid, fv in updates]})

    def sheet_list_records(self, file_id: str, sheet_id: str, field_titles: list[str] | None = None,
                           max_rows: int = 2000) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while offset < max_rows:
            args: dict[str, Any] = {"file_id": file_id, "sheet_id": sheet_id, "limit": 100, "offset": offset}
            if field_titles:
                args["field_titles"] = field_titles
            res = self.call("smartsheet.list_records", args)
            recs = res.get("records") or []
            out += recs
            if not res.get("has_more") or not recs:
                break
            offset = int(res.get("next") or offset + len(recs))
        return out


def pick_id(obj: Any) -> str | None:
    """从各种返回结构里找出文档/节点 ID。"""
    if isinstance(obj, dict):
        for k in ("file_id", "node_id", "fileId", "nodeId", "id"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                return v
        for v in obj.values():
            if (found := pick_id(v)):
                return found
    elif isinstance(obj, list):
        for v in obj:
            if (found := pick_id(v)):
                return found
    return None


def pick_url(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for k in ("url", "file_url", "link", "doc_url"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for v in obj.values():
            if (found := pick_url(v)):
                return found
    elif isinstance(obj, list):
        for v in obj:
            if (found := pick_url(v)):
                return found
    return None
