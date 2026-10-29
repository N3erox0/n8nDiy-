from datetime import datetime

from app.config import TZ
from app.util import normalize_url, session_of, snowflake_time, window_of


def t(h, m=0, d=29):
    return datetime(2026, 10, d, h, m, tzinfo=TZ)


def test_session_and_window():
    assert session_of(t(7, 30)) == ("早场", "2026-10-29-早场")
    assert session_of(t(17, 30)) == ("晚场", "2026-10-29-晚场")
    a, b = window_of(t(7, 30))
    assert (a, b) == (t(17, 30, 28), t(7, 30))
    a, b = window_of(t(18, 0))
    assert (a, b) == (t(7, 30), t(17, 30))


def test_normalize_url():
    assert normalize_url("https://Example.com/a/?utm_source=x&id=3#top") == "https://example.com/a?id=3"
    wx = "https://mp.weixin.qq.com/s?__biz=MzA&mid=1&idx=1&sn=abc&chksm=zz&scene=21"
    assert normalize_url(wx) == "https://mp.weixin.qq.com/s?__biz=MzA&mid=1&idx=1&sn=abc"
    assert normalize_url("https://t.co/abc") is None


def test_snowflake():
    # 2026 年的帖子 ID 换算出来应落在 2026 年
    assert snowflake_time("1983000000000000000").year >= 2025
