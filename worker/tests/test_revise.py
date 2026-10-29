from app import revise
from app.mowen_doc import atom_to_md, md_to_atom, validate

OLD = "# 标题\n\n第一段原文。\n\n第二段原文。\n\n第三段原文。\n"


def test_analyze_instructions_and_edits():
    new = ("# 标题\n\n第一段我自己改过了。\n\n第二段原文。【改：更口语】\n\n第三段原文。\n\n【待修改】\n\n"
           + revise.APPENDIX_H + "\n\n附录内容\n")
    ri = revise.analyze(OLD, new)
    assert ri.status_signal == "待修改"
    assert [i.kind for i in ri.instructions] == ["改"]
    assert ri.instructions[0].idx == 2
    assert ri.user_edited == [1]
    assert all("附录" not in b for b in ri.body_blocks)


def test_apply_plan_respects_allowed_and_locks():
    bs = ["# 标题", "A", '![图](mowen:f1 "center")', "C"]
    plan = {"blocks": {"1": "A2", "2": "改图片", "3": "C2"}, "inserts": [{"after": 1, "text": "新块"}]}
    out, changed = revise.apply_plan(bs, plan, {1})
    assert out == ["# 标题", "A2", "新块", '![图](mowen:f1 "center")', "C"]
    assert changed == [1, 2]


def test_compose_and_split():
    md = revise.compose("# T\n\n正文", "附录", revise.changelog_md("草稿2", [{"what": "改了一句"}]))
    body, appendix = revise.split_sections(md)
    assert body == "# T\n\n正文" and "附录" in appendix


def test_mowen_roundtrip():
    md = "# 标题\n\n第一段，**加粗**和==高亮==，还有[链接](https://example.com)。\n\n> 引用一段\n"
    atom = md_to_atom(md)
    assert validate(atom) == []
    back = atom_to_md(atom)
    assert "**加粗**" in back and "==高亮==" in back and "[链接](https://example.com)" in back
    assert "> 引用一段" in back


def test_mowen_image_locked_roundtrip():
    md = '# 标题\n\n![官方图](mowen:abc123 "center")\n\n正文\n'
    back = atom_to_md(md_to_atom(md))
    assert "mowen:abc123" in back
