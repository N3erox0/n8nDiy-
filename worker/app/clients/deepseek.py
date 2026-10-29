"""DeepSeek（OpenAI 兼容）客户端。"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from .. import config

BASE = "https://api.deepseek.com"
PREFERRED = ("deepseek-chat", "deepseek-v4-flash", "deepseek-v3", "deepseek-flash")


class LLMError(RuntimeError):
    pass


def repair_json(text: str) -> Any:
    """容错解析：去掉代码块围栏，截断时补全括号。"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = min([i for i in (t.find("{"), t.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise LLMError("模型没有返回 JSON")
    t = t[start:]
    stack: list[str] = []
    in_str = esc = False
    for ch in t:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    fixed = t + ('"' if in_str else "") + "".join(reversed(stack))
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON 解析失败：{e}") from e


class LLM:
    def __init__(self, api_key: str | None = None, model: str | None = None, base: str = BASE,
                 client: httpx.Client | None = None):
        self.api_key = api_key or config.secret("DEEPSEEK_API_KEY")
        self._model = model or config.secret("DEEPSEEK_MODEL")
        self.base = base.rstrip("/")
        self.client = client or httpx.Client(timeout=httpx.Timeout(240.0, connect=15.0))

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict:
        if not self.api_key:
            raise LLMError("DEEPSEEK_API_KEY 未填写")
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def model(self) -> str:
        if self._model:
            return self._model
        try:
            r = self.client.get(f"{self.base}/models", headers=self._headers())
            ids = [m["id"] for m in r.json().get("data", [])]
        except Exception:  # noqa: BLE001
            ids = []
        for p in PREFERRED:
            if p in ids:
                self._model = p
                break
        else:
            chats = [i for i in ids if "reason" not in i]
            self._model = chats[0] if chats else "deepseek-chat"
        return self._model

    def chat(self, system: str, user: str, *, json_mode: bool = False, temperature: float = 0.4,
             max_tokens: int = 8000, retries: int = 3) -> str:
        payload: dict[str, Any] = {
            "model": self.model(),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        last: Exception | None = None
        for i in range(retries):
            try:
                r = self.client.post(f"{self.base}/chat/completions", headers=self._headers(), json=payload)
                if r.status_code in (401, 402, 403):
                    raise LLMError(f"DeepSeek 拒绝请求 {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                content = r.json()["choices"][0]["message"].get("content") or ""
                if not content.strip():
                    raise LLMError("模型返回空内容")
                return content
            except LLMError as e:
                if "拒绝" in str(e):
                    raise
                last = e
            except Exception as e:  # noqa: BLE001
                last = e
            time.sleep(8 if i < retries - 1 else 0)
        raise LLMError(f"DeepSeek 调用失败：{last}")

    def chat_json(self, system: str, user: str, **kw) -> Any:
        return repair_json(self.chat(system, user, json_mode=True, **kw))
