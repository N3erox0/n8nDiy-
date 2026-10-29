"""提醒：先记进 notices 表，任务结束时统一刷到活页「改稿台」发一个新版本 → 服务号推送。

只发标题和链接，不发正文。同一任务里的多条提醒合并成一次推送。
"""
from __future__ import annotations

import logging

from .board import Board
from .util import short

log = logging.getLogger("notify")

ACTIVE = ("待确认", "待修改", "修改中", "确认", "放弃")


def add(c, kind: str, title: str, url: str | None = None, body: str = "") -> None:
    c.execute("INSERT INTO notices(kind,title,body,url) VALUES(%s,%s,%s,%s)", (kind, short(title, 60), body, url))


def active_drafts(c) -> list[dict]:
    return c.execute(
        "SELECT id,title,version_label,status,mowen_url FROM drafts WHERE status = ANY(%s) "
        "ORDER BY created_at DESC LIMIT 30", (list(ACTIVE),)).fetchall()


def flush(c, board: Board | None = None) -> int:
    """把没发出去的提醒刷到改稿台。返回本次发出的条数。"""
    pending = c.execute("SELECT id,kind,title,url FROM notices WHERE sent_at IS NULL ORDER BY id").fetchall()
    if not pending:
        return 0
    board = board or Board(c)
    if not board.ready:
        log.warning("活页未初始化，%d 条提醒暂存", len(pending))
        return 0
    recent = c.execute("SELECT kind,title,url,created_at FROM notices ORDER BY id DESC LIMIT 10").fetchall()
    note = pending[-1]["title"] if len(pending) == 1 else f"{len(pending)} 条新提醒"
    board.publish_desk(recent, active_drafts(c), note=short(note, 20), quiet=False)
    c.execute("UPDATE notices SET sent_at=now(), channel='livepage' WHERE id = ANY(%s)",
              ([p["id"] for p in pending],))
    return len(pending)
