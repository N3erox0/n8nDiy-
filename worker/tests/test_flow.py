"""端到端流程测试：打分入库 → 聚类出题 → 选题卡 → 确认 → 生文 → 改稿 → 定稿。

LLM、墨问、活页用假的；数据库用真的（cf_test 库）。没有 TEST_DATABASE_URL 时跳过。
在容器里跑：docker compose exec -e TEST_DATABASE_URL=postgresql://…/cf_test worker pytest -q
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta

import pytest

from app import config, db, jobs, notify, prompts
from app.config import TZ
from app.extract import Article
from app.mowen_doc import atom_to_md, md_to_atom

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="需要 TEST_DATABASE_URL")
AT = datetime(2026, 10, 29, 7, 30, tzinfo=TZ)


class FakeLLM:
    ready = True

    def __init__(self):
        self.calls: list[str] = []
        self.p = {n: prompts.load(n) for n in ("score", "topic", "fact_check", "revise", "image_prompts", "fact_base")}

    def model(self):
        return "fake"

    def _kind(self, system: str) -> str:
        for k, v in self.p.items():
            if system.startswith(v[:60]):
                return k
        return "write"

    def chat(self, system, user, **kw):
        k = self._kind(system)
        self.calls.append(k)
        if k == "fact_base":
            return "1. OpenAI 于 2026-10-29 发布 GPT-6。\n2. 上下文 200 万 token。"
        return "# GPT-6 来了\n\n第一段：OpenAI 发布 GPT-6。\n\n第二段：上下文 200 万 token。\n\n第三段：结尾。"

    def chat_json(self, system, user, **kw):
        k = self._kind(system)
        self.calls.append(k)
        if k == "score":
            items = json.loads(user)
            out = []
            for it in items:
                off = "openai.com" in it.get("title", "") or it["author"] == "OpenAI"
                scope = "漏洞" not in it["title"]
                out.append({"id": it["id"], "in_scope": scope,
                            "density": {"subject": 2.5, "numbers": 2.5, "conditions": 1.5 if off else 1, "traceable": 2.5},
                            "brand": "OpenAI", "theme": "大模型", "event_key": "OpenAI|发布 GPT-6",
                            "facts": "OpenAI 发布 GPT-6；上下文 200 万 token", "quotes": "",
                            "title_zh": "OpenAI 发布 GPT-6", "summary": "摘要"})
            return {"items": out}
        if k == "topic":
            return {"title": "OpenAI 发布 GPT-6", "reasons": ["新一代旗舰模型", "上下文大幅提升"]}
        if k == "fact_check":
            text = user.split("稿件：\n", 1)[1]
            return {"text": text, "numbers_ok": 2, "quotes_ok": 0, "unverified": 0, "issues": []}
        if k == "image_prompts":
            return {"slots": []}
        if k == "revise":
            nums = re.findall(r"^\[(\d+)\] 改", user, re.M)
            return {"blocks": {n: "第二段：改得更口语了，上下文有 200 万 token。" for n in nums},
                    "log": [{"what": "第二段改口语", "status": "已改"}]}
        return {}


class FakeMowen:
    def __init__(self):
        self.notes: dict[str, dict] = {}
        self.t = 1000
        self.tagged: list = []

    def _bump(self):
        self.t += 10
        return self.t

    def create(self, atom, tags=None, publish=False):
        nid = f"n{len(self.notes) + 1}"
        self.notes[nid] = {"atom": atom, "updated": self._bump()}
        return nid

    def edit(self, nid, atom):
        self.notes[nid] = {"atom": atom, "updated": self._bump()}
        return {}

    def info(self, nid, with_json=True, with_comment=False):
        n = self.notes[nid]
        return {"info": {"base": {"updatedAt": str(n["updated"]), "title": ""},
                         "content": {"json": n["atom"], "html": ""}}}

    def tags(self, nid, add=None, remove=None):
        self.tagged.append((nid, add, remove))
        return {}

    def user_edit(self, nid, fn):
        md = atom_to_md(self.notes[nid]["atom"])
        self.notes[nid] = {"atom": md_to_atom(fn(md)), "updated": self._bump()}


class FakeBoard:
    ready = True

    def __init__(self):
        self.confirms: list[dict] = []
        self.desk: list[dict] = []
        self.cards: list[str] = []
        self.desks: list = []

    def url(self, k):
        return f"https://space.24haowan.com/p/{k}"

    def publish_card(self, md, options, note):
        self.cards.append(md)
        self.card_options = options
        return {"status": "published"}

    def read_confirms(self):
        out, self.confirms = self.confirms, []
        return out

    def read_desk(self):
        out, self.desk = self.desk, []
        return out

    def publish_desk(self, notices, drafts, note, quiet=False):
        self.desks.append((notices, drafts))
        return {"status": "published"}

    def update_desk_form(self, drafts):
        pass


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db.init_schema(DSN)
    c = db.connect(DSN)
    for t in ("kv", "materials", "collect_stats", "topics", "drafts", "draft_versions", "notices", "quota", "job_runs"):
        c.execute(f"TRUNCATE {t} RESTART IDENTITY")
    monkeypatch.setattr(config, "ASSETS_DIR", tmp_path)

    def fake_fetch(url, client=None):
        return Article(url=url, title="Introducing GPT-6", author="OpenAI", published_at=AT - timedelta(hours=2),
                       text_md="GPT-6 正文。\n\n![](https://openai.com/img.png)", html="<p>GPT-6</p>", site="OpenAI")
    monkeypatch.setattr(jobs, "fetch_article", fake_fetch)
    llm, mw, bd = FakeLLM(), FakeMowen(), FakeBoard()

    def ctx(at=AT):
        x = jobs.Ctx(c, at)
        x._llm, x._mowen, x._board = llm, mw, bd
        return x
    yield c, ctx, llm, mw, bd
    c.close()


def _mat(c, mid, url, author, source_id, layer, title, stype="官方"):
    c.execute("""INSERT INTO materials(id,stage,source_id,source_type,layer,url,url_norm,platform,author,published_at,
                 title,summary,session) VALUES(%s,'staged',%s,%s,%s,%s,%s,'web',%s,%s,%s,'摘要',%s)""",
              (mid, source_id, stype, layer, url, url, author, AT - timedelta(hours=3), title, "2026-10-29-早场"))


def test_full_flow(env):
    c, ctx, llm, mw, bd = env
    _mat(c, "S20261029-001", "https://openai.com/index/gpt-6", "OpenAI", "openai", "L1", "Introducing GPT-6")
    _mat(c, "S20261029-002", "https://www.qbitai.com/gpt6", "量子位", "qbitai", "L3", "OpenAI 发布 GPT-6", "媒体")
    _mat(c, "S20261029-003", "https://example.com/vuln", "某媒体", "x", "L3", "某系统漏洞", "媒体")

    # WF-03 选题加工
    s = jobs.job_process(ctx())
    assert s["passed"] == 2 and s["dropped"] == 1 and len(s["topics"]) == 1
    tid = s["topics"][0]
    t = c.execute("SELECT * FROM topics WHERE id=%s", (tid,)).fetchone()
    assert t["title"] == "OpenAI 发布 GPT-6" and t["status"] == "推荐"
    assert t["official"]["dir"].startswith("assets/official/" + tid)
    assert (config.ASSETS_DIR / "official").exists()
    assert c.execute("SELECT count(*) n FROM materials WHERE topic_id=%s", (tid,)).fetchone()["n"] == 2

    # WF-04 选题卡
    r = jobs.job_card(ctx(AT.replace(hour=8, minute=0)))
    assert r["picked"] == [tid] and tid in bd.cards[-1] and bd.card_options[0].startswith(tid)

    # WF-05：观点金句没写观点 → 不出稿
    bd.confirms = [{"tid": tid, "types": ["长文"], "style": "观点金句", "opinion": ""}]
    g = jobs.job_generate(ctx())
    assert g["drafts"] == [] and "缺观点" in g["skipped"][0]
    # 补上观点 → 出稿
    bd.confirms = [{"tid": tid, "types": ["长文", "快讯"], "style": "观点金句", "opinion": "我觉得很重要"}]
    g = jobs.job_generate(ctx())
    assert sorted(g["drafts"]) == sorted([f"{tid}-快讯", f"{tid}-长文"])
    d = c.execute("SELECT * FROM drafts WHERE id=%s", (f"{tid}-长文",)).fetchone()
    assert d["status"] == "待确认" and d["version_label"] == "草稿1"
    assert "附录（系统生成）" in atom_to_md(mw.notes[d["mowen_note_id"]]["atom"])
    assert c.execute("SELECT status FROM topics WHERE id=%s", (tid,)).fetchone()["status"] == "已出稿"
    # 重复确认不会重复出稿
    bd.confirms = [{"tid": tid, "types": ["长文"], "style": "观点金句", "opinion": "x"}]
    assert jobs.job_generate(ctx())["drafts"] == []

    # WF-06 改稿：你直接改第一段 + 在第二段写【改】
    nid = d["mowen_note_id"]
    mw.user_edit(nid, lambda md: md.replace("第一段：OpenAI 发布 GPT-6。", "第一段：我亲手改的。")
                 .replace("第二段：上下文 200 万 token。", "第二段：上下文 200 万 token。【改：更口语】"))
    bd.desk = [{"draft_id": d["id"], "action": "待修改", "note": ""}]
    out = jobs.job_revise(ctx())
    assert out[d["id"]] == "草稿2"
    md2 = atom_to_md(mw.notes[nid]["atom"])
    assert "第一段：我亲手改的。" in md2 and "改得更口语了" in md2 and "【改" not in md2
    assert "修改记录（系统生成）" in md2

    # 改稿台选「确认」→ 定稿（正文末尾残留的状态行不会进定稿）
    mw.user_edit(nid, lambda md: md + "\n\n【确认】\n")
    bd.desk = [{"draft_id": d["id"], "action": "确认", "note": ""}]
    out = jobs.job_revise(ctx())
    assert out[d["id"]] == "已定稿"
    d = c.execute("SELECT * FROM drafts WHERE id=%s", (d["id"],)).fetchone()
    assert d["status"] == "已定稿" and d["version_label"] == "定稿"
    assert any(add == ["定稿"] for _, add, _ in mw.tagged)

    # 放弃快讯
    bd.desk = [{"draft_id": f"{tid}-快讯", "action": "放弃", "note": ""}]
    assert jobs.job_revise(ctx())[f"{tid}-快讯"] == "已放弃"

    # 提醒合并成一次推送到改稿台
    n = notify.flush(c, bd)
    assert n >= 4 and bd.desks
    assert c.execute("SELECT count(*) n FROM notices WHERE sent_at IS NULL").fetchone()["n"] == 0

    # WF-07 清理不报错
    assert "versions_deleted" in jobs.job_cleanup(ctx())


def test_quota_hook(env):
    c, *_ = env
    hook = jobs.quota_hook(c)
    assert all(hook("note/create", 3) for _ in range(3))
    assert hook("note/create", 3) is False


def test_revise_waits_when_mowen_quota_out(env):
    from app.clients.mowen import MowenError
    c, ctx, llm, mw, bd = env
    nid = mw.create(md_to_atom("# 标题\n\n正文。\n"))
    c.execute("""INSERT INTO topics(id,title,status) VALUES('T20261029-07','测试','已出稿')""")
    c.execute("""INSERT INTO drafts(id,topic_id,dtype,title,mowen_note_id,mowen_url,status)
                 VALUES('T20261029-07-快讯','T20261029-07','快讯','标题',%s,'u','待确认')""", (nid,))
    c.execute("""INSERT INTO draft_versions(draft_id,version,label,md,appendix_md) VALUES
                 ('T20261029-07-快讯',1,'草稿1','# 标题\n\n正文。','')""")
    real_info = mw.info
    mw.info = lambda *a, **k: (_ for _ in ()).throw(MowenError("QUOTA", "api=NoteInfo"))
    bd.desk = [{"draft_id": "T20261029-07-快讯", "action": "确认", "note": ""}]
    out = jobs.job_revise(ctx())
    assert "明天自动处理" in out["T20261029-07-快讯"]
    assert c.execute("SELECT status FROM drafts WHERE id='T20261029-07-快讯'").fetchone()["status"] == "待确认"
    assert jobs.job_revise(ctx())["T20261029-07-快讯"].startswith("墨问额度")  # 没有新信号也会重试
    assert c.execute("SELECT count(*) n FROM notices WHERE title LIKE '%明天自动处理%'").fetchone()["n"] == 1
    mw.info = real_info  # 第二天额度恢复
    assert jobs.job_revise(ctx())["T20261029-07-快讯"] == "已定稿"
    assert jobs.job_revise(ctx()) == {}
