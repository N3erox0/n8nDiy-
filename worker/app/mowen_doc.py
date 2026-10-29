"""Markdown ↔ 墨问正文树（NoteAtom）。

规则取自墨问官方 mo-note-content-schema：
- 顶层 {"type":"doc","content":[...]}，相邻正文块之间插一个空段落
- heading 只有 "1"/"2"/"3" 级，更深的标题转为加粗段落
- 支持的块：paragraph heading quote note image audio pdf codeblock；marks：highlight link bold code
系统内部用 Markdown；墨问里的图片、音频、PDF、内链笔记用以下标记在 Markdown 中原样保留：
  图片 ![描述](mowen:<fileId> "center")
  其它 [[mowen:<type> {"attr":"..."}]]
"""
from __future__ import annotations

import json
import re
from typing import Any

from markdown_it import MarkdownIt

ALLOWED_TYPES = {"doc", "paragraph", "heading", "text", "quote", "note", "image", "audio", "pdf", "codeblock"}
ALLOWED_MARKS = {"highlight", "link", "bold", "code"}
EMPTY_P = {"type": "paragraph"}

_md = MarkdownIt("commonmark")
TOKEN_RE = re.compile(r"^\[\[mowen:(\w+)\s*(\{.*\})?\]\]$")
IMG_RE = re.compile(r'^!\[([^\]]*)\]\(mowen:([^\s)]+)(?:\s+"(\w+)")?\)$')
HL_RE = re.compile(r"==(.+?)==")


def _text(t: str, marks: list[dict] | None = None) -> dict:
    node: dict[str, Any] = {"type": "text", "text": t}
    if marks:
        node["marks"] = marks
    return node


def _split_highlight(t: str, marks: list[dict]) -> list[dict]:
    out: list[dict] = []
    pos = 0
    for m in HL_RE.finditer(t):
        if m.start() > pos:
            out.append(_text(t[pos:m.start()], [dict(x) for x in marks] or None))
        out.append(_text(m.group(1), [dict(x) for x in marks] + [{"type": "highlight"}]))
        pos = m.end()
    if pos < len(t):
        out.append(_text(t[pos:], [dict(x) for x in marks] or None))
    return out


def _inline(children: list) -> list[dict]:
    nodes: list[dict] = []
    bold = 0
    link: str | None = None
    for tok in children or []:
        t = tok.type
        marks: list[dict] = []
        if bold:
            marks.append({"type": "bold"})
        if link:
            marks.append({"type": "link", "attrs": {"href": link}})
        if t == "text":
            if tok.content:
                nodes.extend(_split_highlight(tok.content, marks))
        elif t == "softbreak":
            nodes.append(_text("\n", marks or None))
        elif t == "hardbreak":
            nodes.append(_text("\n", marks or None))
        elif t == "strong_open":
            bold += 1
        elif t == "strong_close":
            bold = max(0, bold - 1)
        elif t == "link_open":
            link = tok.attrGet("href")
        elif t == "link_close":
            link = None
        elif t == "code_inline":
            nodes.append(_text(tok.content, marks + [{"type": "code"}]))
        elif t == "image":
            alt = "".join(c.content for c in (tok.children or [])) or tok.content
            src = tok.attrGet("src") or ""
            nodes.append(_text(f"[图片：{alt or src}]", marks or None))
        elif t in ("html_inline",):
            nodes.append(_text(tok.content, marks or None))
    merged: list[dict] = []
    for n in nodes:
        if merged and merged[-1].get("marks") == n.get("marks") and merged[-1]["type"] == "text":
            merged[-1]["text"] += n["text"]
        else:
            merged.append(n)
    return [n for n in merged if n.get("text")]


def _special_block(line: str) -> dict | None:
    line = line.strip()
    m = IMG_RE.match(line)
    if m:
        attrs = {"uuid": m.group(2)}
        if m.group(1):
            attrs["alt"] = m.group(1)
        attrs["align"] = m.group(3) or "center"
        return {"type": "image", "attrs": attrs}
    m = TOKEN_RE.match(line)
    if m:
        try:
            attrs = json.loads(m.group(2)) if m.group(2) else {}
        except json.JSONDecodeError:
            attrs = {}
        return {"type": m.group(1), "attrs": {k: str(v) for k, v in attrs.items()}}
    return None


