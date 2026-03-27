#!/usr/bin/env python3
import argparse
import os
import re
import subprocess
import time
from pathlib import Path

SESSION_RE = re.compile(r"^\[SESSION_ID\](.*?)\[/SESSION_ID\]\s*", re.DOTALL)

MSG_RE = re.compile(r"^\[.*?\]\s+message\s+from=.*?\s:\s(.*)$")


def run(cmd, env=None, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=timeout)
    out = ((p.stdout or '') + ('\n' + p.stderr if p.stderr else '')).strip()
    return p.returncode, out


def read_one(team: str, agent: str, env: dict) -> str | None:
    rc, out = run(['clawteam', 'inbox', 'receive', team, '--agent', agent, '--limit', '1'], env=env, timeout=120)
    if rc != 0:
        return None
    if 'No messages' in out:
        return None

    for line in out.splitlines():
        m = MSG_RE.match(line.strip())
        if m:
            return m.group(1).strip()
    return None


def split_session_payload(text: str) -> tuple[str, str]:
    raw = (text or '').strip()
    m = SESSION_RE.match(raw)
    if not m:
        return 'cli:direct', raw
    session_id = (m.group(1) or '').strip() or 'cli:direct'
    body = SESSION_RE.sub('', raw, count=1).strip()
    return session_id, body


def ask_nanobot(nanobot_bin: str, workspace: str, text: str) -> str:
    session_id, body = split_session_payload(text)
    rc, out = run([
        nanobot_bin,
        'agent',
        '--workspace', workspace,
        '--session', session_id,
        '--message', body,
        '--no-logs',
    ], timeout=900)
    if rc == 0 and out.strip():
        cleaned = []
        for line in out.splitlines():
            if line.strip().startswith('You:'):
                continue
            cleaned.append(line)
        ans = '\n'.join(cleaned).strip() or out.strip()
        return ans[:3000]
    return f"处理失败(rc={rc})\n{out[:1200]}"


def send_back(team: str, leader: str, text: str, env: dict):
    run(['clawteam', 'inbox', 'send', team, leader, text], env=env, timeout=120)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--team', required=True)
    ap.add_argument('--agent', required=True)
    ap.add_argument('--leader', default='leader')
    ap.add_argument('--prefix', default='REPLY')
    ap.add_argument('--sleep', type=float, default=1.5)
    ap.add_argument('--nanobot-bin', default=str(Path.home() / 'ai-lab' / 'venvs' / 'nanobot' / 'bin' / 'nanobot'))
    ap.add_argument('--workspace', default=str(Path.home() / 'ai-lab' / 'nanobot-workspace'))
    args = ap.parse_args()

    env = dict(os.environ)
    env.setdefault('CLAWTEAM_DATA_DIR', str(Path.home() / 'ai-lab' / 'clawteam-data'))

    while True:
        try:
            msg = read_one(args.team, args.agent, env)
            if not msg:
                time.sleep(args.sleep)
                continue

            if msg.startswith(f"{args.prefix}:"):
                time.sleep(0.3)
                continue

            reply = ask_nanobot(args.nanobot_bin, args.workspace, msg)
            send_back(args.team, args.leader, f"{args.prefix}: {reply}", env)
        except Exception as e:
            send_back(args.team, args.leader, f"{args.prefix}: worker异常 {e}", env)
            time.sleep(1.5)


if __name__ == '__main__':
    main()
