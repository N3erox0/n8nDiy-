from datetime import datetime, timedelta

from app import topics
from app.config import TZ


def test_density_and_grade():
    assert topics.density_score({"subject": 2.5, "numbers": 2, "conditions": 1, "traceable": 9}) == 8.0
    assert topics.grade(0.85) == "A" and topics.grade(0.65) == "B" and topics.grade(0.3) == "C"


def test_score_modes():
    at = datetime(2026, 10, 29, 7, 30, tzinfo=TZ)
    fresh = topics.timeliness(at - timedelta(hours=1), at)
    with_heat = topics.topic_score(9, 80, fresh, "normal")
    no_heat = topics.topic_score(9, None, fresh, "none")
    assert 0 < with_heat <= 1 and 0 < no_heat <= 1
    heats, mode = topics.fill_heat([None, None])
    assert mode == "none"
    heats, mode = topics.fill_heat([50, None, 70])
    assert heats[1] == 60 and mode == "filled"


def test_cluster_and_dup():
    items = [{"id": "a", "title": "OpenAI 发布 GPT-6", "event_key": "OpenAI|发布 GPT-6"},
             {"id": "b", "title": "GPT-6 正式发布，OpenAI 公布细节", "event_key": "OpenAI|发布 GPT-6"},
             {"id": "c", "title": "DeepSeek 开源新模型", "event_key": "DeepSeek|开源新模型"}]
    groups = topics.cluster(items)
    assert sorted(len(g) for g in groups) == [1, 2]
    assert topics.is_duplicate_topic("OpenAI 发布 GPT-6", "OpenAI|发布 GPT-6",
                                     [{"title": "x", "cluster_key": "OpenAI|发布 GPT-6"}])


def test_card_markdown_mentions_form():
    md = topics.card_markdown(datetime(2026, 10, 29, 8, tzinfo=TZ), [
        {"id": "T20261029-01", "title": "测试选题", "grade": "A", "origin": "热点", "reasons": ["新", "大"],
         "links": [{"author": "OpenAI", "time": "2026-10-29 06:00", "url": "https://openai.com/x"}],
         "official": {"doc_url": "https://docs.qq.com/aio/x", "doc_title": "原文"}}],
        2, "采集：…", "https://docs.qq.com/smartsheet/x", ["连续 7 天失败的源：A"])
    assert "T20261029-01" in md and "确认选题" in md and "others：本场另有 2 个候选" in md
    assert "连续 7 天失败的源" in md and "[原文](https://docs.qq.com/aio/x)" in md
