from typing import Any


class BindingResolver:
    def __init__(self, bindings: dict[str, Any]):
        self.bindings = bindings or {}

    def resolve(self, channel: str, app_id: str, chat_id: str) -> dict[str, Any] | None:
        channels = self.bindings.get("channels", {})
        ch = channels.get(channel, {})

        app_scopes = ch.get("apps", {})
        app_cfg = app_scopes.get(app_id) or ch.get("default_app") or {}

        chats = app_cfg.get("chats", {}) or {}
        if chat_id in chats:
            return chats[chat_id]

        # Backward/forward compatible defaults:
        # 1) apps.<app_id>.default
        # 2) apps.<app_id>.chats.default
        return app_cfg.get("default") or chats.get("default")
