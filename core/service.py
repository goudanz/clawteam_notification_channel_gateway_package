import re
import threading
import time
from collections.abc import Callable

from core.executor import build_clawteam_cmd, run_cmd, wait_for_agent_reply
from core.log import log
from core.models import DispatchResult, InboundEvent
from core.resolver import BindingResolver


class GatewayService:
    BACKGROUND_HINT = "这个问题稍复杂，我还在处理中，请稍等"

    def __init__(self, cfg):
        self.cfg = cfg
        self.resolver = BindingResolver(cfg.bindings)

    @staticmethod
    def _estimate_wait_strategy(text: str) -> tuple[str, int]:
        raw = (text or "").strip()
        t = raw.lower()
        hard_complex_keywords = [
            "分别", "各自", "同时从", "多个角度", "多角度", "详细", "完整", "系统", "深入", "展开", "方案",
            "产品", "法律", "合规", "技术", "实现", "架构", "获客", "增长", "内容", "指标", "数据指标", "商业化",
            "roadmap", "gtm", "mvp", "api", "architecture",
        ]
        simple_keywords = ["只回复", "不超过", "100字", "简要", "一句话", "简短", "简单说", "结论即可"]

        hit_complex = sum(1 for k in hard_complex_keywords if k in raw or k in t)
        hit_simple = any(k in raw or k in t for k in simple_keywords)

        if hit_complex >= 3 and len(raw) >= 40 and not hit_simple:
            return "background", 0
        if hit_complex >= 1 and not hit_simple:
            return "multi", 100
        return "simple", 60

    def _wait_reply_in_background(
        self,
        *,
        team: str,
        agent: str,
        send_started_ms: int,
        session_id: str | None,
        wait_seconds: int,
        on_deferred_reply: Callable[[str], None] | None,
        chat_id: str,
    ) -> None:
        if not on_deferred_reply or wait_seconds <= 0:
            return

        def runner():
            try:
                deferred = wait_for_agent_reply(
                    team=team,
                    from_agent=agent,
                    after_ms=send_started_ms,
                    timeout_sec=wait_seconds,
                    session_id=session_id,
                )
                if not deferred:
                    log(
                        f"reply-background-timeout team={team} agent={agent} chat={chat_id} "
                        f"timeout={wait_seconds}s"
                    )
                    return
                clean = self._clean_agent_reply(deferred)
                on_deferred_reply(clean[:2000])
                log(f"reply-background-ok team={team} agent={agent} chat={chat_id}")
            except Exception as e:
                log(f"reply-background-error team={team} agent={agent} chat={chat_id}: {e}")

        threading.Thread(target=runner, daemon=True, name=f"deferred-reply-{team}-{agent}").start()

    def handle_event(self, event: InboundEvent, on_deferred_reply: Callable[[str], None] | None = None) -> DispatchResult:
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

        payload_text = event.text
        if event.session_id:
            payload_text = f"[SESSION_ID]{event.session_id}[/SESSION_ID]\n{event.text}"

        cmd = build_clawteam_cmd(payload_text, route)
        log(
            f"dispatch channel={event.channel} app={event.app_id} chat={event.chat_id} "
            f"session={event.session_id} cmd={' '.join(cmd)}"
        )

        send_started_ms = int(time.time() * 1000)
        rc, out = run_cmd(cmd, timeout_sec=int(route.get("timeout_sec", 90)))
        summary = out[:1200] if out else "(no output)"
        if rc != 0:
            return DispatchResult(ok=False, output=f"rc={rc}\n{summary}", route=route)

        is_inbox_send = len(cmd) >= 4 and cmd[0:3] == ["clawteam", "inbox", "send"]
        if is_inbox_send:
            team = str(route.get("team") or "")
            agent = str(route.get("agent") or "")
            strategy, dynamic_timeout = self._estimate_wait_strategy(event.text)
            reply_timeout = dynamic_timeout
            configured_timeout = int(route.get("reply_timeout_sec", 60))
            if strategy == "multi":
                reply_timeout = min(max(dynamic_timeout, 90), configured_timeout)
            elif strategy == "simple":
                reply_timeout = min(dynamic_timeout, configured_timeout)
            else:
                reply_timeout = 0

            deferred_timeout = int(route.get("deferred_reply_timeout_sec", max(configured_timeout, 600)))
            log(
                f"reply-strategy team={team} agent={agent} chat={event.chat_id} "
                f"strategy={strategy} front_timeout={reply_timeout}s deferred_timeout={deferred_timeout}s"
            )

            if strategy == "background":
                self._wait_reply_in_background(
                    team=team,
                    agent=agent,
                    send_started_ms=send_started_ms,
                    session_id=event.session_id,
                    wait_seconds=deferred_timeout,
                    on_deferred_reply=on_deferred_reply,
                    chat_id=event.chat_id,
                )
                return DispatchResult(ok=True, output=self.BACKGROUND_HINT, route=route)

            reply = wait_for_agent_reply(
                team=team,
                from_agent=agent,
                after_ms=send_started_ms,
                timeout_sec=reply_timeout,
                session_id=event.session_id,
            )
            if reply:
                log(f"reply-ok team={team} agent={agent} chat={event.chat_id}")
                clean = self._clean_agent_reply(reply)
                return DispatchResult(ok=True, output=clean[:2000], route=route)

            if on_deferred_reply and deferred_timeout > reply_timeout:
                log(
                    f"reply-deferred team={team} agent={agent} chat={event.chat_id} "
                    f"reply_timeout={reply_timeout}s deferred_timeout={deferred_timeout}s"
                )
                self._wait_reply_in_background(
                    team=team,
                    agent=agent,
                    send_started_ms=send_started_ms,
                    session_id=event.session_id,
                    wait_seconds=max(1, deferred_timeout - reply_timeout),
                    on_deferred_reply=on_deferred_reply,
                    chat_id=event.chat_id,
                )
                return DispatchResult(ok=True, output=self.BACKGROUND_HINT, route=route)

            log(f"reply-timeout team={team} agent={agent} chat={event.chat_id} timeout={reply_timeout}s")
            return DispatchResult(ok=False, output=f"agent未在{reply_timeout}s内返回结果，请稍后重试。", route=route)

        return DispatchResult(ok=True, output=summary, route=route)

    @staticmethod
    def _clean_agent_reply(text: str) -> str:
        t = (text or "").strip()
        t = re.sub(r"^\[SESSION_ID\].*?\[/SESSION_ID\]\s*", "", t, flags=re.IGNORECASE | re.DOTALL)
        # strip worker-added prefixes like "DEV_REPLY:" / "MARKET_REPLY:" / "REPLY:"
        t = re.sub(r"^[A-Z_]+_?REPLY\s*:\s*", "", t, flags=re.IGNORECASE)
        return t.strip()


    def run_forever(self):
        log("clawteam-notification-channel-gateway running")
        while True:
            time.sleep(1)
