"""Presentation events, separate from scheduler activity and persistent traces."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class UIEvent:
    kind: str
    owner: str = "lead"
    call_id: str = ""
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
