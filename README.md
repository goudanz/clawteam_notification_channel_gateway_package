# ClawTeam Notification Channel Gateway

ClawTeam 通知渠道网关。

当前已落地的是 **Feishu 长连接接入 + ClawTeam inbox 路由 + nanobot worker 回包** 的完整链路，目标是把外部 IM 渠道消息稳定转成 ClawTeam / nanobot 可消费的任务，并把 agent 结果原路回到用户所在会话。

当前仓库已经不是“只收消息、简单转发”的最小 demo，而是已经包含了以下生产向能力：

- Feishu WebSocket 长连接接入，不依赖公网回调 URL
- 单仓库支持多 Feishu bot / 多 app
- 同一进程自动托管多 bot 子进程，规避 `lark-oapi` 多 app 同进程冲突
- 按 `channel + app_id + chat_id` 路由到指定 `team / agent`
- 通过 ClawTeam inbox 模式等待 **真实 agent 回包**，不是只返回“已转发”
- worker 进程把 inbox 消息喂给 nanobot，再把结果写回 leader inbox
- 飞书回复仅回传 agent 正文，自动清洗 `REPLY:` / `DEV_REPLY:` / `MARKET_REPLY:` 等包装前缀
- 群聊只处理 **明确 @ 当前 bot** 的消息，避免串触发
- 私聊（p2p）无需 `@`，可直接回复
- 仅处理 `sender_type=user`，忽略 bot / app / system 消息
- 忽略 bot 自己发出的消息，避免自激活回环
- 按 `message_id` 去重，降低重复投递 / SDK 重放引发的重复回复
- 收到消息后，自动给原消息添加 Feishu reaction（当前固定 `OneSecond`）
- 已处理 Feishu `reaction.created` 事件，避免 SDK 持续报 `processor not found`
- 通过 `session_id = feishu:{app_id}:{chat_id}` 把不同群聊 / 私聊的上下文彻底隔离，避免 nanobot 串上下文

---

## 1. 当前架构

```text
Feishu message
  -> channels/feishu_ws.py
  -> core/service.py
  -> clawteam inbox send
  -> scripts/gateway_inbox_worker.py
  -> nanobot agent --session <session_id>
  -> clawteam leader inbox
  -> core/service.py wait_for_agent_reply()
  -> Feishu send_text_to_chat(original chat_id)
```

### 核心设计点

1. **渠道适配层**只负责把外部消息转成统一 `InboundEvent`
2. **GatewayService** 负责路由、投递、等待回包、清洗输出
3. **worker** 负责真正调用 nanobot
4. **渠道发送**永远回到原始 `chat_id`
5. **上下文隔离**不依赖 route，而依赖 `session_id`

因此：
- 路由决定“消息发给哪个 team / agent”
- `session_id` 决定“这个会话的上下文归谁”

即使多个群/私聊共用同一个 `team / agent`，也不会再共享 nanobot 上下文。

---

## 2. 已实现功能详解

### 2.1 Feishu 长连接接入（无需公网回调）

实现文件：
- `channels/feishu_ws.py`
- `channels/feishu_client.py`

说明：
- 使用 Feishu 官方 SDK WebSocket 长连接接收事件
- 不要求公网可访问 webhook URL
- 适合家庭网络、局域网服务器、内网环境部署

当前行为：
- 网关启动后主动连 Feishu
- 接收 `im.message.receive_v1`
- 接收并吞掉 `im.message.reaction.created_v1`，避免日志噪音

---

### 2.2 多 bot / 多 app 自动托管

实现文件：
- `channels/feishu_ws.py`

当 `configs/feishu_apps.yaml` 中配置多个 app 时：

- 主进程只启动一次 `python main.py`
- 主进程自动为每个 app 拉起一个子进程
- 每个子进程只消费自己对应的 Feishu app
- 子进程异常退出后会自动重拉

这样做的原因：
- `lark-oapi` 多 app 同进程 WebSocket 在实战中容易出现 event loop 冲突
- 因此采用“一个 app 一个子进程”的稳定方案

你不需要手工起多个网关终端。

---

### 2.3 通用路由：`channel + app_id + chat_id -> team / agent`

实现文件：
- `core/resolver.py`
- `core/service.py`
- `configs/bindings.yaml`

当前支持：

