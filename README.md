# ClawTeam Notification Channel Gateway

clawteam通知渠道网关（可扩展）：
- 统一接收多渠道消息（当前已实现 Feishu 长连接）
- 按 `channel + app + chat` 绑定到 ClawTeam `team/agent`
- 回传执行结果到原会话

## 目录结构

- `main.py`：程序入口
- `core/`：核心能力（配置/模型/绑定解析/执行器/服务）
- `channels/`：渠道适配器（当前 `feishu_ws.py`）
- `configs/`：配置（channels、bindings、feishu_apps）
- `run_clawteam_notification_channel_gateway.ps1`：Windows 启动脚本

## 扩展方式

后续接入钉钉/企微时：
1. 在 `channels/` 新增 adapter（继承 `ChannelAdapter`）
2. 输出统一 `InboundEvent`
3. 调用 `GatewayService.handle_event()`
4. 在 `configs/channels.yaml` 打开对应渠道

## 快速启动（Windows）

```powershell
cd clawteam_notification_channel_gateway_package
pip install -r requirements.txt
.\run_clawteam_notification_channel_gateway.ps1
```

## 配置说明

### 1) channels.yaml

定义启用的渠道和其适配器参数（例如 feishu apps 文件路径）。

### 2) feishu_apps.yaml

配置多个飞书应用（每个机器人一条）：
- `app_id`
- `app_secret`
- `verify_token`（可选）
- `encrypt_key`（可选）

### 3) bindings.yaml

通用绑定规则：

`channels.<channel>.apps.<app_id>.chats.<chat_id> -> {team, agent, mode, timeout_sec}`

并支持 `default` 兜底。

## 备注

- 当前仅实现 Feishu 长连接接入（不依赖公网回调地址）。
- 钉钉、企微 adapter 已预留架构位，可按同一接口扩展。
