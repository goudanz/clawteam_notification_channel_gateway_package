import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any


def build_clawteam_cmd(text: str, route: dict[str, Any]) -> list[str]:
    team = route.get("team")
    agent = route.get("agent")
    mode = (route.get("mode") or "task").lower()

    txt = (text or "").strip()
    if not txt:
        txt = "请处理该渠道任务（原消息为空文本）"

    if txt.startswith("/inbox "):
        content = txt[len("/inbox "):].strip()
        return ["clawteam", "inbox", "send", team, agent, content]

    if txt.startswith("/task "):
        content = txt[len("/task "):].strip()
        title = content[:30] if len(content) > 30 else content
        return ["clawteam", "task", "create", team, title, "-o", agent, "-d", content]

    if txt.startswith("/spawn "):
        content = txt[len("/spawn "):].strip()
        return ["clawteam", "spawn", "--team", team, "--agent-name", agent, "--task", content]

    if mode == "inbox":
        return ["clawteam", "inbox", "send", team, agent, txt]

    title = txt[:30] if len(txt) > 30 else txt
    return ["clawteam", "task", "create", team, title, "-o", agent, "-d", txt]


def run_cmd(cmd: list[str], timeout_sec: int = 90) -> tuple[int, str]:
    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
    )
    out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
    return p.returncode, out.strip()


def _clawteam_data_dir() -> Path:
    data_dir = os.environ.get("CLAWTEAM_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir)
    return Path.home() / ".clawteam"


def _parse_msg_epoch_ms(filename: str) -> int:
    try:
        parts = filename.split("-")
        if len(parts) >= 3 and parts[0] == "msg":
            return int(parts[1])
    except Exception:
        pass
    return 0


def wait_for_agent_reply(
    team: str,
    from_agent: str,
    *,
    leader_agent: str = "leader",
    after_ms: int,
    timeout_sec: int = 45,
    poll_interval_sec: float = 1.0,
) -> str | None:
    inbox_dir = _clawteam_data_dir() / "teams" / team / "inboxes" / leader_agent
    deadline = time.time() + max(1, timeout_sec)

    while time.time() < deadline:
        if inbox_dir.exists():
            files = sorted(inbox_dir.glob("msg-*.json"), key=lambda p: p.name)
            for f in files:
                if _parse_msg_epoch_ms(f.name) < after_ms:
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue

                sender = str(data.get("from") or "")
                receiver = str(data.get("to") or "")
                content = str(data.get("content") or "").strip()
                # Worker may run outside clawteam spawn context and send with from="agent".
                # Accept both explicit target agent name and generic "agent".
                if sender in {from_agent, "agent"} and receiver == leader_agent and content:
                    return content

        time.sleep(max(0.2, poll_interval_sec))

    return None