```yaml
channels:
  feishu:
    apps:
      <app_id>:
        chats:
          <chat_id>:
            team: gateway-dev
            agent: dev
            mode: inbox
            timeout_sec: 90
            reply_timeout_sec: 60
          default:
            ...
```

用途：
- 不同 bot 可进不同 team
- 同一 bot 的不同群可进不同 agent
- 也支持 `default` 作为兜底路由

---

### 2.4 inbox 模式等待真实 agent 回包

实现文件：
- `core/service.py`
- `core/executor.py`

不是简单执行完 `clawteam inbox send` 就返回“已转发”，而是：

1. 把消息送进目标 agent inbox
2. 记录发送时间
3. 轮询 leader inbox
4. 等待这个 agent 的真实回复
5. 拿到回复后再回渠道

因此终端用户收到的是 **真实 agent 输出**，不是中间态提示。

超时行为：
- 如果在 `reply_timeout_sec` 内没有拿到回包，返回：

```text
agent未在XXs内返回结果，请稍后重试。
```

---

### 2.5 worker 调 nanobot 执行真实回复

实现文件：
- `scripts/gateway_inbox_worker.py`

worker 职责：
- 从 `clawteam inbox receive` 拉取目标 agent 待处理消息
- 调用 `nanobot agent`
- 把结果写回 leader inbox

核心调用形态：

```bash
nanobot agent \
  --workspace <workspace> \
  --session <session_id> \
  --message <text> \
  --no-logs
```

这一步是当前“渠道网关真正连到 nanobot”的关键，不再依赖交互式终端人工处理。

---

### 2.6 回复内容清洗：只回 agent 正文

实现文件：
- `core/service.py`

worker 会把回包写成类似：

```text
DEV_REPLY: ...
MARKET_REPLY: ...
REPLY: ...
```

但终端用户不应该看到这些包装前缀。

因此网关在回 Feishu 前会自动清洗：
- `DEV_REPLY:`
- `MARKET_REPLY:`
- `REPLY:`
- 同类大写前缀

最终只把 **agent 正文** 发送给终端用户。

---

### 2.7 群聊只处理明确 @ 当前 bot

实现文件：
- `channels/feishu_ws.py`
- `channels/feishu_client.py`

行为：
- 启动时通过 `/open-apis/bot/v3/info` 获取当前 bot `open_id`
- 群聊消息必须在 mentions 中明确包含当前 bot 的 `open_id`
- 未 @ 当前 bot 的群消息直接忽略

这样可以避免：
- 同群内没 @ 机器人却误触发
- 多 bot 场景下 A bot 误处理 B bot 的 @ 消息
- 公司群 / 公共群中大面积误响应

---

### 2.8 私聊（p2p）无需 @，可直接回复

实现文件：
- `channels/feishu_ws.py`

行为：
- `chat_type == "p2p"` 时，不再要求 mention
- 私聊消息可直接进入路由处理

因此当前规则是：
- **群聊**：必须明确 @ 当前 bot
- **私聊**：无需 @，直接可回复

---

### 2.9 只处理用户消息，忽略 bot / app / system

实现文件：
- `channels/feishu_ws.py`

行为：
- 读取 `sender_type`
- 只有 `sender_type=user` 才继续处理
- bot / app / system 事件直接忽略

这一步是为了阻断自动回复环、系统事件串入、应用自身消息回灌。

---

### 2.10 忽略 bot 自己发出的消息

实现文件：
- `channels/feishu_ws.py`

行为：
- 对比消息发送者 `sender_id` 与当前 bot `open_id`
- 如果相同，直接忽略

作用：
- 防止 bot 自己发送的消息再次进入网关
- 避免回声 / 无限递归回复

---

### 2.11 按 message_id 去重

实现文件：
- `channels/feishu_ws.py`

行为：
- 在进程内维护 `message_id` TTL 缓存
- 默认保留 600 秒
- 相同 `(app_id, message_id)` 只处理一次

作用：
- SDK 重放时不重复执行
- 网络抖动或重复回调时不重复回复
- 降低误判为“机器人抽风连发”的风险

---

### 2.12 收到消息后自动添加 reaction

实现文件：
- `channels/feishu_ws.py`
- `channels/feishu_client.py`

当前行为：
- 网关成功接收到待处理消息后，先对原消息添加 reaction
- 当前固定使用：

```text
OneSecond
```

用途：
- 给用户即时反馈：机器人已收到并开始处理
- 比发一条“处理中”文案更轻量，不污染聊天内容

