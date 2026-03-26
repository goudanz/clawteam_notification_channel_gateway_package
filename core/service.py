from core.executor import build_clawteam_cmd, run_cmd
from core.log import log
from core.models import DispatchResult, InboundEvent
from core.resolver import BindingResolver


class GatewayService:
    def __init__(self, cfg):
        self.cfg = cfg
        self.resolver = BindingResolver(cfg.bindings)

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

        rc, out = run_cmd(cmd, timeout_sec=int(route.get("timeout_sec", 90)))
        summary = out[:1200] if out else "(no output)"
        if rc == 0:
            return DispatchResult(ok=True, output=summary, route=route)
        return DispatchResult(ok=False, output=f"rc={rc}\n{summary}", route=route)

    def run_forever(self):
        log("clawteam-notification-channel-gateway running")
        while True:
            import time

            time.sleep(1)
