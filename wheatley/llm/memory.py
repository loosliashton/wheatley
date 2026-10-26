"""Persistent Conversation Memory Engine for Wheatley (SQLite-backed)"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from wheatley.llm.base import LLMMessage, ToolCall

logger = logging.getLogger("wheatley.llm.memory")


class ConversationStore:
    """Manages chat history and session persistence in SQLite across service restarts."""

    def __init__(self, db_path: str | Path = "/var/lib/wheatley/wheatley.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_calls TEXT,
                    created_at REAL NOT NULL,
                    timestamp TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conv_chat_created
                ON conversations(chat_id, created_at);
                """
            )
            conn.commit()

    def add_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        tool_calls: Optional[List[ToolCall | Dict[str, Any]]] = None,
    ) -> None:
        """Persist a single chat turn (user, assistant, or tool) for a chat_id."""
        now_ts = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()

        tc_json = None
        if tool_calls:
            serialized_calls = []
            for tc in tool_calls:
                if isinstance(tc, ToolCall):
                    serialized_calls.append({"id": tc.id, "name": tc.name, "arguments": tc.arguments})
                elif isinstance(tc, dict):
                    serialized_calls.append(tc)
            tc_json = json.dumps(serialized_calls)

        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO conversations (chat_id, role, content, tool_calls, created_at, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (chat_id, role, content, tc_json, now_ts, now_iso),
                )
                conn.commit()
        except Exception as e:
            logger.warning("Failed to save conversation message: %s", e)

    def get_history(self, chat_id: str, max_turns: int = 10) -> List[LLMMessage]:
        """Fetch recent conversation turns for chat_id in chronological order.

        A turn represents a user interaction and assistant response.
        Ensures the returned message list begins cleanly with a 'user' message.
        """
        # Fetch up to 2 * max_turns messages (user + assistant pairs)
        limit_messages = max(max_turns * 2, 4)

        try:
            with self._get_conn() as conn:
                cursor = conn.execute(
                    """
                    SELECT role, content, tool_calls
                    FROM conversations
                    WHERE chat_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (chat_id, limit_messages),
                )
                rows = cursor.fetchall()
        except Exception as e:
            logger.warning("Failed to fetch conversation history: %s", e)
            return []

        if not rows:
            return []

        # Rows are in reverse order (newest first). Reverse to chronological order.
        rows.reverse()

        messages: List[LLMMessage] = []
        for r in rows:
            role = r["role"]
            content = r["content"]
            tcs = None
            if r["tool_calls"]:
                try:
                    raw_tcs = json.loads(r["tool_calls"])
                    tcs = [
                        ToolCall(
                            id=item.get("id", ""),
                            name=item.get("name", ""),
                            arguments=item.get("arguments", {}),
                        )
                        for item in raw_tcs
                    ]
                except Exception:
                    tcs = None

            messages.append(LLMMessage(role=role, content=content, tool_calls=tcs))

        # Ensure history begins on a user turn for strict API compatibility
        start_idx = 0
        for i, m in enumerate(messages):
            if m.role == "user":
                start_idx = i
                break

        return messages[start_idx:]

    def clear_history(self, chat_id: str) -> int:
        """Clear all conversation history for a specific chat_id (e.g. /reset)."""
        try:
            with self._get_conn() as conn:
                cursor = conn.execute(
                    "DELETE FROM conversations WHERE chat_id = ?",
                    (chat_id,),
                )
                conn.commit()
                deleted = cursor.rowcount
                logger.info("Cleared conversation history for %s (%d rows deleted)", chat_id, deleted)
                return deleted
        except Exception as e:
            logger.warning("Failed to clear conversation history for %s: %s", chat_id, e)
            return 0

    def cleanup_old_conversations(self, retention_days: int = 30) -> int:
        """Prune messages older than retention_days."""
        cutoff = time.time() - (retention_days * 86400)
        try:
            with self._get_conn() as conn:
                cursor = conn.execute(
                    "DELETE FROM conversations WHERE created_at < ?",
                    (cutoff,),
                )
                conn.commit()
                deleted = cursor.rowcount
                if deleted > 0:
                    logger.info("Pruned %d expired conversation turns older than %d days", deleted, retention_days)
                return deleted
        except Exception as e:
            logger.warning("Failed to prune old conversations: %s", e)
            return 0