说明：
- 不同租户对 reaction 类型兼容性不同
- 当前仓库按实测稳定值固定为 `OneSecond`

---

### 2.13 处理 reaction 事件，消除 SDK 噪音日志

实现文件：
- `channels/feishu_ws.py`

当前已注册：
- `register_p2_im_message_receive_v1(on_message)`
- `register_p2_im_message_reaction_created_v1(on_reaction_created)`

作用：
- 吞掉 reaction.created 事件
- 避免日志里持续出现：

```text
processor not found, type: im.message.reaction.created_v1
```

---

### 2.14 会话隔离：不同群 / 私聊不再串 nanobot 上下文

实现文件：
- `channels/feishu_ws.py`
- `core/models.py`
- `core/service.py`
- `scripts/gateway_inbox_worker.py`

这是当前仓库非常重要的一项增强。

#### 背景

如果多个群 / 私聊共享同一个 `team / agent`，但传给 nanobot 的只是纯文本，那么模型侧可能会把不同聊天窗口当成同一个连续会话，导致：
- 群 A 的上下文串到群 B
- 私聊上下文串到群聊
- 两个不同业务群共用一个 agent 时互相污染历史上下文

#### 当前修法

为每条 Feishu 消息生成稳定 session：

```text
feishu:{app_id}:{chat_id}
```

然后：
1. 在渠道入站时写入 `InboundEvent.session_id`
2. GatewayService 投递时把 `session_id` 注入消息体
3. worker 解析消息体中的 `[SESSION_ID]...[/SESSION_ID]`
4. 调用 nanobot 时使用 `--session <session_id>`

#### 修复后的效果

- 群 1 和群 2 即使都走同一个 agent，也不会共享上下文
- 私聊和群聊即使共用 agent，也不会共享上下文
- 回复仍然只回原始 `chat_id`

#### 当前隔离级别

当前隔离键为：

```text
app_id + chat_id
```

这已经能解决“不同聊天窗口串上下文”的核心问题。

---

## 3. 配置文件说明

### 3.1 `configs/channels.yaml`

定义启用哪些渠道，以及对应参数。

典型用途：
- 指定 Feishu apps 配置文件路径
- 后续新增 DingTalk / 企业微信时也从这里挂载

---

### 3.2 `configs/feishu_apps.yaml`

一个 bot 一条配置，支持多条。

字段：
- `name`：机器人名称（可选，便于日志识别）
- `app_id`
- `app_secret`
- `verify_token`（可选）
- `encrypt_key`（可选）

示例：

```yaml
apps:
  - name: dev-bot
    app_id: cli_xxx
    app_secret: xxx
  - name: market-bot
    app_id: cli_yyy
    app_secret: yyy
```

---

### 3.3 `configs/bindings.yaml`

按渠道 / app / chat 映射到具体 team / agent。

示例：

```yaml
channels:
  feishu:
    apps:
      cli_xxx:
        chats:
          oc_group_1:
            team: gateway-dev
            agent: dev
            mode: inbox
            timeout_sec: 90
            reply_timeout_sec: 60
          oc_group_2:
            team: gateway-market
            agent: market
            mode: inbox
            timeout_sec: 90
            reply_timeout_sec: 60
          default:
            team: gateway-dev
            agent: dev
            mode: inbox
```

说明：
- `mode` 当前主要使用 `inbox`
- `reply_timeout_sec` 控制等待 agent 回包时长
- 可对不同聊天配置不同 team / agent

---

## 4. 目录结构

```text
.
├─ main.py
├─ README.md
├─ requirements.txt
├─ run_clawteam_notification_channel_gateway.ps1
├─ channels/
│  ├─ base.py
│  ├─ feishu_client.py
│  └─ feishu_ws.py
├─ core/
│  ├─ config.py
│  ├─ executor.py
│  ├─ log.py
│  ├─ models.py
│  ├─ resolver.py
│  └─ service.py
├─ configs/
│  ├─ channels.yaml
│  ├─ bindings.yaml
│  └─ feishu_apps.yaml
└─ scripts/
   └─ gateway_inbox_worker.py
```

---

## 5. 快速启动

### 5.1 Windows 本地调试

```powershell
cd clawteam_notification_channel_gateway_package
pip install -r requirements.txt
.\run_clawteam_notification_channel_gateway.ps1
```

