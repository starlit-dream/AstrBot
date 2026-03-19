from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

TagType = Literal["active", "pending"]


@dataclass
class ShortTermEntry:
    """单轮对话的短期记忆条目。"""

    tag: TagType
    user: str
    assistant: str
    timestamp: str


@dataclass
class ShortTermWindow:
    """某个会话的短期记忆窗口。"""

    session_id: str
    entries: list[ShortTermEntry] = field(default_factory=list)

    @property
    def pending_count(self) -> int:
        return sum(1 for item in self.entries if item.tag == "pending")
