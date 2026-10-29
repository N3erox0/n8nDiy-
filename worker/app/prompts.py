"""提示词加载：prompts/ 下的纯文本，改规则只改这里，不用重启。"""
from __future__ import annotations

from . import config


def load(name: str) -> str:
    p = config.PROMPTS_DIR / (name if name.endswith(".md") else f"{name}.md")
    return p.read_text(encoding="utf-8").strip()


def fill(name: str, **kw: str) -> str:
    text = load(name)
    for k, v in kw.items():
        text = text.replace("{" + k + "}", v or "")
    return text
