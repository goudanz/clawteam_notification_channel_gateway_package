import re
import time

from core.executor import build_clawteam_cmd, run_cmd, wait_for_agent_reply
from core.log import log
from core.models import DispatchResult, InboundEvent
from core.resolver import BindingResolver


class GatewayService:
    def __init__(self, cfg):
        self.cfg = cfg
        self.resolver = BindingResolver(cfg.bindings)

    @staticmethod
    def _clean_agent_reply(text: str) -> str:
        t = (text or "").strip()
        # strip worker-added prefixes like "DEV_REPLY:" / "MARKET_REPLY:" / "REPLY:"
        t = re.sub(r"^[A-Z_]+_?REPLY\s*:\s*", "", t, flags=re.IGNORECASE)
        return t.strip()

    def handle_event(self, event: InboundEvent) -> DispatchResult:
        log(
            f"inbound channel={event.channel} app={event.app_id} chat={event.chat_id} "
            f"type={event.message_type} text={event.text[:80]!r}"
        )

        if event.message_type != "text":
            return DispatchResult(ok=True, output=f"ignored non-text: {event.message_type}", route={})

        route = self.resolver.resolve(event.channel, event.app_id, event.chat_id)
        if not route:
            log(
                f"no-route channel={event.channel} app={event.app_id} chat={event.chat_id}; "
                f"check configs/bindings.yaml"
            )
            return DispatchResult(ok=True, output="ignored no route", route={})

        cmd = build_clawteam_cmd(event.text, route)
        log(f"dispatch channel={event.channel} app={event.app_id} chat={event.chat_id} cmd={' '.join(cmd)}")

        send_started_ms = int(time.time() * 1000)
        rc, out = run_cmd(cmd, timeout_sec=int(route.get("timeout_sec", 90)))
        summary = out[:1200] if out else "(no output)"
        if rc != 0:
            return DispatchResult(ok=False, output=f"rc={rc}\n{summary}", route=route)

        is_inbox_send = len(cmd) >= 4 and cmd[0:3] == ["clawteam", "inbox", "send"]
        if is_inbox_send:
            team = str(route.get("team") or "")
            agent = str(route.get("agent") or "")
            reply_timeout = int(route.get("reply_timeout_sec", 60))
            reply = wait_for_agent_reply(
                team=team,
                from_agent=agent,
                after_ms=send_started_ms,
                timeout_sec=reply_timeout,
            )
            if reply:
                log(f"reply-ok team={team} agent={agent} chat={event.chat_id}")
                clean = self._clean_agent_reply(reply)
                return DispatchResult(ok=True, output=clean[:2000], route=route)

            log(f"reply-timeout team={team} agent={agent} chat={event.chat_id} timeout={reply_timeout}s")
            return DispatchResult(ok=False, output=f"agent未在{reply_timeout}s内返回结果，请稍后重试。", route=route)

        return DispatchResult(ok=True, output=summary, route=route)

    def run_forever(self):
        log("clawteam-notification-channel-gateway running")
        while True:
            time.sleep(1)
