import subprocess
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
