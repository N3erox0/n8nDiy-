"""worker HTTP 入口。n8n 定时调用 POST /jobs/<name>，任务在后台线程跑，立即返回。

鉴权：请求头 X-Worker-Token 必须等于环境变量 WORKER_TOKEN。
同名任务不并发（Postgres advisory lock）；n8n 重复触发时返回 busy。
"""
from __future__ import annotations

import logging
import os
import threading
import traceback
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from . import config, db, notify
from .jobs import JOBS, Ctx
from .util import now

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")
app = FastAPI(title="cf-worker", version="1.0")

QUIET_ERRORS = {"collect"}  # 采集里个别源失败不算任务失败，汇总进选题卡统计行


def auth(x_worker_token: str | None = Header(default=None)) -> None:
    want = os.environ.get("WORKER_TOKEN", "")
    if want and x_worker_token != want:
        raise HTTPException(401, "bad token")


@app.on_event("startup")
def _startup() -> None:
    db.init_schema()


def _parse_at(at: str | None) -> datetime | None:
    if not at:
        return None
    t = datetime.fromisoformat(at)
    return t if t.tzinfo else t.replace(tzinfo=config.TZ)


def run_job(name: str, params: dict[str, Any], at: datetime | None = None) -> dict:
    """在当前线程跑一个任务（拿锁 → 记 job_runs → 跑 → 刷提醒 → 放锁）。"""
    lock = db.connect()
    c = db.connect()
    lock_name = f"job:{name}" + (f":{params['group']}" if params.get("group") else "")
    try:
        if not db.try_lock(lock, lock_name):
            return {"busy": True, "job": name}
        rid = c.execute("INSERT INTO job_runs(job, params) VALUES(%s,%s) RETURNING id",
                        (name, db.J({**params, "at": at.isoformat() if at else None}))).fetchone()["id"]
        ctx = Ctx(c, at)
        try:
            summary = JOBS[name](ctx, **params)
            c.execute("UPDATE job_runs SET ok=TRUE, summary=%s, finished_at=now() WHERE id=%s",
                      (db.J(summary), rid))
            result = {"ok": True, "run_id": rid, "summary": summary}
        except Exception as e:  # noqa: BLE001
            log.exception("任务 %s 失败", name)
            err = f"{type(e).__name__}: {e}"[:500]
            c.execute("UPDATE job_runs SET ok=FALSE, error=%s, finished_at=now() WHERE id=%s",
                      (err + "\n" + traceback.format_exc()[-1500:], rid))
            prev = c.execute("SELECT error FROM job_runs WHERE job=%s AND id<%s ORDER BY id DESC LIMIT 1",
                             (name, rid)).fetchone()
            if not (prev and (prev["error"] or "").startswith(err[:80])):
                notify.add(c, "错误", f"任务 {name} 失败：{err[:40]}")
            result = {"ok": False, "run_id": rid, "error": err}
        try:
            notify.flush(c, ctx.board if ctx._board else None)
        except Exception as e:  # noqa: BLE001
            log.warning("提醒推送失败：%s", e)
        return result
    finally:
        try:
            db.unlock(lock, lock_name)
        finally:
            lock.close()
            c.close()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "time": now().isoformat()}


@app.get("/status", dependencies=[Depends(auth)])
def status() -> dict:
    with db.conn() as c:
        runs = c.execute("SELECT DISTINCT ON (job) job, ok, error, started_at, finished_at, summary FROM job_runs "
                         "ORDER BY job, id DESC").fetchall()
        quota = c.execute("SELECT api, n FROM quota WHERE day=%s", (now().date(),)).fetchall()
        layout = db.kv_get(c, "tdocs_layout", {}) or {}
        lp = db.kv_get(c, "lp_layout", {}) or {}
    return {"secrets": config.secrets_status(),
            "tdocs": {"space_id": layout.get("space_id"), "sheets_ok": layout.get("sheets_ok"),
                      "sheets": {k: v.get("url") for k, v in (layout.get("sheets") or {}).items()}},
            "livepage": {k: (lp.get(k) or {}).get("url") for k in ("card", "desk")},
            "mowen_quota_today": {q["api"]: q["n"] for q in quota},
            "last_runs": {r["job"]: {"ok": r["ok"], "at": r["started_at"].isoformat(),
                                     "error": (r["error"] or "")[:200] or None} for r in runs}}


@app.post("/jobs/{name}", dependencies=[Depends(auth)])
def start_job(name: str, group: str | None = Query(None), at: str | None = Query(None),
              wait: bool = Query(False)) -> dict:
    if name not in JOBS:
        raise HTTPException(404, f"unknown job {name}")
    params: dict[str, Any] = {}
    if name == "collect":
        params["group"] = group or "G1"
    t_at = _parse_at(at)
    if wait:
        return run_job(name, params, t_at)
    threading.Thread(target=run_job, args=(name, params, t_at), daemon=True, name=f"job-{name}").start()
    return {"started": True, "job": name, **params}


@app.get("/jobs/runs/{run_id}", dependencies=[Depends(auth)])
def job_run(run_id: int) -> dict:
    with db.conn() as c:
        r = c.execute("SELECT * FROM job_runs WHERE id=%s", (run_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()}
