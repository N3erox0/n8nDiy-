"""墨问 OpenAPI 客户端。

接口地址来自官方文档 mowen.apifox.cn 和官方 mocli v0.5.5：
  公开：note/create、note/edit、note/set、upload/prepare、upload/url
  mocli 用到（文档未列出）：note/info、note/tag、note/list、tag/list、auth/echo

限制：每个接口每秒 1 次；每日配额（默认 / Pro）：
  create 10/100、edit 100/200、set 10/100、upload 20/200
  info 10/?（10-29 实测：note/info 也有每日配额，免费版 10 次；Pro 额度未知，先按 100 计）
  → 不能轮询墨问，只在收到改稿信号后读一次
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .. import config

BASE = "https://open.mowen.cn/api/open/api/v1"
DAILY = {"note/create": (10, 100), "note/edit": (100, 200), "note/set": (10, 100), "note/info": (10, 100),
         "upload/url": (20, 200), "upload/prepare": (20, 200)}
FILE_TYPES = {"image": 1, "audio": 2, "pdf": 3}
UA = "cf-worker/1.0 (content-factory)"


class MowenError(RuntimeError):
    def __init__(self, reason: str, message: str = "", code: int = 0):
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason, self.message, self.code = reason, message, code


QuotaHook = Callable[[str, int], bool]  # (api, limit) -> 允许调用并已计数


class Mowen:
    def __init__(self, api_key: str | None = None, base: str = BASE, client: httpx.Client | None = None,
                 quota: QuotaHook | None = None, min_gap: float = 1.2):
        self.api_key = api_key or config.secret("MOWEN_API_KEY")
        self.base = base.rstrip("/")
        # 墨问网关会拦截 python-httpx 默认 UA（10-29 实测 403），必须换掉
        self.client = client or httpx.Client(timeout=60.0, headers={"User-Agent": UA})
        self.quota = quota
        self.min_gap = min_gap
        self._last: dict[str, float] = {}
        self.pro = config.flag("MOWEN_PRO")

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def limit(self, api: str) -> int | None:
        lim = DAILY.get(api)
        return None if lim is None else lim[1 if self.pro else 0]

    def _post(self, api: str, payload: dict) -> dict:
        if not self.api_key:
            raise MowenError("NO_KEY", "MOWEN_API_KEY 未填写")
        lim = self.limit(api)
        if lim is not None and self.quota and not self.quota(f"mowen:{api}", lim):
            raise MowenError("QUOTA", f"{api} 今日额度 {lim} 次已用完（本地计数）", 403)
        t = self._last.get(api)
        if t is not None and (wait := self.min_gap - (time.monotonic() - t)) > 0:
            time.sleep(wait)
        for attempt in range(3):
            self._last[api] = time.monotonic()
            r = self.client.post(f"{self.base}/{api}", json=payload,
                                 headers={"Authorization": f"Bearer {self.api_key}", "User-Agent": UA})
            if r.status_code == 429 and attempt < 2:
                time.sleep(2.5)
                continue
            break
        try:
            data = r.json()
        except json.JSONDecodeError:
            data = {}
        if r.status_code >= 400:
            raise MowenError(str(data.get("reason") or r.status_code), str(data.get("message") or r.text[:200]),
                             r.status_code)
        return data

    # ---------- 笔记 ----------
    def echo(self) -> dict:
        return self._post("auth/echo", {})

    def create(self, body: dict, tags: list[str] | None = None, publish: bool = False) -> str:
        d = self._post("note/create", {"body": body, "settings": {"autoPublish": publish, "tags": tags or []}})
        nid = d.get("noteId") or d.get("note_id")
        if not nid:
            raise MowenError("BAD_REPLY", f"创建成功但没返回 noteId：{str(d)[:200]}")
        return nid

    def edit(self, note_id: str, body: dict) -> dict:
        return self._post("note/edit", {"noteId": note_id, "body": body})

    def info(self, note_id: str, with_json: bool = True, with_comment: bool = False) -> dict:
        return self._post("note/info", {"noteId": note_id, "options": {
            "withContentJson": with_json, "withComment": with_comment, "withReference": False}})

    def set_privacy(self, note_id: str, ptype: str = "private", no_share: bool = False, expire_at: int = 0) -> dict:
        priv: dict[str, Any] = {"type": ptype}
        if ptype == "rule":
            priv["rule"] = {"noShare": no_share, "expireAt": str(expire_at)}
        return self._post("note/set", {"noteId": note_id, "section": 1, "settings": {"privacy": priv}})

    def tags(self, note_id: str, add: list[str] | None = None, remove: list[str] | None = None) -> dict:
        tags: dict[str, Any] = {}
        if add:
            tags["add"] = add
        if remove:
            tags["del"] = remove
        return self._post("note/tag", {"noteId": note_id, "tags": tags})

    # ---------- 上传 ----------
    def upload_url(self, url: str, kind: str = "image", name: str | None = None) -> str:
        payload: dict[str, Any] = {"fileType": FILE_TYPES[kind], "url": url}
        if name:
            payload["fileName"] = name
        d = self._post("upload/url", payload)
        f = d.get("file") or {}
        fid = f.get("fileId") or f.get("file_id")
        if not fid:
            raise MowenError("BAD_REPLY", f"上传没返回 fileId：{str(d)[:200]}")
        return fid

    def upload_file(self, path: Path, kind: str = "image") -> str:
        d = self._post("upload/prepare", {"fileType": FILE_TYPES[kind], "fileName": path.name})
        form = dict(d.get("form") or {})
        endpoint = form.pop("endpoint", None)
        if not endpoint:
            raise MowenError("BAD_REPLY", f"上传授权没返回 endpoint：{list(form)}")
        with path.open("rb") as fh:
            r = self.client.post(endpoint, data=form, files={"file": (path.name, fh)}, timeout=120)
        if r.status_code >= 400:
            raise MowenError("UPLOAD", r.text[:200], r.status_code)
        try:
            data = r.json()
        except json.JSONDecodeError:
            data = {}
        f = data.get("file") or data
        fid = f.get("fileId") or f.get("file_id") or form.get("x:file_id")
        if not fid:
            raise MowenError("BAD_REPLY", f"投递后没拿到 fileId：{str(data)[:200]}")
        return fid


def note_url(note_id: str) -> str:
    return f"https://note.mowen.cn/detail/{note_id}"


def extract_ast(info_reply: dict) -> dict | None:
    """从 note/info 返回里取正文树；兼容对象或 JSON 字符串两种形式。"""
    info = info_reply.get("info") or info_reply
    content = info.get("content") or {}
    ast = content.get("ast") or content.get("json") or info.get("body")
    if isinstance(ast, str):
        try:
            ast = json.loads(ast)
        except json.JSONDecodeError:
            return None
    return ast if isinstance(ast, dict) else None


def updated_at(info_reply: dict) -> int:
    """笔记最后修改时间（秒）。10-29 实测在 info.base.updatedAt，值是字符串。"""
    info = info_reply.get("info") or info_reply
    for src in (info.get("base") or {}, info):
        for k in ("updatedAt", "updated_at", "editedAt"):
            v = src.get(k)
            if v:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
    return 0


def title_of(info_reply: dict) -> str:
    info = info_reply.get("info") or info_reply
    return ((info.get("base") or {}).get("title") or "").strip()
