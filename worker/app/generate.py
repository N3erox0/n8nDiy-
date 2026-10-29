"""WF-05 生文链路：事实底稿 → 初稿 → 文风改写 → 事实核对 → 去 AI 味 → 再核对 → 配图 prompt。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import prompts
from .clients.deepseek import LLM

WORD_LIMITS = {"快讯": (500, 800), "长文": (3000, 5000), "图文": (0, 140)}
SLOT_RE = re.compile(r"【配图\d+｜[^】]*】")


@dataclass
class DraftResult:
    title: str
    body_md: str
    appendix_md: str
    fact_base: str
    check: dict = field(default_factory=dict)
    words: int = 0
    image_slots: int = 0


def count_words(md: str) -> int:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)|【[^】]*】|[#>*`=\[\]()]|https?://\S+", "", md or "")
    return len(re.sub(r"\s+", "", text))


def split_title(md: str, fallback: str) -> tuple[str, str]:
    lines = (md or "").strip().split("\n")
    if lines and lines[0].startswith("# "):
        return lines[0][2:].strip(), "\n".join(lines[1:]).strip()
    return fallback, (md or "").strip()


def materials_text(topic: dict, mats: list[dict], official_md: str | None) -> str:
    parts = [f"选题：{topic['title']}", "理由：" + "；".join(topic.get("reasons") or [])]
    if official_md:
        parts.append("=== 官方原文 ===\n" + official_md[:12000])
    for m in mats[:8]:
        parts.append(
            f"=== 素材 {m['id']}｜{m.get('author') or '—'}｜{m.get('published_at')}｜{m.get('url')} ===\n"
            f"标题：{m.get('title') or ''}\n事实：{m.get('facts') or ''}\n观点：{m.get('quotes') or ''}\n"
            f"正文：{(m.get('content_md') or m.get('summary') or '')[:4000]}")
    return "\n\n".join(parts)


def _system(*names: str, **kw: str) -> str:
    return "\n\n".join(prompts.fill(n, **kw) for n in names)


def run_pipeline(llm: LLM, topic: dict, dtype: str, style: str, mats: list[dict], official_md: str | None,
                 opinion: str = "", quotes: str = "", has_official_image: bool = False) -> DraftResult:
    src = materials_text(topic, mats, official_md)
    # ① 事实底稿
    fact_base = llm.chat(prompts.load("fact_base"), src, temperature=0.1, max_tokens=6000)
    style_name = "style_官方资讯" if dtype == "快讯" else f"style_{style}"
    style_txt = prompts.fill(style_name, opinion=opinion or "（无）", quotes=quotes or "（无）")
    # ① 初稿（含 ② 配图位）
    user = f"事实底稿：\n{fact_base}\n\n选题：{topic['title']}\n理由：{'；'.join(topic.get('reasons') or [])}"
    if dtype == "快讯" and not has_official_image:
        user += "\n\n（本篇没有官方图，不要写【官方图】）"
    draft = llm.chat(_system(f"write_{dtype}", "rules_common") + "\n\n" + style_txt, user, temperature=0.6)
    # ③ 文风改写
    restyle_sys = ("你是责任编辑。按下面的文风和规范改写稿件，事实、数字、引语、配图位一律不动，"
                   "只调整表达和段落组织。只输出改写后的完整 Markdown。\n\n" + style_txt + "\n\n"
                   + prompts.load("rules_common"))
    draft = llm.chat(restyle_sys, draft, temperature=0.5)
    # ④ 事实核对
    chk1 = llm.chat_json(prompts.load("fact_check"), f"事实底稿：\n{fact_base}\n\n稿件：\n{draft}", temperature=0.1)
    draft = chk1.get("text") or draft
    # ⑤ 去 AI 味
    scope = "light" if dtype == "图文" else "structural"
    hw = ""
    if dtype == "长文":
        hw = "\n参考规则二（human-writing，MIT 协议）：\n" + prompts.load("third_party/human-writing")
    de_sys = prompts.fill("de_ai", scope=scope, shuorenhua=prompts.load("third_party/shuorenhua-mini"),
                          human_writing=hw)
    draft = llm.chat(de_sys, draft, temperature=0.5)
    # ④ 再核对一次
    chk2 = llm.chat_json(prompts.load("fact_check"), f"事实底稿：\n{fact_base}\n\n稿件：\n{draft}", temperature=0.1)
    draft = chk2.get("text") or draft
    title, body = split_title(draft, topic["title"])
    # 配图 prompt
    slots = SLOT_RE.findall(body)
    image_part = ""
    if slots:
        style_hint = "科技感" if "科技感" in (topic.get("note") or "") else "极简手绘"
        ip = llm.chat_json(prompts.load("image_prompts"),
                           f"文类：{dtype}\n作者指定风格：{style_hint}\n配图位：\n" + "\n".join(slots)
                           + f"\n\n文章：\n{body[:6000]}", temperature=0.4)
        rows = []
        for s in ip.get("slots") or []:
            rows.append(f"· {s.get('slot')}｜{s.get('style')}｜{s.get('size')}\n  主体：{s.get('subject')}\n"
                        f"  prompt：{s.get('prompt')}\n  negative：{s.get('negative')}")
        image_part = "配图清单与 prompt：\n\n" + "\n\n".join(rows)
    numbers_ok = int(chk2.get("numbers_ok") or 0)
    quotes_ok = int(chk2.get("quotes_ok") or 0)
    unverified = int(chk1.get("unverified") or 0) + int(chk2.get("unverified") or 0)
    check_line = f"数字 {numbers_ok} 处有出处；引语 {quotes_ok} 处与原文一致；未核实 {unverified}"
    links = [f"· {ln.get('author')}｜{ln.get('time')}｜{ln.get('url')}" for ln in topic.get("links") or []]
    appendix = "\n\n".join(filter(None, [
        "素材链接：\n" + "\n".join(links) if links else "",
        "核对结果：" + check_line,
        image_part,
    ]))
    return DraftResult(title=title, body_md=body, appendix_md=appendix, fact_base=fact_base,
                       check={"line": check_line, "issues": (chk1.get("issues") or []) + (chk2.get("issues") or []),
                              "numbers_ok": numbers_ok, "quotes_ok": quotes_ok, "unverified": unverified},
                       words=count_words(body), image_slots=len(slots))


def revise_with_llm(llm: LLM, fact_base: str, numbered_body: str, instructions: list[dict],
                    user_edited: list[int], overall: str) -> dict:
    lines = [f"[{i['idx']}] {i['kind']}：{i['text'] or '（无补充说明）'}" for i in instructions]
    user = (f"事实底稿：\n{fact_base}\n\n稿件（按块编号）：\n{numbered_body}\n\n"
            f"指令清单：\n{chr(10).join(lines) or '（无）'}\n\n"
            f"作者已改（不要动）：{', '.join(map(str, user_edited)) or '（无）'}\n\n"
            f"整体意见：{overall or '（无）'}")
    return llm.chat_json(prompts.load("revise"), user, temperature=0.3)


def check_blocks(llm: LLM, fact_base: str, text: str) -> dict:
    return llm.chat_json(prompts.load("fact_check"), f"事实底稿：\n{fact_base}\n\n稿件：\n{text}", temperature=0.1)
