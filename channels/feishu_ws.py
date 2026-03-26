import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from channels.base import ChannelAdapter
from channels.feishu_client import FeishuClient
from core.log import log
from core.models import InboundEvent

try:
    import yaml  # type: ignore
except Exception:
    yaml = None


def _safe_get(obj: Any, path: list[str], default=None):
    cur = obj
    for k in path:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            cur = getattr(cur, k, None)
    return default if cur is None else cur


class FeishuWSAdapter(ChannelAdapter):
    """Feishu long-connection adapter.

    Multi-app strategy:
    - Single app: run in-process (legacy behavior)
    - Multi app: parent process auto-spawns one child process per app.
      Each child handles exactly one app via CBG_FEISHU_APP_INDEX,
      avoiding lark-oapi websocket event-loop conflicts in one process.
    """

    APP_INDEX_ENV = "CBG_FEISHU_APP_INDEX"

    def __init__(self, service, channel_cfg: dict, base_dir: Path):
        self.service = service
        self.channel_cfg = channel_cfg or {}
        self.base_dir = base_dir
        self._running = False
        self._threads: list[threading.Thread] = []
        self._children: dict[int, subprocess.Popen] = {}
        self._seen_lock = threading.Lock()
        self._seen_ids: dict[str, float] = {}
        self._seen_ttl_sec = 600

        apps_file = self.channel_cfg.get("apps_file", "./configs/feishu_apps.yaml")
        self.apps_path = (base_dir / apps_file).resolve() if not Path(apps_file).is_absolute() else Path(apps_file)

        all_apps = self._load_apps()
        self.apps = self._select_apps_for_current_process(all_apps)

    def _load_apps(self) -> list[dict]:
        if not self.apps_path.exists():
            raise RuntimeError(f"feishu apps config not found: {self.apps_path}")
        if yaml is None:
            raise RuntimeError("PyYAML not installed. Run: pip install pyyaml")
        data = yaml.safe_load(self.apps_path.read_text(encoding="utf-8")) or {}
        apps = data.get("apps") or []
        if not apps:
            raise RuntimeError("feishu apps config invalid: apps must be non-empty")
        return apps

    def _select_apps_for_current_process(self, all_apps: list[dict]) -> list[dict]:
        idx_text = os.environ.get(self.APP_INDEX_ENV, "").strip()
        if not idx_text:
            return all_apps

        try:
            idx = int(idx_text)
        except Exception as e:
            raise RuntimeError(f"invalid {self.APP_INDEX_ENV}={idx_text}: {e}")

        if idx < 0 or idx >= len(all_apps):
            raise RuntimeError(f"{self.APP_INDEX_ENV} out of range: {idx}, apps={len(all_apps)}")

        app = all_apps[idx]
        app_name = str(app.get("name") or app.get("app_id") or f"app-{idx}")
        log(f"[feishu] child mode active: index={idx}, app={app_name}")
        return [app]

    def _extract_event(self, data: Any, fallback_app_id: str) -> InboundEvent:
        app_id = _safe_get(data, ["header", "app_id"], "") or _safe_get(data, ["app_id"], "") or fallback_app_id
        event = _safe_get(data, ["event"], None) or data
        msg = _safe_get(event, ["message"], None) or _safe_get(event, ["event", "message"], None)
        sender = _safe_get(event, ["sender"], None) or _safe_get(event, ["event", "sender"], None)

        chat_id = str(_safe_get(msg, ["chat_id"], "") or "")
        msg_type = str(_safe_get(msg, ["message_type"], "") or "")
        msg_id = str(_safe_get(msg, ["message_id"], "") or "")
        sender_id = str(_safe_get(sender, ["sender_id", "open_id"], "") or "")
        content_raw = _safe_get(msg, ["content"], "{}") or "{}"

        text = ""
        try:
            if isinstance(content_raw, str):
                text = (json.loads(content_raw) or {}).get("text", "")
            elif isinstance(content_raw, dict):
                text = content_raw.get("text", "") or ""
        except Exception:
            text = ""

        return InboundEvent(
            channel="feishu",
            app_id=str(app_id),
            chat_id=chat_id,
            sender_id=sender_id,
            message_id=msg_id,
            message_type=msg_type,
            text=text,
            raw=data,
        )

    def _is_duplicate_message(self, app_id: str, message_id: str) -> bool:
        if not message_id:
            return False
        now = time.time()
        key = f"{app_id}:{message_id}"
        with self._seen_lock:
            expired = [k for k, ts in self._seen_ids.items() if now - ts > self._seen_ttl_sec]
            for k in expired:
                self._seen_ids.pop(k, None)
            if key in self._seen_ids:
                return True
            self._seen_ids[key] = now
        return False

    def _start_one_app(self, app: dict):
        import lark_oapi as lark
        import lark_oapi.ws.client as _lark_ws_client

        name = str(app.get("name") or app.get("app_id") or "feishu-app")
        app_id = str(app.get("app_id") or "").strip()
        app_secret = str(app.get("app_secret") or "").strip()
        verify_token = str(app.get("verify_token") or app.get("verification_token") or "").strip()
        encrypt_key = str(app.get("encrypt_key") or "").strip()

        if not app_id or not app_secret:
            raise RuntimeError(f"invalid feishu app config for {name}")

        client = FeishuClient(app_id, app_secret)

        bot_open_id = ""
        try:
            bot_info = client.get_bot_info()
            bot_open_id = str(((bot_info or {}).get("bot") or {}).get("open_id") or "")
            if bot_open_id:
                log(f"[feishu:{name}] bot_open_id={bot_open_id}")
        except Exception as e:
            log(f"[feishu:{name}] get bot info failed: {e}")

        def on_message(data: Any):
            try:
                evt = self._extract_event(data, fallback_app_id=app_id)

                if self._is_duplicate_message(evt.app_id, evt.message_id):
                    log(f"[feishu:{name}] duplicate ignored message_id={evt.message_id}")
                    return

                # Ignore messages sent by this bot itself (prevents echo loops).
                if bot_open_id and evt.sender_id and evt.sender_id == bot_open_id:
                    log(f"[feishu:{name}] self message ignored message_id={evt.message_id}")
                    return

                # Strict mention gate: process only when this specific bot is explicitly @mentioned.
                # This avoids any cross-bot/self-trigger noise in group environments.
                mentions = _safe_get(data, ["event", "message", "mentions"], None) or []
                mention_ids = {
                    str(_safe_get(m, ["id", "open_id"], "") or "")
                    for m in mentions
                }
                if (not bot_open_id) or (bot_open_id not in mention_ids):
                    log(
                        f"[feishu:{name}] not-mentioned ignored "
                        f"message_id={evt.message_id} chat={evt.chat_id}"
                    )
                    return

                if evt.message_id:
                    try:
                        client.add_reaction(evt.message_id, emoji_type="OneSecond")
                        log(f"[feishu:{name}] ack reaction ok message_id={evt.message_id} emoji=OneSecond")
                    except Exception as e:
                        log(f"[feishu:{name}] add reaction failed emoji=OneSecond: {e}")

                result = self.service.handle_event(evt)
                if result.route:
                    # Only forward agent reply text to end user.
                    client.send_text_to_chat(evt.chat_id, result.output)
            except Exception as e:
                log(f"[feishu:{name}] on_message error: {e}")

        builder = lark.EventDispatcherHandler.builder(encrypt_key, verify_token).register_p2_im_message_receive_v1(on_message)
        handler = builder.build()
        ws_client = lark.ws.Client(app_id, app_secret, event_handler=handler, log_level=lark.LogLevel.INFO)

        def run_ws():
            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            _lark_ws_client.loop = ws_loop
            try:
                while self._running:
                    try:
                        ws_client.start()
                    except Exception as e:
                        log(f"[feishu:{name}] websocket error: {e}")
                    if self._running:
                        time.sleep(5)
            finally:
                ws_loop.close()

        t = threading.Thread(target=run_ws, daemon=True, name=f"cbg-feishu-{name}")
        t.start()
        self._threads.append(t)
        log(f"[feishu] started app={name} ({app_id})")

    def _spawn_child_for_app(self, index: int, app: dict) -> subprocess.Popen:
        app_name = str(app.get("name") or app.get("app_id") or f"app-{index}")
        env = os.environ.copy()
        env[self.APP_INDEX_ENV] = str(index)

        cmd = [sys.executable, "main.py"]
        proc = subprocess.Popen(cmd, cwd=str(self.base_dir), env=env)
        log(f"[feishu] worker spawned: app={app_name} index={index} pid={proc.pid}")
        return proc

    def _start_multi_app_supervisor(self, all_apps: list[dict]) -> None:
        for idx, app in enumerate(all_apps):
            self._children[idx] = self._spawn_child_for_app(idx, app)

        def supervise_loop():
            while self._running:
                for idx, app in enumerate(all_apps):
                    proc = self._children.get(idx)
                    if proc is None:
                        continue
                    rc = proc.poll()
                    if rc is not None and self._running:
                        app_name = str(app.get("name") or app.get("app_id") or f"app-{idx}")
                        log(f"[feishu] worker exited: app={app_name} index={idx} rc={rc}, restarting...")
                        time.sleep(1)
                        self._children[idx] = self._spawn_child_for_app(idx, app)
                time.sleep(3)

        t = threading.Thread(target=supervise_loop, daemon=True, name="cbg-feishu-supervisor")
        t.start()
        self._threads.append(t)
        log(f"[feishu] multi-app supervisor started (workers={len(all_apps)})")

    def start(self) -> None:
        try:
            import lark_oapi  # noqa: F401
        except Exception:
            raise RuntimeError("Missing dependency lark-oapi. Run: pip install lark-oapi")

        self._running = True

        # Parent mode + multi app => process-supervised workers (one app per process).
        # Child mode (CBG_FEISHU_APP_INDEX set) => in-process single app.
        is_child = bool(os.environ.get(self.APP_INDEX_ENV, "").strip())
        if not is_child and len(self.apps) > 1:
            self._start_multi_app_supervisor(self.apps)
            return

        for app in self.apps:
            self._start_one_app(app)
