#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SEEN_EVENT_FILES = set()
SEEN_LOCK = threading.Lock()

SESSION_RE = re.compile(r"^\[SESSION_ID\](.*?)\[/SESSION_ID\]\s*", re.DOTALL)
JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
AGENTS = ["product", "law", "code", "quant", "content", "market"]
ROUTER_INSTRUCTION = """你是 main，总入口 CEO agent。
你的任务不是直接长篇回答，而是先判断这条用户请求应该交给哪些内部 agent。
可选 agent 只有：product, law, code, quant, content, market。
输出必须是严格 JSON，且只能输出 JSON，不要解释，不要 markdown 代码块。
格式：{"agents":["product"],"reason":"一句中文","user_intent":"一句中文"}
规则：
- 产品定位/需求拆解/商业方案 => product
- 合同/合规/法律风险 => law
- 编码/架构/API/部署/脚本/调试 => code
- 量化/因子/回测/交易/金融研究 => quant
- 文案/文章/脚本/内容表达 => content
- 营销/增长/渠道/获客/传播 => market
- 至少 1 个，最多 3 个；不确定默认 product
"""
SYNTH_INSTRUCTION = """你是 main，总入口 CEO agent。
请把内部专业结果整合成面向最终用户的一版统一答复：
- 不暴露内部调度过程
- 直接给最终可执行结果
- 多维度时按小标题整合
- 中文、专业、直接
"""


def run(cmd, env=None, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=timeout)
    out = ((p.stdout or '') + ('\n' + p.stderr if p.stderr else '')).strip()
    return p.returncode, out


def mark_seen(name):
    with SEEN_LOCK:
        SEEN_EVENT_FILES.add(name)


def is_seen(name):
    with SEEN_LOCK:
        return name in SEEN_EVENT_FILES


def read_one(team, agent, env):
    base = Path(env.get('CLAWTEAM_DATA_DIR', str(Path.home() / 'ai-lab' / 'clawteam-data'))) / 'teams' / team / 'events'
    if not base.exists():
        return None
    files = sorted(base.glob('evt-*.json'), key=lambda p: p.name)
    for f in files:
        if is_seen(f.name):
            continue
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except Exception:
            mark_seen(f.name)
            continue
        if str(data.get('type') or '') != 'message':
            mark_seen(f.name)
            continue
        if str(data.get('to') or '') != agent:
            mark_seen(f.name)
            continue
        content = str(data.get('content') or '').strip()
        mark_seen(f.name)
        if content:
            event_id = str(data.get('id') or f.stem)
            return {
                'event_file': f.name,
                'event_id': event_id,
                'content': content,
            }
    return None


def split_session_payload(text):
    raw = (text or '').strip()
    m = SESSION_RE.match(raw)
    if not m:
        return 'cli:direct', raw
    session_id = (m.group(1) or '').strip() or 'cli:direct'
    body = SESSION_RE.sub('', raw, count=1).strip()
    return session_id, body


def ask_nanobot(nanobot_bin, workspace, session_id, text, timeout=900):
    rc, out = run([
        nanobot_bin, 'agent', '--workspace', workspace, '--session', session_id, '--message', text, '--no-logs'
    ], timeout=timeout)
    cleaned = []
    for line in out.splitlines():
        if line.strip().startswith('You:'):
            continue
        cleaned.append(line)
    return rc, ('\n'.join(cleaned).strip() or out.strip())


def extract_json(text):
    raw = (text or '').strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = JSON_RE.search(raw)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def expand_agents_by_keywords(user_text, agents):
    text = (user_text or '').lower()
    result = list(agents or [])
    mapping = [
        ('product', ['产品', '定位', 'mvp', '需求', '路线图', '定价']),
        ('law', ['法律', '法务', '合规', '监管', '隐私', '数据安全', '风险']),
        ('code', ['技术', '架构', '开发', '代码', 'api', '部署', '实现']),
        ('quant', ['数据指标', '指标', 'roi', '转化', '留存', '量化', '测算']),
        ('content', ['文案', '内容', '话术', '品牌表达', '落地页']),
        ('market', ['获客', '增长', '渠道', '营销', 'gtm', '市场', '客户']),
    ]
    for agent, keywords in mapping:
        if any(k.lower() in text for k in keywords) and agent not in result:
            result.append(agent)
    if not result:
        result = ['product']
    return result[:6]


def normalize_agents(value):
    if not isinstance(value, list):
        return ['product']
    result = []
    for item in value:
        s = str(item).strip().lower()
        if s in AGENTS and s not in result:
            result.append(s)
    return (result or ['product'])[:3]


def build_agent_prompt(agent, user_text, intent, reason):
    return f"""你是 {agent} 专业 agent。
请只从 {agent} 职责出发回答，不要扩展到别的领域。

用户原始需求：
{user_text}

main 对任务的理解：
- 用户真实目标：{intent}
- 分派原因：{reason}

请直接输出你这个专业视角下最有用的结果，中文，务实，可执行。"""