def md_to_atom(md: str) -> dict:
    tokens = _md.parse(md or "")
    blocks: list[dict] = []
    i = 0
    list_stack: list[dict] = []
    while i < len(tokens):
        tok = tokens[i]
        t = tok.type
        if t == "heading_open":
            level = int(tok.tag[1])
            inline = tokens[i + 1]
            content = _inline(inline.children)
            if level <= 3:
                blocks.append({"type": "heading", "attrs": {"level": str(level)}, "content": content})
            else:
                for n in content:
                    marks = n.setdefault("marks", [])
                    if not any(m["type"] == "bold" for m in marks):
                        marks.insert(0, {"type": "bold"})
                blocks.append({"type": "paragraph", "content": content})
            i += 3
            continue
        if t in ("bullet_list_open", "ordered_list_open"):
            list_stack.append({"ordered": t == "ordered_list_open", "n": int(tok.attrGet("start") or 1)})
        elif t in ("bullet_list_close", "ordered_list_close"):
            list_stack.pop()
        elif t == "blockquote_open":
            depth, j, texts = 1, i + 1, []
            while j < len(tokens) and depth:
                if tokens[j].type == "blockquote_open":
                    depth += 1
                elif tokens[j].type == "blockquote_close":
                    depth -= 1
                elif tokens[j].type == "inline":
                    if texts:
                        texts.append(_text("\n"))
                    texts.extend(_inline(tokens[j].children))
                j += 1
            blocks.append({"type": "quote", "content": texts})
            i = j
            continue
        elif t == "paragraph_open":
            inline = tokens[i + 1]
            special = _special_block(inline.content) if not list_stack else None
            if special:
                blocks.append(special)
            else:
                content = _inline(inline.children)
                if list_stack:
                    ls = list_stack[-1]
                    prefix = f"{ls['n']}. " if ls["ordered"] else "· "
                    if ls["ordered"]:
                        ls["n"] += 1
                    content = [_text(prefix)] + content
                if content:
                    blocks.append({"type": "paragraph", "content": content})
            i += 3
            continue
        elif t in ("fence", "code_block"):
            node: dict[str, Any] = {"type": "codeblock", "content": [_text(tok.content.rstrip("\n"))]}
            lang = (tok.info or "").strip().split(" ")[0]
            if lang:
                node["attrs"] = {"language": lang}
            blocks.append(node)
        elif t == "hr":
            blocks.append({"type": "paragraph", "content": [_text("———")]})
        elif t == "html_block":
            txt = re.sub(r"<[^>]+>", "", tok.content).strip()
            if txt:
                blocks.append({"type": "paragraph", "content": [_text(txt)]})
        i += 1
    content: list[dict] = []
    for b in blocks:
        if content:
            content.append(dict(EMPTY_P))
        content.append(b)
    return {"type": "doc", "content": content}


# ---------- 墨问 → Markdown ----------

def _inline_md(nodes: list[dict] | None) -> str:
    out = []
    for n in nodes or []:
        if n.get("type") != "text":
            continue
        s = n.get("text", "")
        marks = {m.get("type"): m for m in n.get("marks", []) or []}
        if "code" in marks:
            s = f"`{s}`"
        if "highlight" in marks and s.strip():
            s = f"=={s}=="
        if "bold" in marks and s.strip():
            s = f"**{s}**"
        if "link" in marks:
            href = (marks["link"].get("attrs") or {}).get("href", "")
            s = f"[{s}]({href})"
        out.append(s)
    return "".join(out)


def atom_to_md(atom: dict | None) -> str:
    if not atom:
        return ""
    parts: list[str] = []
    for b in atom.get("content", []) or []:
        t = b.get("type")
        attrs = b.get("attrs") or {}
        if t == "paragraph":
            s = _inline_md(b.get("content"))
            if s.strip():
                parts.append(s)
        elif t == "heading":
            lvl = int(attrs.get("level", "2") or 2)
            parts.append("#" * max(1, min(lvl, 3)) + " " + _inline_md(b.get("content")))
        elif t == "quote":
            s = _inline_md(b.get("content"))
            parts.append("\n".join("> " + ln for ln in s.split("\n")))
        elif t == "image":
            alt = attrs.get("alt", "")
            align = attrs.get("align", "center")
            parts.append(f'![{alt}](mowen:{attrs.get("uuid", "")} "{align}")')
        elif t == "codeblock":
            lang = attrs.get("language", "")
            code = "".join(n.get("text", "") for n in b.get("content", []) or [])
            parts.append(f"```{lang}\n{code}\n```")
        elif t in ("audio", "pdf", "note"):
            parts.append(f"[[mowen:{t} {json.dumps(attrs, ensure_ascii=False)}]]")
        else:
            parts.append(f"[[mowen:unsupported:{t}]]")
    return "\n\n".join(parts) + ("\n" if parts else "")


def validate(atom: dict) -> list[str]:
    """写回墨问前的检查：返回不支持的节点或样式（为空表示可以写回）。"""
    problems: list[str] = []

    def walk(n: dict) -> None:
        t = n.get("type")
        if t not in ALLOWED_TYPES:
            problems.append(f"不支持的节点：{t}")
        if t == "heading" and str((n.get("attrs") or {}).get("level")) not in {"1", "2", "3"}:
            problems.append("标题级别只能是 1–3")
        for m in n.get("marks", []) or []:
            if m.get("type") not in ALLOWED_MARKS:
                problems.append(f"不支持的样式：{m.get('type')}")
        for c in n.get("content", []) or []:
            walk(c)

    if atom.get("type") != "doc":
        problems.append("顶层必须是 doc")
    walk(atom)
    return sorted(set(problems))


def unsupported_in_md(md: str) -> list[str]:
    return re.findall(r"\[\[mowen:unsupported:(\w+)\]\]", md or "")


def plain_text(atom: dict | None) -> str:
    return re.sub(r"[*=`#>\[\]]", "", atom_to_md(atom))
