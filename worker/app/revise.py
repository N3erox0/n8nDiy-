"""改稿（WF-06）：从墨问读回的正文里找出你的修改。

你在墨问里可以：
  - 直接改字：系统保留你的改动
  - 写指令：在句子后面写【改：…】【删】【补：…】（全角括号）
  - 在正文任意位置单独一行写【待修改】/【确认】/【放弃】作为状态信号（不填登记表时用）
系统生成的正文末尾有「附录（系统生成）」和「修改记录（系统生成）」两段，比对时剔除。
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

APPENDIX_H = "## 附录（系统生成）"
CHANGELOG_H = "## 修改记录（系统生成）"
SENTINELS = (APPENDIX_H, CHANGELOG_H)

INSTR_RE = re.compile(r"【\s*(改|删|补)\s*[:：]?\s*([^】]*)】")
STATUS_RE = re.compile(r"^\s*【\s*(待修改|确认|放弃)\s*】\s*$")
LOCKED_RE = re.compile(r'^(!\[[^\]]*\]\(mowen:|\[\[mowen:)')


def split_sections(md: str) -> tuple[str, str]:
    """拆成（正文, 附录）。修改记录整段丢弃，附录保留原样。"""
    md = md or ""
    lines = md.split("\n")
    body: list[str] = []
    appendix: list[str] = []
    cur = body
    for ln in lines:
        s = ln.strip()
        heading = s.replace("**", "")
        if heading.startswith(APPENDIX_H.replace("## ", "")) or s == APPENDIX_H:
            cur = appendix
            continue
        if heading.startswith(CHANGELOG_H.replace("## ", "")) or s == CHANGELOG_H:
            cur = []  # 丢弃旧的修改记录
            continue
        cur.append(ln)
    appendix = [ln for ln in appendix if not STATUS_RE.match(ln)]
    return "\n".join(body).strip(), "\n".join(appendix).strip()


def blocks(md: str) -> list[str]:
    return [b.strip() for b in re.split(r"\n\s*\n", md or "") if b.strip()]


def join_blocks(bs: list[str]) -> str:
    return "\n\n".join(b for b in bs if b.strip()) + "\n"


@dataclass
class Instruction:
    idx: int          # 在新正文块列表里的位置
    kind: str         # 改/删/补
    text: str         # 指令内容
    target: str       # 去掉指令后的原句块


@dataclass
class ReviseInput:
    body_blocks: list[str]
    instructions: list[Instruction] = field(default_factory=list)
    user_edited: list[int] = field(default_factory=list)   # 你直接改过的块
    status_signal: str | None = None
    images_added: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.instructions or self.user_edited)


def analyze(old_body: str, new_md: str) -> ReviseInput:
    """old_body：系统上一版正文；new_md：从墨问读回的全文（可能含附录、修改记录、状态信号）。"""
    body, _ = split_sections(new_md)
    raw = blocks(body)
    status = None
    # 状态行写在哪都认（包括写在附录、修改记录后面）；以最后一处为准
    for ln in (new_md or "").split("\n"):
        if (mm := STATUS_RE.match(ln)):
            status = mm.group(1)
    kept: list[str] = []
    for b in raw:
        if STATUS_RE.match(b):
            continue
        lines = [ln for ln in b.split("\n") if not STATUS_RE.match(ln)]
        if lines:
            kept.append("\n".join(lines))
    instructions: list[Instruction] = []
    for i, b in enumerate(kept):
        found = list(INSTR_RE.finditer(b))
        if not found:
            continue
        target = INSTR_RE.sub("", b).strip()
        for m in found:
            instructions.append(Instruction(i, m.group(1), m.group(2).strip(), target))
    old = blocks(old_body)
    clean_new = [INSTR_RE.sub("", b).strip() for b in kept]
    sm = difflib.SequenceMatcher(a=old, b=clean_new, autojunk=False)
    edited: list[int] = []
    images_added = 0
    for op, _a0, _a1, b0, b1 in sm.get_opcodes():
        if op in ("replace", "insert"):
            for j in range(b0, b1):
                if LOCKED_RE.match(clean_new[j]):
                    if op == "insert":
                        images_added += 1
                    continue
                if clean_new[j]:
                    edited.append(j)
    instr_idx = {ins.idx for ins in instructions}
    edited = [j for j in edited if j not in instr_idx]
    return ReviseInput(kept, instructions, edited, status, images_added)


def numbered(blocks_: list[str]) -> str:
    out = []
    for i, b in enumerate(blocks_):
        tag = "（图片/资源，锁定）" if LOCKED_RE.match(b) else ""
        out.append(f"[{i}]{tag}\n{b}")
    return "\n\n".join(out)


def apply_plan(blocks_: list[str], plan: dict, allowed: set[int] | None) -> tuple[list[str], list[int]]:
    """按模型返回的修改计划落地。只允许改 allowed 里的块（None = 全部可改，用于整体意见）；
    图片等锁定块一律不动。返回（新块列表, 实际改动过的块序号）。"""
    out = list(blocks_)
    changed: list[int] = []
    for k, v in (plan.get("blocks") or {}).items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            continue
        if not (0 <= i < len(out)) or LOCKED_RE.match(out[i]):
            continue
        if allowed is not None and i not in allowed:
            continue
        new = (v or "").strip()
        new = INSTR_RE.sub("", new).strip()
        if new != out[i]:
            out[i] = new
            changed.append(i)
    inserts = sorted(plan.get("inserts") or [], key=lambda x: -int(x.get("after", -1)))
    for ins in inserts:
        try:
            after = int(ins.get("after", -1))
        except (TypeError, ValueError):
            continue
        if allowed is not None and after not in allowed:
            continue
        text = INSTR_RE.sub("", (ins.get("text") or "")).strip()
        if text and -1 <= after < len(out):
            out.insert(after + 1, text)
            changed = [c + 1 if c > after else c for c in changed] + [after + 1]
    final = [INSTR_RE.sub("", b).strip() for b in out]
    return [b for b in final if b], sorted(set(changed))


def changelog_md(version_label: str, items: list[dict]) -> str:
    lines = [CHANGELOG_H, "", f"{version_label}："]
    if not items:
        lines.append("· 无")
    for it in items:
        st = it.get("status") or "已改"
        note = it.get("note") or ""
        what = it.get("what") or ""
        lines.append(f"· {st}：{what}{('（' + note + '）') if note else ''}")
    return "\n".join(lines)


def compose(body: str, appendix: str, changelog: str | None = None) -> str:
    parts = [body.strip()]
    if appendix.strip():
        parts.append(APPENDIX_H + "\n\n" + appendix.strip())
    if changelog:
        parts.append(changelog.strip())
    return "\n\n".join(parts) + "\n"
