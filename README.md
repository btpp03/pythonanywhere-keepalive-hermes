# pythonanywhere-keepalive-hermes

PythonAnywhere 免费账号上的 **哪吒监控 agent** 保活。

## 为什么需要它

免费版 PA 的出站必须走 `proxy.server:3128` 白名单，面板域名 `nezhak2.btpp.ggff.net` 不在白名单里。
所以链路是：

```
agent(PA) --HTTPS_PROXY=127.0.0.1:18081--> ~/nz/bridge.py (本地 CONNECT 代理)
   --squid CONNECT--> *.ngrok-free.app --wss--> VPS relay --裸TCP--> 面板:443
```

TLS 始终是 agent ↔ 面板端到端，中间只搬字节（gRPC 需要 h2 + trailers，解析型代理会废掉它）。

**问题**：PA 免费版隔一段时间会回收控制台里的后台进程，`setsid nohup` 也活不下来
（实测两个进程一起消失，面板转离线）。所以必须有外部的东西定时登录、重新拉起。

## 这个仓做什么

`pa_keepalive.py` 每次跑：

1. `requests` 登录 PA（Django 表单 + csrf）
2. 复用 bash 控制台，走 SockJS/WebSocket 发命令
3. 探三件事：`bridge.py` 在不在、`nezha-agent` 在不在、经本地桥对面板的 HTTPS 探测是否 200
4. 任一不满足 → 跑 `NZKEY=*** bash ~/nz/start.sh`（幂等：先 pkill 再起），20 秒后复探
5. 真掉过线才推 TG；健康空转完全静默；失败必推

退出码：`0` 健康或已修复；`1` 仍不通 → run 变红，GitHub 失败邮件也是告警通道。

## 触发方式

- **cron-job.org 每 10 分钟**（主力）：`POST https://api.github.com/repos/btpp03/pythonanywhere-keepalive-hermes/actions/workflows/<workflow_id>/dispatches`，body `{"ref":"main","inputs":{"quiet":"true"}}`
- `on: schedule:` 保留作备用（本账号 GA 原生 schedule 长期不触发）
- 手动：Actions → Run workflow（`mode=status` 只探测，不动进程）

## 需要的 secrets

| 名字 | 用途 |
|---|---|
| `PA_USER` / `PA_PASS` | PythonAnywhere 账号 |
| `NZ_KEY` | 哪吒 agent client secret |
| `RELAY_HOST` | 当前 ngrok 域名（免费版每次重启会变，变了要重钉 bridge.py 的 `RELAY`） |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | 可选，掉线/修复通知 |

## 已知边界

- ngrok 免费版地址是随机的，**VPS 上 ngrok 重启后 `RELAY_HOST` 秘钥必须同步更新**，否则探测不到 200，脚本会在日志里点明 `PR=000`。
- PA 免费版配额：控制台 100 CPU 秒/天、最多 2 个控制台；本脚本每跳只发几条短命令，占用极小。
- 换个面板/密钥：改 `nz_start.sh` 里的 `-s <panel> --tls` 与 secret `NZ_KEY`。
