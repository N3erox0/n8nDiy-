from datetime import datetime

from app import board, space
from app.clients.livepage import row_ok, row_values
from app.config import TZ


def test_space_encode_decode_roundtrip():
    vals = {"选题ID": "T20261029-01", "确认": True, "生文类型": ["长文", "快讯"], "文风": "观点金句",
            "官方原件": ("原文", "https://openai.com"), "选题分": 0.83, "不存在的列": "x", "备注": None}
    fv = space.encode("选题登记", vals)
    assert {f["field"] for f in fv} == {"选题ID", "确认", "生文类型", "文风", "官方原件", "选题分"}
    # 实测 list_records 返回的就是这几种 *_value 结构
    back = space.decode(fv)
    assert back["选题ID"] == "T20261029-01" and back["确认"] is True
    assert back["生文类型"] == ["长文", "快讯"] and back["文风"] == ["观点金句"]
    assert back["官方原件"] == "https://openai.com" and back["选题分"] == 0.83


def test_field_defs_have_properties():
    for book in space.WORKBOOKS.values():
        for fields in book.values():
            for f in fields:
                d = space.field_def(f)
                assert any(k.startswith("property_") for k in d), f  # 不带 property_* 会报 22019


def test_parse_confirm_and_desk():
    c = board.parse_confirm({"topic": "T20261029-02 某选题", "types": ["图文", "长文"], "style": "观点金句",
                             "opinion": "我觉得…"})
    assert c == {"tid": "T20261029-02", "types": ["长文", "图文"], "style": "观点金句", "opinion": "我觉得…"}
    assert board.parse_confirm({"topic": "（暂无选题）", "types": ["长文"]}) is None
    d = board.parse_desk({"draft": "T20261029-02-长文-2 标题", "action": "确认"})
    assert d == {"draft_id": "T20261029-02-长文-2", "action": "确认", "note": ""}


def test_row_helpers():
    assert row_values({"row_id": "r1", "values": {"topic": "x"}}) == {"topic": "x"}
    assert row_values({"row_id": "r1", "topic": "x", "status": "approved"}) == {"topic": "x"}
    assert row_ok({"status": "approved"}) and not row_ok({"status": "rejected"})
    assert not row_ok({"withdrawn_at": "2026-10-29"})
    assert not row_ok({"by": {"kind": "visitor"}})


def test_desk_markdown():
    md = board.desk_markdown([{"title": "新草稿·长文：x", "url": "https://note.mowen.cn/detail/1",
                               "created_at": datetime(2026, 10, 29, 9, tzinfo=TZ)}],
                             [{"id": "T20261029-01-长文", "title": "x", "version_label": "草稿1", "status": "待确认",
                               "mowen_url": "https://note.mowen.cn/detail/1"}], datetime(2026, 10, 29, 9, tzinfo=TZ))
    assert "| T20261029-01-长文 |" in md and "10-29 09:00" in md
