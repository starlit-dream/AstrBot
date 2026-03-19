"""短期记忆窗口：active/pending 标记、30 轮计数、滑动淘汰、持久化落盘。"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timezone

from astrbot.core import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .models import ShortTermEntry, ShortTermWindow

_ACTIVE_MAX = 30
_DIR = os.path.join(get_astrbot_data_path(), "short_term_memory")

# 基于 session_id 的并发锁，确保同一会话的短期窗口读写是串行的。
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def _get_session_lock(session_id: str) -> asyncio.Lock:
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _SESSION_LOCKS[session_id] = lock
        return lock


def _path(session_id: str) -> str:
    # Windows 文件名不允许包含 ":" 等字符，这里做一次保守替换
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    return os.path.join(_DIR, f"{safe}.json")


def _entry_to_dict(e: ShortTermEntry) -> dict:
    return {
        "tag": e.tag,
        "user": e.user,
        "assistant": e.assistant,
        "timestamp": e.timestamp,
    }


def _dict_to_entry(d: dict) -> ShortTermEntry:
    return ShortTermEntry(
        tag=d.get("tag", "active"),
        user=d.get("user", ""),
        assistant=d.get("assistant", ""),
        timestamp=d.get("timestamp", ""),
    )


async def load_window(session_id: str) -> ShortTermWindow:
    """从落盘文件加载短期窗口。"""
    lock = _get_session_lock(session_id)
    async with lock:
        p = _path(session_id)
        if not os.path.exists(p):
            return ShortTermWindow(session_id=session_id)
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            logger.warning("短期窗口加载失败 %s: %s", session_id, exc)
            return ShortTermWindow(session_id=session_id)
        entries = [_dict_to_entry(d) for d in data.get("entries", [])]
        return ShortTermWindow(session_id=session_id, entries=entries)


def _save_sync(window: ShortTermWindow) -> None:
    os.makedirs(_DIR, exist_ok=True)
    p = _path(window.session_id)
    logger.info(
        "短期窗口落盘：dir=%s, path=%s, session_id=%s, entries=%d",
        _DIR,
        p,
        window.session_id,
        len(window.entries),
    )
    with open(p, "w", encoding="utf-8") as f:
        json.dump(
            {
                "session_id": window.session_id,
                "entries": [_entry_to_dict(e) for e in window.entries],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


async def save_window(window: ShortTermWindow) -> None:
    """将短期窗口落盘。"""
    lock = _get_session_lock(window.session_id)
    async with lock:
        await asyncio.to_thread(_save_sync, window)


def entries_to_context_messages(entries: list[ShortTermEntry]) -> list[dict]:
    """将短期条目转换为 OpenAI 格式的 messages（用于 req.contexts）。"""
    out: list[dict] = []
    for e in entries:
        out.append({"role": "user", "content": e.user})
        out.append({"role": "assistant", "content": e.assistant})
    return out


def slide_and_collect_pending(
    window: ShortTermWindow,
    active_max: int = _ACTIVE_MAX,
) -> list[ShortTermEntry]:
    """
    当 active 超过 active_max 时，将最旧的 active 逐个标记为 pending，直到 active 数量符合配置。
    返回本轮需要提交 Mem0.add 的 pending 条目（成功后可清除）。
    """
    active_indices = [i for i, e in enumerate(window.entries) if e.tag == "active"]
    if len(active_indices) <= active_max:
        return []

    # 计算需要标记为 pending 的数量
    to_mark_count = len(active_indices) - active_max

    # 从最旧的开始标记
    for i in range(to_mark_count):
        idx = active_indices[i]
        e = window.entries[idx]
        window.entries[idx] = ShortTermEntry(
            tag="pending",
            user=e.user,
            assistant=e.assistant,
            timestamp=e.timestamp,
        )

    pending_list = [x for x in window.entries if x.tag == "pending"]
    return list(pending_list)


def append_turn(
    window: ShortTermWindow,
    user_content: str,
    assistant_content: str,
) -> None:
    """追加本轮对话为 active。"""
    now = datetime.now(timezone.utc).isoformat()
    window.entries.append(
        ShortTermEntry(
            tag="active",
            user=user_content,
            assistant=assistant_content,
            timestamp=now,
        )
    )


def remove_pending_after_add(
    window: ShortTermWindow, to_remove: list[ShortTermEntry]
) -> None:
    """Mem0.add 成功后，从窗口中移除这些 pending 条目。"""
    seen = {(e.user, e.assistant, e.timestamp) for e in to_remove}
    window.entries = [
        e for e in window.entries if (e.user, e.assistant, e.timestamp) not in seen
    ]
