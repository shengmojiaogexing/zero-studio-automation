"""按联系人隔离的 SQLite 会话记忆与入站消息防重。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class ConversationStore:
    def __init__(
        self,
        db_path: Optional[Path] = None,
        context_ttl_sec: Optional[int] = None,
        history_limit: int = 40,
        cleanup_interval_sec: int = 300,
    ):
        default_db = Path(__file__).resolve().parent / "im_gateway.db"
        self.db_path = Path(db_path or os.getenv("IM_GATEWAY_DB_PATH") or default_db)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.context_ttl_sec = int(context_ttl_sec or os.getenv("IM_CONTEXT_TTL_SEC", "86400"))
        self.history_limit = max(8, int(history_limit))
        self.cleanup_interval_sec = max(30, int(cleanup_interval_sec))
        self._last_cleanup_at = 0.0
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
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_sessions (
                    platform TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    nickname TEXT NOT NULL DEFAULT '',
                    auto_remark TEXT NOT NULL DEFAULT '',
                    slots_json TEXT NOT NULL DEFAULT '{}',
                    ask_counts_json TEXT NOT NULL DEFAULT '{}',
                    booking_json TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_active_at REAL NOT NULL,
                    PRIMARY KEY (platform, chat_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_messages_chat
                    ON conversation_messages(platform, chat_id, id);
                CREATE TABLE IF NOT EXISTS processed_messages (
                    dedupe_key TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_processed_messages_created
                    ON processed_messages(created_at);
                CREATE TABLE IF NOT EXISTS conversation_feed (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT UNIQUE,
                    platform TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    chat_type TEXT NOT NULL DEFAULT 'private',
                    group_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    images_json TEXT NOT NULL DEFAULT '[]',
                    action TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    parent_id INTEGER,
                    created_at REAL NOT NULL,
                    processed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_feed_chat
                    ON conversation_feed(platform, chat_id, id);
                CREATE INDEX IF NOT EXISTS idx_conversation_feed_status
                    ON conversation_feed(status, created_at);
                CREATE TABLE IF NOT EXISTS conversation_feed_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._migrate_legacy_feed(conn)
        finally:
            conn.close()

    @staticmethod
    def _migrate_legacy_feed(conn: sqlite3.Connection) -> None:
        """升级时把原 AI 记忆库里的双方消息迁入永久档案，且只迁移一次。"""
        migrated = conn.execute(
            "SELECT value FROM conversation_feed_meta WHERE key='legacy_memory_migrated'"
        ).fetchone()
        if migrated and migrated["value"] == "1":
            return
        rows = conn.execute(
            """SELECT m.id, m.platform, m.chat_id, m.role, m.text, m.created_at,
                      COALESCE(s.nickname, '') AS nickname
               FROM conversation_messages m
               LEFT JOIN conversation_sessions s
                 ON s.platform=m.platform AND s.chat_id=m.chat_id
               ORDER BY m.id"""
        ).fetchall()
        current_incoming: Dict[Tuple[str, str], int] = {}
        for row in rows:
            direction = "out" if row["role"] == "assistant" else "in"
            chat_key = (str(row["platform"]), str(row["chat_id"]))
            parent_id = current_incoming.get(chat_key) if direction == "out" else None
            cursor = conn.execute(
                """INSERT OR IGNORE INTO conversation_feed
                (dedupe_key, platform, chat_id, chat_type, user_id, nickname, direction,
                 text, action, status, parent_id, created_at, processed_at)
                VALUES (?, ?, ?, 'private', ?, ?, ?, ?, 'LEGACY_MEMORY', ?, ?, ?, ?)""",
                (
                    f"legacy:{row['id']}",
                    row["platform"],
                    row["chat_id"],
                    row["chat_id"],
                    row["nickname"],
                    direction,
                    row["text"],
                    "" if direction == "out" else "needs_reply",
                    parent_id,
                    row["created_at"],
                    row["created_at"] if direction == "out" else None,
                ),
            )
            feed_id = int(cursor.lastrowid or 0)
            if not feed_id:
                existing = conn.execute(
                    "SELECT id FROM conversation_feed WHERE dedupe_key=?",
                    (f"legacy:{row['id']}",),
                ).fetchone()
                feed_id = int(existing["id"]) if existing else 0
            if direction == "in":
                current_incoming[chat_key] = feed_id
            elif parent_id:
                conn.execute(
                    """UPDATE conversation_feed SET status='replied', processed_at=?
                       WHERE id=? AND direction='in'""",
                    (row["created_at"], parent_id),
                )
        conn.execute(
            "INSERT OR REPLACE INTO conversation_feed_meta (key, value) VALUES ('legacy_memory_migrated', '1')"
        )

    @staticmethod
    def _empty_slots() -> Dict[str, str]:
        return {k: "" for k in ("cn", "character", "event_name", "date", "time", "remarks")}

    @staticmethod
    def _base_session() -> Dict[str, Any]:
        return {
            "slots": ConversationStore._empty_slots(),
            "history": [],
            "ask_counts": {},
            "_version": 0,
        }

    @staticmethod
    def _auto_remark(nickname: str, chat_id: str, slots: Dict[str, str], version: int) -> str:
        parts = [f"微信:{nickname or chat_id}"]
        if slots.get("cn"):
            parts.append(f"CN:{slots['cn']}")
        if slots.get("character"):
            parts.append(f"角色:{slots['character']}")
        event_date = " ".join(x for x in (slots.get("event_name", ""), slots.get("date", "")) if x)
        if event_date:
            parts.append(f"档期:{event_date}")
        parts.append(f"记忆v{version}")
        return " | ".join(parts)

    def load_session(
        self,
        platform: str,
        chat_id: str,
        nickname: str = "",
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        now = float(now_ts if now_ts is not None else time.time())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM conversation_sessions WHERE platform=? AND chat_id=?",
                (platform, chat_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO conversation_sessions
                    (platform, chat_id, nickname, auto_remark, created_at, updated_at, last_active_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (platform, chat_id, nickname, f"微信:{nickname or chat_id} | 记忆v0", now, now, now),
                )
                row = conn.execute(
                    "SELECT * FROM conversation_sessions WHERE platform=? AND chat_id=?",
                    (platform, chat_id),
                ).fetchone()
            elif now - float(row["last_active_at"]) > self.context_ttl_sec:
                version = int(row["version"]) + 1
                conn.execute(
                    "DELETE FROM conversation_messages WHERE platform=? AND chat_id=?",
                    (platform, chat_id),
                )
                conn.execute(
                    """UPDATE conversation_sessions SET nickname=?, auto_remark=?, slots_json='{}',
                    ask_counts_json='{}', booking_json='', version=?, updated_at=? WHERE platform=? AND chat_id=?""",
                    (
                        nickname,
                        f"微信:{nickname or chat_id} | 记忆v{version}",
                        version,
                        now,
                        platform,
                        chat_id,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM conversation_sessions WHERE platform=? AND chat_id=?",
                    (platform, chat_id),
                ).fetchone()
            elif nickname and nickname != row["nickname"]:
                conn.execute(
                    "UPDATE conversation_sessions SET nickname=?, updated_at=? WHERE platform=? AND chat_id=?",
                    (nickname, now, platform, chat_id),
                )
            conn.execute("COMMIT")

            messages = conn.execute(
                """SELECT role, text FROM conversation_messages
                WHERE platform=? AND chat_id=? ORDER BY id DESC LIMIT ?""",
                (platform, chat_id, self.history_limit),
            ).fetchall()
            session = self._base_session()
            session["slots"].update(json.loads(row["slots_json"] or "{}"))
            session["ask_counts"] = json.loads(row["ask_counts_json"] or "{}")
            session["history"] = [
                {"role": str(m["role"]), "text": str(m["text"])} for m in reversed(messages)
            ]
            session["_version"] = int(row["version"])
            session["auto_remark"] = str(row["auto_remark"] or "")
            if row["booking_json"]:
                session["booking"] = json.loads(row["booking_json"])
            return session
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def save_session(
        self,
        platform: str,
        chat_id: str,
        nickname: str,
        session: Dict[str, Any],
        now_ts: Optional[float] = None,
    ) -> None:
        now = float(now_ts if now_ts is not None else time.time())
        history = list(session.get("history") or [])[-self.history_limit:]
        slots = self._empty_slots()
        slots.update(session.get("slots") or {})
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT version FROM conversation_sessions WHERE platform=? AND chat_id=?",
                (platform, chat_id),
            ).fetchone()
            version = max(int(row["version"]) if row else 0, int(session.get("_version") or 0)) + 1
            remark = self._auto_remark(nickname, chat_id, slots, version)
            conn.execute(
                """INSERT INTO conversation_sessions
                (platform, chat_id, nickname, auto_remark, slots_json, ask_counts_json, booking_json,
                 version, created_at, updated_at, last_active_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, chat_id) DO UPDATE SET
                    nickname=excluded.nickname, auto_remark=excluded.auto_remark,
                    slots_json=excluded.slots_json, ask_counts_json=excluded.ask_counts_json,
                    booking_json=excluded.booking_json, version=excluded.version,
                    updated_at=excluded.updated_at, last_active_at=excluded.last_active_at""",
                (
                    platform,
                    chat_id,
                    nickname,
                    remark,
                    json.dumps(slots, ensure_ascii=False),
                    json.dumps(session.get("ask_counts") or {}, ensure_ascii=False),
                    json.dumps(session.get("booking"), ensure_ascii=False) if session.get("booking") else "",
                    version,
                    now,
                    now,
                    now,
                ),
            )
            conn.execute(
                "DELETE FROM conversation_messages WHERE platform=? AND chat_id=?",
                (platform, chat_id),
            )
            conn.executemany(
                """INSERT INTO conversation_messages
                (platform, chat_id, role, text, version, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (platform, chat_id, str(item.get("role") or ""), str(item.get("text") or ""), version, now)
                    for item in history
                ],
            )
            conn.execute("COMMIT")
            session["_version"] = version
            session["auto_remark"] = remark
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def record_incoming(
        self,
        *,
        platform: str,
        chat_type: str,
        group_id: Optional[str],
        user_id: str,
        nickname: str,
        text: str,
        images: Optional[List[str]],
        dedupe_key: str,
        now_ts: Optional[float] = None,
    ) -> int:
        """写入一条去重后的客户消息；初始状态为需要回复，处理完成后再更新。"""
        now = float(now_ts if now_ts is not None else time.time())
        chat_id = str(group_id or "") if chat_type == "group" else str(user_id)
        conn = self._connect()
        try:
            cursor = conn.execute(
                """INSERT INTO conversation_feed
                (dedupe_key, platform, chat_id, chat_type, group_id, user_id, nickname,
                 direction, text, images_json, action, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'in', ?, ?, 'RECEIVED', 'needs_reply', ?)""",
                (
                    dedupe_key,
                    platform,
                    chat_id,
                    chat_type,
                    str(group_id or ""),
                    user_id,
                    nickname,
                    text,
                    json.dumps(list(images or []), ensure_ascii=False),
                    now,
                ),
            )
            return int(cursor.lastrowid)
        finally:
            conn.close()

    def record_result(
        self,
        incoming_id: int,
        result: Dict[str, Any],
        now_ts: Optional[float] = None,
    ) -> None:
        """保存处理结论及全部回复气泡；无关静默与需要人工处理分开标记。"""
        now = float(now_ts if now_ts is not None else time.time())
        action = str(result.get("action") or "PROCESSED")
        raw_replies = result.get("replies")
        if isinstance(raw_replies, list):
            replies = [str(item).strip() for item in raw_replies if str(item).strip()]
        else:
            reply = str(result.get("reply") or "").strip()
            replies = [reply] if reply else []
        if replies:
            status = "replied"
        elif action.startswith("IGNORED_"):
            status = "no_reply"
        else:
            status = "needs_reply"
        assessment = result.get("assessment") if isinstance(result.get("assessment"), dict) else {}
        reason = str(result.get("reason") or assessment.get("reason") or "").strip()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            incoming = conn.execute(
                "SELECT * FROM conversation_feed WHERE id=? AND direction='in'",
                (incoming_id,),
            ).fetchone()
            if incoming is None:
                raise KeyError(f"入站消息不存在: {incoming_id}")
            conn.execute(
                """UPDATE conversation_feed
                   SET action=?, status=?, reason=?, processed_at=? WHERE id=?""",
                (action, status, reason, now, incoming_id),
            )
            conn.executemany(
                """INSERT INTO conversation_feed
                (platform, chat_id, chat_type, group_id, user_id, nickname, direction,
                 text, action, parent_id, created_at, processed_at)
                VALUES (?, ?, ?, ?, ?, ?, 'out', ?, ?, ?, ?, ?)""",
                [
                    (
                        incoming["platform"],
                        incoming["chat_id"],
                        incoming["chat_type"],
                        incoming["group_id"],
                        incoming["user_id"],
                        incoming["nickname"],
                        reply,
                        action,
                        incoming_id,
                        now + index * 0.001,
                        now + index * 0.001,
                    )
                    for index, reply in enumerate(replies)
                ],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def record_failure(self, incoming_id: int, error: BaseException) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """UPDATE conversation_feed
                   SET action='ERROR', status='needs_reply', reason=?, processed_at=?
                   WHERE id=? AND direction='in'""",
                (str(error), time.time(), incoming_id),
            )
        finally:
            conn.close()

    def record_outgoing(
        self,
        *,
        platform: str,
        chat_id: str,
        text: str,
        chat_type: str = "private",
        group_id: str = "",
        user_id: str = "",
        nickname: str = "",
        action: str = "MANUAL_REPLY",
        resolve_pending: bool = False,
        now_ts: Optional[float] = None,
    ) -> int:
        """记录人工回复或选图/交付通知，不进入 AI 上下文清理范围。"""
        now = float(now_ts if now_ts is not None else time.time())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            parent_id = None
            if resolve_pending:
                pending = conn.execute(
                    """SELECT id FROM conversation_feed
                       WHERE platform=? AND chat_id=? AND direction='in' AND status='needs_reply'
                       ORDER BY id DESC LIMIT 1""",
                    (platform, chat_id),
                ).fetchone()
                if pending:
                    parent_id = int(pending["id"])
                    conn.execute(
                        """UPDATE conversation_feed SET status='replied', processed_at=?
                           WHERE id=?""",
                        (now, parent_id),
                    )
            cursor = conn.execute(
                """INSERT INTO conversation_feed
                (platform, chat_id, chat_type, group_id, user_id, nickname, direction,
                 text, action, parent_id, created_at, processed_at)
                VALUES (?, ?, ?, ?, ?, ?, 'out', ?, ?, ?, ?, ?)""",
                (
                    platform,
                    chat_id,
                    chat_type,
                    group_id,
                    user_id or chat_id,
                    nickname,
                    text,
                    action,
                    parent_id,
                    now,
                    now,
                ),
            )
            conn.execute("COMMIT")
            return int(cursor.lastrowid)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        def parse_json(value: Any, fallback: Any) -> Any:
            try:
                return json.loads(value) if value else fallback
            except (TypeError, ValueError):
                return fallback

        return {
            "auto_remark": str(row["auto_remark"] or ""),
            "slots": parse_json(row["slots_json"], {}),
            "ask_counts": parse_json(row["ask_counts_json"], {}),
            "booking": parse_json(row["booking_json"], None),
            "version": int(row["session_version"] or 0),
            "updated_at": row["session_updated_at"],
            "last_active_at": row["last_active_at"],
        }

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        try:
            images = json.loads(row["images_json"] or "[]")
        except (TypeError, ValueError):
            images = []
        return {
            "id": int(row["id"]),
            "platform": row["platform"],
            "chat_id": row["chat_id"],
            "chat_type": row["chat_type"],
            "group_id": row["group_id"],
            "user_id": row["user_id"],
            "nickname": row["nickname"],
            "direction": row["direction"],
            "text": row["text"],
            "images": images,
            "action": row["action"],
            "status": row["status"],
            "reason": row["reason"],
            "parent_id": row["parent_id"],
            "created_at": row["created_at"],
            "processed_at": row["processed_at"],
        }

    def list_conversations(
        self,
        status: str = "all",
        query: str = "",
        limit: int = 200,
    ) -> Dict[str, Any]:
        """按联系人聚合永久消息档案，支持昵称、CN、备注和消息全文搜索。"""
        if status not in {"all", "needs_reply", "replied", "no_reply"}:
            raise ValueError("status 必须为 all/needs_reply/replied/no_reply")
        limit = max(1, min(500, int(limit)))
        query = str(query or "").strip()
        like = f"%{query}%"
        conn = self._connect()
        try:
            rows = conn.execute(
                """WITH matched AS (
                       SELECT DISTINCT f.platform, f.chat_id
                       FROM conversation_feed f
                       LEFT JOIN conversation_sessions s
                         ON s.platform=f.platform AND s.chat_id=f.chat_id
                       WHERE ?='' OR f.nickname LIKE ? OR f.user_id LIKE ? OR f.chat_id LIKE ?
                            OR f.text LIKE ? OR COALESCE(s.nickname, '') LIKE ?
                            OR COALESCE(s.auto_remark, '') LIKE ? OR COALESCE(s.slots_json, '') LIKE ?
                   )
                   SELECT f.*, s.nickname AS session_nickname, s.auto_remark,
                          s.slots_json, s.ask_counts_json, s.booking_json,
                          s.version AS session_version, s.updated_at AS session_updated_at,
                          s.last_active_at
                   FROM matched m
                   JOIN conversation_feed f ON f.platform=m.platform AND f.chat_id=m.chat_id
                   LEFT JOIN conversation_sessions s
                     ON s.platform=f.platform AND s.chat_id=f.chat_id
                   ORDER BY f.id""",
                (query, like, like, like, like, like, like, like),
            ).fetchall()
            session_rows = conn.execute(
                """SELECT platform, chat_id, nickname AS session_nickname, auto_remark,
                          slots_json, ask_counts_json, booking_json,
                          version AS session_version, updated_at AS session_updated_at,
                          last_active_at
                   FROM conversation_sessions"""
            ).fetchall()
        finally:
            conn.close()

        grouped: Dict[Tuple[str, str], List[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault((str(row["platform"]), str(row["chat_id"])), []).append(row)
        for session in session_rows:
            key = (str(session["platform"]), str(session["chat_id"]))
            if key in grouped:
                continue
            haystack = " ".join(
                str(session[field] or "")
                for field in ("session_nickname", "auto_remark", "slots_json", "chat_id")
            )
            if not query or query.lower() in haystack.lower():
                grouped[key] = [session]

        conversations: List[Dict[str, Any]] = []
        for (platform, chat_id), chat_rows in grouped.items():
            first = chat_rows[0]
            has_feed = "direction" in first.keys()
            messages = [self._message_from_row(row) for row in chat_rows] if has_feed else []
            incoming = [message for message in messages if message["direction"] == "in"]
            outgoing = [message for message in messages if message["direction"] == "out"]
            last = messages[-1] if messages else None
            last_incoming = incoming[-1] if incoming else None
            current_status = (
                str(last_incoming["status"] or "needs_reply")
                if last_incoming
                else ("replied" if last else "no_reply")
            )
            nickname = next(
                (
                    str(message["nickname"])
                    for message in reversed(messages)
                    if str(message.get("nickname") or "").strip()
                ),
                str(first["session_nickname"] or chat_id),
            )
            item = {
                "platform": platform,
                "chat_id": chat_id,
                "chat_type": (last or {}).get("chat_type", "private"),
                "group_id": (last or {}).get("group_id", ""),
                "user_id": (last_incoming or last or {}).get("user_id", chat_id),
                "nickname": nickname or str(first["session_nickname"] or chat_id),
                "status": current_status,
                "last_message": (last or {}).get("text", "") or ("[图片]" if (last or {}).get("images") else "暂无消息"),
                "last_direction": (last or {}).get("direction", ""),
                "last_action": (last_incoming or last or {}).get("action", ""),
                "last_reason": (last_incoming or {}).get("reason", ""),
                "last_activity": (last or {}).get("created_at") or first["session_updated_at"] or 0,
                "total_messages": len(messages),
                "incoming_count": len(incoming),
                "reply_count": len(outgoing),
                "needs_reply_count": sum(1 for message in incoming if message["status"] == "needs_reply"),
                "replied_count": sum(1 for message in incoming if message["status"] == "replied"),
                "no_reply_count": sum(1 for message in incoming if message["status"] == "no_reply"),
                "session": self._session_from_row(first) if "auto_remark" in first.keys() else None,
            }
            conversations.append(item)

        conversations.sort(key=lambda item: (float(item["last_activity"] or 0), item["chat_id"]), reverse=True)
        stats = {
            "total": len(conversations),
            "needs_reply": sum(1 for item in conversations if item["status"] == "needs_reply"),
            "replied": sum(1 for item in conversations if item["status"] == "replied"),
            "no_reply": sum(1 for item in conversations if item["status"] == "no_reply"),
        }
        filtered = conversations if status == "all" else [item for item in conversations if item["status"] == status]
        return {
            "status": "ok",
            "items": filtered[:limit],
            "total": len(filtered),
            "stats": stats,
            "query": query,
            "filter": status,
        }

    def get_conversation(
        self,
        platform: str,
        chat_id: str,
        limit: int = 500,
        before_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """读取单联系人的完整双方消息；before_id 用于继续加载更早档案。"""
        limit = max(1, min(1000, int(limit)))
        conn = self._connect()
        try:
            params: List[Any] = [platform, chat_id]
            before_sql = ""
            if before_id is not None:
                before_sql = "AND id<?"
                params.append(int(before_id))
            params.append(limit + 1)
            rows = conn.execute(
                f"""SELECT * FROM conversation_feed
                    WHERE platform=? AND chat_id=? {before_sql}
                    ORDER BY id DESC LIMIT ?""",
                params,
            ).fetchall()
            session = conn.execute(
                """SELECT platform, chat_id, nickname AS session_nickname, auto_remark,
                          slots_json, ask_counts_json, booking_json,
                          version AS session_version, updated_at AS session_updated_at,
                          last_active_at
                   FROM conversation_sessions WHERE platform=? AND chat_id=?""",
                (platform, chat_id),
            ).fetchone()
        finally:
            conn.close()
        has_more = len(rows) > limit
        visible = list(reversed(rows[:limit]))
        messages = [self._message_from_row(row) for row in visible]
        nickname = next(
            (str(row["nickname"]) for row in reversed(visible) if str(row["nickname"] or "").strip()),
            str(session["session_nickname"] or chat_id) if session else chat_id,
        )
        return {
            "status": "ok",
            "conversation": {
                "platform": platform,
                "chat_id": chat_id,
                "nickname": nickname,
                "chat_type": visible[-1]["chat_type"] if visible else "private",
                "group_id": visible[-1]["group_id"] if visible else "",
                "user_id": visible[-1]["user_id"] if visible else chat_id,
                "session": self._session_from_row(session) if session else None,
            },
            "messages": messages,
            "has_more": has_more,
            "next_before_id": int(visible[0]["id"]) if has_more and visible else None,
        }

    def clear_session(self, platform: str, chat_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM conversation_messages WHERE platform=? AND chat_id=?", (platform, chat_id))
            conn.execute("DELETE FROM conversation_sessions WHERE platform=? AND chat_id=?", (platform, chat_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def claim_message(
        self,
        dedupe_key: str,
        platform: str,
        chat_id: str,
        ttl_sec: int,
        now_ts: Optional[float] = None,
    ) -> bool:
        """原子登记消息；同一 key 在 TTL 内只允许一个线程或进程继续处理。"""
        now = float(now_ts if now_ts is not None else time.time())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT created_at FROM processed_messages WHERE dedupe_key=?",
                (dedupe_key,),
            ).fetchone()
            if row is not None and now - float(row["created_at"]) < ttl_sec:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "INSERT OR REPLACE INTO processed_messages (dedupe_key, platform, chat_id, created_at) VALUES (?, ?, ?, ?)",
                (dedupe_key, platform, chat_id, now),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def maybe_cleanup(self, now_ts: Optional[float] = None) -> None:
        now = float(now_ts if now_ts is not None else time.time())
        if now - self._last_cleanup_at < self.cleanup_interval_sec:
            return
        self._last_cleanup_at = now
        self.cleanup(now)

    def cleanup(self, now_ts: Optional[float] = None) -> Dict[str, int]:
        """定期清理防重记录、过期上下文与 30 天未活跃联系人。"""
        now = float(now_ts if now_ts is not None else time.time())
        expired_cutoff = now - self.context_ttl_sec
        inactive_cutoff = now - 30 * 86400
        conn = self._connect()
        expired_sessions = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            processed_deleted = conn.execute(
                "DELETE FROM processed_messages WHERE created_at<?",
                (now - 30 * 86400,),
            ).rowcount
            rows = conn.execute(
                "SELECT platform, chat_id, nickname, slots_json, ask_counts_json, booking_json, version FROM conversation_sessions WHERE last_active_at<?",
                (expired_cutoff,),
            ).fetchall()
            for row in rows:
                has_messages = conn.execute(
                    "SELECT 1 FROM conversation_messages WHERE platform=? AND chat_id=? LIMIT 1",
                    (row["platform"], row["chat_id"]),
                ).fetchone()
                has_state = bool(
                    has_messages
                    or row["slots_json"] not in ("", "{}")
                    or row["ask_counts_json"] not in ("", "{}")
                    or row["booking_json"]
                )
                if not has_state:
                    continue
                version = int(row["version"]) + 1
                conn.execute(
                    "DELETE FROM conversation_messages WHERE platform=? AND chat_id=?",
                    (row["platform"], row["chat_id"]),
                )
                conn.execute(
                    """UPDATE conversation_sessions SET auto_remark=?, slots_json='{}', ask_counts_json='{}',
                    booking_json='', version=?, updated_at=? WHERE platform=? AND chat_id=?""",
                    (
                        f"微信:{row['nickname'] or row['chat_id']} | 记忆v{version}",
                        version,
                        now,
                        row["platform"],
                        row["chat_id"],
                    ),
                )
                expired_sessions += 1
            contacts_deleted = conn.execute(
                "DELETE FROM conversation_sessions WHERE last_active_at<?",
                (inactive_cutoff,),
            ).rowcount
            conn.execute("COMMIT")
            return {
                "processed_deleted": processed_deleted,
                "expired_sessions": expired_sessions,
                "contacts_deleted": contacts_deleted,
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