def build_synth_prompt(user_text, intent, reason, outputs):
    parts = [SYNTH_INSTRUCTION, '', f'用户原始需求：\n{user_text}', '', f'main 对需求的总结：{intent}', f'分派原因：{reason}', '', '以下是内部专业结果：']
    for agent, content in outputs.items():
        parts.append(f'## {agent}\n{content}')
    return '\n'.join(parts)


def process_message(nanobot_bin, root_workspace, session_id, body):
    main_ws = str(Path(root_workspace) / 'main')
    rc, route_raw = ask_nanobot(nanobot_bin, main_ws, session_id + ':main-router', ROUTER_INSTRUCTION + '\n\n用户消息：\n' + body, timeout=300)
    route = extract_json(route_raw)
    agents = normalize_agents(route.get('agents'))
    agents = expand_agents_by_keywords(body, agents)
    reason = str(route.get('reason') or '按需求相关性分派').strip()
    intent = str(route.get('user_intent') or body[:120]).strip()
    print(f'[main-worker] route agents={agents} reason={reason} intent={intent}', flush=True)
    outputs = {}
    for agent in agents:
        ws = str(Path(root_workspace) / agent)
        prompt = build_agent_prompt(agent, body, intent, reason)
        print(f'[main-worker] calling agent={agent} ws={ws}', flush=True)
        arc, aout = ask_nanobot(nanobot_bin, ws, session_id + ':' + agent, prompt, timeout=900)
        print(f'[main-worker] agent_done agent={agent} rc={arc} out_len={len(aout)}', flush=True)
        outputs[agent] = aout[:3000] if arc == 0 else f'[{agent} 执行失败 rc={arc}]\n{aout[:1200]}'
    print(f'[main-worker] synth_start agents={list(outputs.keys())}', flush=True)
    src, sout = ask_nanobot(nanobot_bin, main_ws, session_id + ':main-synth', build_synth_prompt(body, intent, reason, outputs), timeout=900)
    print(f'[main-worker] synth_done rc={src} out_len={len(sout)}', flush=True)
    if src == 0 and sout.strip():
        return sout[:5000]
    blocks = [f'需求理解：{intent}']
    for agent, content in outputs.items():
        blocks.append(f'【{agent}】\n{content}')
    return '\n\n'.join(blocks)[:5000]


def send_back(team, leader, text, env):
    run(['clawteam', 'inbox', 'send', team, leader, text], env=env, timeout=120)


def handle_message(message, args, env):
    msg = message['content']
    event_file = message.get('event_file', '')
    event_id = message.get('event_id', '')
    print(f"[main-worker] received event_file={event_file} event_id={event_id} msg={msg[:200]!r}", flush=True)
    session_id, body = split_session_payload(msg)
    print(f"[main-worker] session_id={session_id} body={body[:200]!r}", flush=True)
    answer = process_message(args.nanobot_bin, args.workspace_root, session_id, body)
    print(f'[main-worker] answer_ready event_id={event_id} len={len(answer)}', flush=True)
    send_back(args.team, args.leader, f'{args.prefix}: {answer}', env)
    print(f'[main-worker] sent back to leader event_id={event_id}', flush=True)


def reap_futures(futures):
    alive = []
    for future in futures:
        if future.done():
            future.result()
        else:
            alive.append(future)
    return alive


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--team', required=True)
    ap.add_argument('--agent', required=True)
    ap.add_argument('--leader', default='leader')
    ap.add_argument('--prefix', default='MAIN_REPLY')
    ap.add_argument('--sleep', type=float, default=1.5)
    ap.add_argument('--max-workers', type=int, default=4)
    ap.add_argument('--nanobot-bin', default=str(Path.home() / 'ai-lab' / 'venvs' / 'nanobot' / 'bin' / 'nanobot'))
    ap.add_argument('--workspace-root', default=str(Path.home() / '.nanobot' / 'super-team'))
    args = ap.parse_args()
    env = dict(os.environ)
    env.setdefault('CLAWTEAM_DATA_DIR', str(Path.home() / 'ai-lab' / 'clawteam-data'))
    base = Path(env.get('CLAWTEAM_DATA_DIR', str(Path.home() / 'ai-lab' / 'clawteam-data'))) / 'teams' / args.team / 'events'
    if base.exists():
        with SEEN_LOCK:
            for f in base.glob('evt-*.json'):
                SEEN_EVENT_FILES.add(f.name)
    print(f'[main-worker] started team={args.team} agent={args.agent} workspace_root={args.workspace_root} max_workers={args.max_workers} seen={len(SEEN_EVENT_FILES)}', flush=True)
    futures = []
    with ThreadPoolExecutor(max_workers=max(1, args.max_workers), thread_name_prefix='main-worker') as executor:
        while True:
            try:
                futures = reap_futures(futures)
                message = read_one(args.team, args.agent, env)
                if not message:
                    time.sleep(args.sleep)
                    continue
                futures.append(executor.submit(handle_message, message, args, env.copy()))
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f'[main-worker] loop_error: {e!r}', flush=True)
                send_back(args.team, args.leader, f'{args.prefix}: 调度失败\n{e}', env)
                time.sleep(max(1.0, args.sleep))


if __name__ == '__main__':
    main()
