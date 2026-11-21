"""基于本地 SQLite 的防封冷却限频队列（加好友风控守护）。"""

from __future__ import annotations

import logging
import os
import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from adapters.base import IMAdapter

logger = logging.getLogger("im_gateway.rate_limiter")


def _parse_bool_env(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class FriendRequestRateLimiter:
    """
    SQLite 持久化防封冷却限频队列：
    - 每小时最多发起 max_per_hour（默认 3）次好友申请；
    - 每次申请之间随机间隔 min_interval_sec ~ max_interval_sec（默认 30 ~ 120 秒）；
    - 同一 (platform, user_id) 在 dedup_days（默认 30 天）内不重复骚扰；
    - 支持 DRY_RUN_ADD_FRIEND 安全演练开关（默认 true，演练模式仅记录不实际调用平台加好友）。
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        max_per_hour: int = 3,
        min_interval_sec: float = 30.0,
        max_interval_sec: float = 120.0,
        dedup_days: int = 30,
        dry_run: Optional[bool] = None,
    ):
        default_db = Path(__file__).resolve().parent / "im_gateway.db"
        self.db_path = Path(db_path or os.getenv("IM_GATEWAY_DB_PATH") or default_db)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_per_hour = max_per_hour
        self.min_interval_sec = float(min_interval_sec)
        self.max_interval_sec = float(max_interval_sec)
        self.dedup_days = int(dedup_days)
        self.dry_run = _parse_bool_env("DRY_RUN_ADD_FRIEND", True) if dry_run is None else bool(dry_run)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS friend_request_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    group_id TEXT,
                    nickname TEXT,
                    remark TEXT NOT NULL,
                    intent_score INTEGER DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    scheduled_at REAL NOT NULL,
                    executed_at REAL,
                    error_msg TEXT,
                    created_at_ts REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS friend_request_history (
                    platform TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    last_requested_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    remark TEXT,
                    PRIMARY KEY (platform, user_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_frq_status_sched ON friend_request_queue(status, scheduled_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_frq_created ON friend_request_queue(created_at_ts)"
            )
        finally:
            conn.close()

    def enqueue_friend_request(
        self,
        platform: str,
        user_id: str,
        remark: str,
        group_id: Optional[str] = None,
        nickname: str = "",
        intent_score: int = 0,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """校验 30 天去重与每小时额度后，将好友申请压入冷却队列。"""
        now = float(now_ts if now_ts is not None else time.time())
        dedup_cutoff = now - self.dedup_days * 86400.0
        hour_cutoff = now - 3600.0

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # 1. 同一 platform + user_id 30 天内不重复骚扰（查历史表与当前队列）
                hist = conn.execute(
                    """
                    SELECT last_requested_at, status FROM friend_request_history
                    WHERE platform = ? AND user_id = ? AND last_requested_at >= ?
                    """,
                    (platform, user_id, dedup_cutoff),
                ).fetchone()
                if hist is not None:
                    conn.execute("ROLLBACK")
                    return {
                        "enqueued": False,
                        "reason": "DEDUP_30D",
                        "message": f"用户 {platform}:{user_id} 在 {self.dedup_days} 天内已触达过，已去重跳过",
                    }

                pending_dup = conn.execute(
                    """
                    SELECT id, status FROM friend_request_queue
                    WHERE platform = ? AND user_id = ?
                      AND status IN ('PENDING', 'SENT', 'DRY_RUN')
                      AND created_at_ts >= ?
                    LIMIT 1
                    """,
                    (platform, user_id, dedup_cutoff),
                ).fetchone()
                if pending_dup is not None:
                    conn.execute("ROLLBACK")
                    return {
                        "enqueued": False,
                        "reason": "DEDUP_30D",
                        "message": f"用户 {platform}:{user_id} 已在队列或 30 天记录中，已去重跳过",
                    }

                # 2. 滚动 1 小时内最多发起 max_per_hour 次
                hour_cnt_row = conn.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM friend_request_queue
                    WHERE status IN ('PENDING', 'SENT', 'DRY_RUN')
                      AND created_at_ts >= ?
                    """,
                    (hour_cutoff,),
                ).fetchone()
                hourly_used = int(hour_cnt_row["cnt"]) if hour_cnt_row else 0
                if hourly_used >= self.max_per_hour:
                    conn.execute("ROLLBACK")
                    return {
                        "enqueued": False,
                        "reason": "HOURLY_QUOTA_EXCEEDED",
                        "hourly_used": hourly_used,
                        "max_per_hour": self.max_per_hour,
                        "message": f"已达每小时加好友上限 ({hourly_used}/{self.max_per_hour})，本次已限频拦截",
                    }

                # 3. 计算随机 30-120 秒冷却间隔
                last_row = conn.execute(
                    """
                    SELECT MAX(scheduled_at) AS max_sched FROM friend_request_queue
                    WHERE status IN ('PENDING', 'SENT', 'DRY_RUN')
                    """
                ).fetchone()
                last_sched = float(last_row["max_sched"]) if (last_row and last_row["max_sched"] is not None) else 0.0
                jitter = random.uniform(self.min_interval_sec, self.max_interval_sec)
                base_time = max(now, last_sched) if last_sched > 0 else now
                scheduled_at = base_time + jitter

                cur = conn.execute(
                    """
                    INSERT INTO friend_request_queue (
                        platform, user_id, group_id, nickname, remark,
                        intent_score, status, scheduled_at, created_at_ts
                    ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                    """,
                    (platform, user_id, group_id, nickname, remark, int(intent_score), scheduled_at, now),
                )
                queue_id = cur.lastrowid
                conn.execute("COMMIT")

                logger.info(
                    "高意向用户已入冷却队列: id=%s %s:%s (score=%d, delay=%.1fs, dry_run=%s)",
                    queue_id,
                    platform,
                    user_id,
                    intent_score,
                    scheduled_at - now,
                    self.dry_run,
                )
                return {
                    "enqueued": True,
                    "queue_id": queue_id,
                    "scheduled_at": scheduled_at,
                    "delay_sec": round(scheduled_at - now, 2),
                    "hourly_used": hourly_used + 1,
                    "max_per_hour": self.max_per_hour,
                    "dry_run": self.dry_run,
                }
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    async def process_due_requests(
        self,
        adapters: Dict[str, IMAdapter],
        now_ts: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """消费所有已到冷却时间的 PENDING 加好友请求。"""
        now = float(now_ts if now_ts is not None else time.time())
        conn = self._connect()
        try:
            due_rows = conn.execute(
                """
                SELECT * FROM friend_request_queue
                WHERE status = 'PENDING' AND scheduled_at <= ?
                ORDER BY scheduled_at ASC, id ASC
                """,
                (now,),
            ).fetchall()
        finally:
            conn.close()

        processed: List[Dict[str, Any]] = []
        for row in due_rows:
            qid = int(row["id"])
            platform = str(row["platform"])
            user_id = str(row["user_id"])
            group_id = row["group_id"]
            remark = str(row["remark"])

            if self.dry_run:
                logger.info(
                    "[DRY_RUN_ADD_FRIEND=true] 演练跳过真实加好友: qid=%d %s:%s remark=%s",
                    qid,
                    platform,
                    user_id,
                    remark,
                )
                self._mark_done(qid, platform, user_id, remark, "DRY_RUN", now, None)
                processed.append({"queue_id": qid, "platform": platform, "user_id": user_id, "status": "DRY_RUN"})
                continue

            adapter = adapters.get(platform)
            if adapter is None:
                err = f"No adapter registered for platform={platform}"
                self._mark_done(qid, platform, user_id, remark, "FAILED", now, err, record_history=False)
                processed.append({"queue_id": qid, "platform": platform, "user_id": user_id, "status": "FAILED", "error": err})
                continue

            try:
                resp = await adapter.request_add_friend(user_id=user_id, remark=remark, group_id=group_id)
                self._mark_done(qid, platform, user_id, remark, "SENT", now, None)
                processed.append({"queue_id": qid, "platform": platform, "user_id": user_id, "status": "SENT", "response": resp})
            except Exception as exc:
                self._mark_done(qid, platform, user_id, remark, "FAILED", now, str(exc), record_history=False)
                processed.append({"queue_id": qid, "platform": platform, "user_id": user_id, "status": "FAILED", "error": str(exc)})

        return processed

    def _mark_done(
        self,
        queue_id: int,
        platform: str,
        user_id: str,
        remark: str,
        status: str,
        executed_at: float,
        error_msg: Optional[str],
        record_history: bool = True,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE friend_request_queue
                SET status = ?, executed_at = ?, error_msg = ?
                WHERE id = ?
                """,
                (status, executed_at, error_msg, queue_id),
            )
            if record_history:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO friend_request_history (
                        platform, user_id, last_requested_at, status, remark
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (platform, user_id, executed_at, status, remark),
                )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def list_leads(self, limit: int = 100, now_ts: Optional[float] = None) -> List[Dict[str, Any]]:
        """列出冷却队列中的拓客线索及距下次执行的倒计时秒数。"""
        now = float(now_ts if now_ts is not None else time.time())
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM friend_request_queue
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                sched = float(d.get("scheduled_at") or 0.0)
                d["countdown_sec"] = max(0, int(round(sched - now))) if d.get("status") == "PENDING" else 0
                out.append(d)
            return out
        finally:
            conn.close()

    async def handle_lead_action(
        self,
        queue_id: int,
        action: str,
        adapters: Dict[str, IMAdapter],
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """人工对单条线索执行批准（立即发送或演练）或忽略（标记 SKIPPED）。"""
        now = float(now_ts if now_ts is not None else time.time())
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM friend_request_queue WHERE id = ?", (int(queue_id),)).fetchone()
            if row is None:
                raise KeyError(f"Queue item #{queue_id} not found")
        finally:
            conn.close()

        platform = str(row["platform"])
        user_id = str(row["user_id"])
        group_id = row["group_id"]
        remark = str(row["remark"])

        if action == "ignore":
            self._mark_done(queue_id, platform, user_id, remark, "SKIPPED", now, "Ignored by operator", record_history=False)
            return {"queue_id": queue_id, "status": "SKIPPED"}

        if action == "approve":
            if self.dry_run:
                self._mark_done(queue_id, platform, user_id, remark, "DRY_RUN", now, None, record_history=True)
                return {"queue_id": queue_id, "status": "DRY_RUN", "dry_run": True}
            adapter = adapters.get(platform)
            if adapter is None:
                raise RuntimeError(f"No adapter registered for platform={platform}")
            resp = await adapter.request_add_friend(user_id=user_id, remark=remark, group_id=group_id)
            self._mark_done(queue_id, platform, user_id, remark, "SENT", now, None, record_history=True)
            return {"queue_id": queue_id, "status": "SENT", "response": resp}

        raise ValueError(f"Invalid action: {action}. Expected 'approve' or 'ignore'.")

    def get_stats(self, now_ts: Optional[float] = None) -> Dict[str, Any]:
        now = float(now_ts if now_ts is not None else time.time())
        hour_cutoff = now - 3600.0
        conn = self._connect()
        try:
            counts = {
                row["status"]: int(row["cnt"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS cnt FROM friend_request_queue GROUP BY status"
                ).fetchall()
            }
            hourly_used = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM friend_request_queue
                    WHERE status IN ('PENDING', 'SENT', 'DRY_RUN') AND created_at_ts >= ?
                    """,
                    (hour_cutoff,),
                ).fetchone()["cnt"]
            )
            history_total = int(conn.execute("SELECT COUNT(*) AS cnt FROM friend_request_history").fetchone()["cnt"])
            return {
                "dry_run": self.dry_run,
                "max_per_hour": self.max_per_hour,
                "hourly_used": hourly_used,
                "dedup_days": self.dedup_days,
                "queue_counts": counts,
                "history_total": history_total,
            }
        finally:
            conn.close()
