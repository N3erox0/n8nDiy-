"""Postgres 访问与表结构（业务库 cf）。"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value JSONB,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS materials (
  id TEXT PRIMARY KEY,                 -- S20261028-001
  stage TEXT NOT NULL DEFAULT 'staged',-- staged=暂存 / library=已入库 / dropped=未过线
  source_id TEXT,                      -- sources.yaml 里的 id，手工为 manual
  source_type TEXT,                    -- 官方/媒体/公众号/X/关键词/链接入表/手工转存
  layer TEXT,                          -- L1..L6 / KW / MANUAL
  url TEXT,
  url_norm TEXT,
  platform TEXT,
  author TEXT,
  published_at TIMESTAMPTZ,
  date_only BOOLEAN DEFAULT FALSE,
  title TEXT,
  summary TEXT,
  content_md TEXT,
  facts TEXT,
  quotes TEXT,
  is_key BOOLEAN DEFAULT FALSE,
  in_scope BOOLEAN,
  brand TEXT,
  theme TEXT,
  event_key TEXT,
  density REAL,
  heat REAL,
  local_dir TEXT,
  doc_id TEXT,
  doc_url TEXT,
  status TEXT NOT NULL DEFAULT '候选',  -- 待补全/候选/已用/失效
  topic_id TEXT,
  note TEXT,
  session TEXT,                         -- 2026-10-28-早场
  sheet_record_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_materials_url ON materials(url_norm) WHERE url_norm IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_materials_stage ON materials(stage, created_at);

CREATE TABLE IF NOT EXISTS collect_stats (
  id BIGSERIAL PRIMARY KEY,
  session TEXT, grp TEXT, source_id TEXT, source_name TEXT, method TEXT,
  status TEXT, items INT, new_items INT, stored INT, reason TEXT, secs REAL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_stats_session ON collect_stats(session);

CREATE TABLE IF NOT EXISTS topics (
  id TEXT PRIMARY KEY,                 -- T20261028-01
  session TEXT,                        -- 早场/晚场/指定
  origin TEXT,                         -- 热点/指定/日历
  title TEXT,
  reasons JSONB DEFAULT '[]',
  material_ids JSONB DEFAULT '[]',
  links JSONB DEFAULT '[]',             -- [{author,time,url}] 第一条为主素材
  cluster_key TEXT,
  score REAL,
  grade TEXT,
  on_card BOOLEAN DEFAULT FALSE,
  confirmed BOOLEAN DEFAULT FALSE,
  types JSONB DEFAULT '[]',
  style TEXT,
  opinion TEXT,
  official JSONB DEFAULT '{}',          -- {dir, docs:[], videos:[]}
  status TEXT NOT NULL DEFAULT '推荐',   -- 待补全/推荐/生成中/已出稿/已作废/失败
  pushed_at TIMESTAMPTZ,
  note TEXT,
  event_date DATE,
  sheet_record_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS calendar (
  id BIGSERIAL PRIMARY KEY,
  event_date DATE NOT NULL,
  event TEXT NOT NULL,
  brand TEXT,
  url TEXT,
  note TEXT,
  used BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS drafts (
  id TEXT PRIMARY KEY,                 -- T20261028-01-长文
  topic_id TEXT NOT NULL,
  dtype TEXT NOT NULL,                 -- 快讯/长文/图文
  style TEXT,
  title TEXT,
  mowen_note_id TEXT,
  mowen_url TEXT,
  version INT NOT NULL DEFAULT 1,
  version_label TEXT NOT NULL DEFAULT '草稿1',
  status TEXT NOT NULL DEFAULT '待确认',  -- 待确认/待修改/修改中/确认/已定稿/放弃/已放弃/失败
  revise_note TEXT,
  words INT, image_slots INT,
  check_result TEXT,
  last_seen_updated BIGINT,            -- 墨问 updated_at，用来判断你有没有改过
  final_doc_id TEXT, final_url TEXT,
  note TEXT,
  sheet_record_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS draft_versions (
  id BIGSERIAL PRIMARY KEY,
  draft_id TEXT NOT NULL,
  version INT NOT NULL,
  label TEXT,
  md TEXT,                             -- 正文 Markdown（不含附录）
  appendix_md TEXT,
  atom JSONB,
  fact_base TEXT,
  check_json JSONB,
  change_log JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_dv_draft ON draft_versions(draft_id, version);

CREATE TABLE IF NOT EXISTS notices (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT, title TEXT, body TEXT, url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at TIMESTAMPTZ,
  channel TEXT
);

CREATE TABLE IF NOT EXISTS quota (
  day DATE NOT NULL,
  api TEXT NOT NULL,
  n INT NOT NULL DEFAULT 0,
  PRIMARY KEY (day, api)
);

CREATE TABLE IF NOT EXISTS job_runs (
  id BIGSERIAL PRIMARY KEY,
  job TEXT, params JSONB, ok BOOLEAN, summary JSONB, error TEXT,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);
"""


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or config.database_url(), row_factory=dict_row, autocommit=True)


@contextmanager
def conn(url: str | None = None) -> Iterator[psycopg.Connection]:
    c = connect(url)
    try:
        yield c
    finally:
        c.close()


def init_schema(url: str | None = None) -> None:
    with conn(url) as c:
        c.execute(SCHEMA)


def J(v: Any) -> Jsonb:
    return Jsonb(v)


def kv_get(c: psycopg.Connection, key: str, default: Any = None) -> Any:
    row = c.execute("SELECT value FROM kv WHERE key=%s", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(c: psycopg.Connection, key: str, value: Any) -> None:
    c.execute(
        "INSERT INTO kv(key,value,updated_at) VALUES(%s,%s,now()) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
        (key, J(value)),
    )


def try_lock(c: psycopg.Connection, name: str) -> bool:
    key = int(hashlib.md5(name.encode()).hexdigest()[:15], 16)
    return bool(c.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,)).fetchone()["ok"])


def unlock(c: psycopg.Connection, name: str) -> None:
    key = int(hashlib.md5(name.encode()).hexdigest()[:15], 16)
    c.execute("SELECT pg_advisory_unlock(%s)", (key,))


def next_id(c: psycopg.Connection, prefix: str, width: int, table: str) -> str:
    """生成 S20261028-001 / T20261028-01 这样的递增编号。"""
    row = c.execute(
        f"SELECT id FROM {table} WHERE id LIKE %s ORDER BY id DESC LIMIT 1", (prefix + "-%",)
    ).fetchone()
    n = 0
    if row:
        try:
            n = int(row["id"].split("-")[1][:width])
        except (IndexError, ValueError):
            n = 0
    return f"{prefix}-{n + 1:0{width}d}"


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)
