from dataclasses import dataclass, field
from typing import Any


@dataclass
class InboundEvent:
    channel: str
    app_id: str
    chat_id: str
    sender_id: str
    message_id: str
    message_type: str
    text: str
    raw: Any = None


@dataclass
class DispatchResult:
    ok: bool
    output: str
    route: dict[str, Any] = field(default_factory=dict)