---

### 5.2 Linux / Ubuntu 运行主网关

```bash
cd /home/ubuntu/ai-lab/services/clawteam_notification_channel_gateway_package
python3 -m venv /home/ubuntu/ai-lab/venvs/channel-gateway
source /home/ubuntu/ai-lab/venvs/channel-gateway/bin/activate
pip install -r requirements.txt
python main.py
```

如果 `feishu_apps.yaml` 中配置了多个 app，`python main.py` 会自动托管多子进程。

---

### 5.3 Linux / Ubuntu 运行 inbox worker

worker 需要单独跑，每个 agent 一条进程 / systemd service。

示例：

```bash
python scripts/gateway_inbox_worker.py \
  --team gateway-dev \
  --agent dev \
  --leader leader \
  --prefix DEV_REPLY \
  --workspace /home/ubuntu/.nanobot/workspace \
  --nanobot-bin /home/ubuntu/ai-lab/venvs/nanobot/bin/nanobot
```

`market` agent 同理再起一份。

---

## 6. 推荐 systemd 拆分方式

线上建议至少拆成 3 个服务：

1. `clawteam-gateway.service`
   - 负责 Feishu WebSocket 接入、路由、等待 leader 回包
2. `nanobot-gateway-dev.service`
   - 负责消费 `gateway-dev/dev` inbox
3. `nanobot-gateway-market.service`
   - 负责消费 `gateway-market/market` inbox

这样做的好处：
- 网关与 agent 执行解耦
- 单个 worker 卡住不会直接拖死 Feishu 接入
- 不同 agent 可以独立重启 / 独立扩容

---

## 7. 运行行为总结

### 群聊行为
- 必须明确 @ 当前 bot 才处理
- 收到后先加 `OneSecond` reaction
- 路由到指定 team / agent
- 等待真实 agent 回包
- 把 agent 正文回到原群

### 私聊行为
- 无需 @
- 收到后加 `OneSecond` reaction
- 路由后等待 agent 回包
- 把 agent 正文回到原私聊

### 防串 / 防环行为
- 忽略非 user 消息
- 忽略 bot 自己发的消息
- 按 message_id 去重
- 按 `app_id + chat_id` 做 nanobot session 隔离

---

## 8. 当前已知边界

### 8.1 当前上下文隔离粒度

当前按：

```text
session_id = feishu:{app_id}:{chat_id}
```

这意味着：
- 不同聊天窗口之间不会串上下文
- 但同一个 `chat_id` 内部，本来就会持续共享上下文（这正是聊天记忆需要的行为）

如果未来要做“同一群里按 thread / topic / user 再细分上下文”，可以继续把 session key 扩成更细粒度。

---

### 8.2 当前仅实现 Feishu

仓库架构已预留多渠道扩展位，但当前正式落地的只有：
- Feishu WebSocket adapter

钉钉 / 企业微信仍需后续补 adapter。

---

### 8.3 当前 worker 默认以文本模式调用 nanobot

当前 worker 通过：

```bash
nanobot agent --message ... --session ...
```

因此它天然适合“单轮消息 -> 单轮结果”的渠道网关场景。

---

## 9. 后续扩展建议

如果后面继续增强，优先级建议如下：

1. **把 session_id 进一步扩展到 thread / topic 粒度**（如果 Feishu 线程场景需要）
2. **补充更完整的 observability**：
   - 每条消息链路 trace id
   - 路由命中日志
   - 发送 / 回包耗时指标
3. **把敏感配置迁到 `.env` / systemd EnvironmentFile**
4. **补齐 DingTalk / 企业微信 adapter**
5. **增加重试 / 熔断 / 限流策略**

---

## 10. 安全说明

- `configs/feishu_apps.yaml` 中通常包含 `app_secret`，生产环境建议不要提交真实密钥
- 推荐改为环境变量注入，或在部署机上以私有配置覆盖
- 如果密钥曾在聊天记录、日志、脚本中出现过，应按已暴露处理并及时轮换

---

## 11. 一句话概括当前版本

当前版本已经具备：

> **Feishu 多 bot 长连接接入 + ClawTeam 路由 + nanobot worker 执行 + 真实回包 + 反回环 + 反重复 + 私聊/群聊差异处理 + chat 级上下文隔离**

适合作为后续扩展钉钉、企业微信、多 agent 通知网关的基础版本。
