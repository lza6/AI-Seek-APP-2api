# AI Seek 2API（R1）

`ai.chatbot.ask.chat.deep.seek.assistant.search.free` v1.6.5 · 协议还原 + OpenAI 兼容网关 + 演示页 + 压测

---

## TL;DR

| 能力 | 状态 |
|---|---|
| 设备匿名注册 / JWT 签发 | ✅ 已验证（任意 `secret` → 稳定 uid，账号可无限生成） |
| 模型目录（28 个） | ✅ 已验证（匿名可读） |
| 对话 SSE 通道 | ✅ 通道打通，解析完成 |
| 真实对话内容产出 | ❌ 上游订阅校验前置拦截，全部模型返回 `code=10007` |
| 网关（mock/real 双模） | ✅ 已验证（mock 流式/非流式、错误映射、账号池） |
| 演示页 + 浏览器 e2e | ✅ 已验证（Playwright PASS，见 `bench/demo_ui.png`） |
| 压测 | ✅ 已验证（网关 32 并发 33.6 rps / 100% 成功；上游 8/8 命中 10007） |

协议细节与证据链见 **[docs/REVERSE-REPORT.md](docs/REVERSE-REPORT.md)**；接 new-api 见 **[docs/NEW-API-INTEGRATION.md](docs/NEW-API-INTEGRATION.md)**。

---

## 快速开始

```bash
pip install fastapi uvicorn httpx aiohttp playwright

# 1) 启动网关（默认 mock：本地合成流，不触达上游）
python -m uvicorn server:app --host 127.0.0.1 --port 8099

# 2) 打开演示页
#    http://127.0.0.1:8099/demo

# 3) 打接口
curl -s http://127.0.0.1:8099/v1/models
curl -s -X POST http://127.0.0.1:8099/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek/deepseek-chat","messages":[{"role":"user","content":"ping"}]}'
```

### 真实上游模式

```bash
AISEEK_MODE=real python -m uvicorn server:app --port 8100
curl -s -X POST http://127.0.0.1:8100/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"deepseek/deepseek-chat","messages":[{"role":"user","content":"hi"}]}'
# -> HTTP 402 {"error":{"type":"PRO_REQUIRED","code":10007,...}}   ← 上游订阅校验，如实透出
```

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 模式/池/指标 |
| GET | `/api/stats` | 延迟分位、PRO 拦截数、按模型计数 |
| GET | `/v1/models` | OpenAI 格式 28 模型（带 `meta.paidOnly`） |
| POST | `/v1/chat/completions` | OpenAI 兼容，`stream=true/false` |
| POST | `/v1/chat/send` | 上游原生协议透传（SSE 原样回吐） |
| GET | `/native/models` | 上游原始模型目录（real 模式取线上） |
| GET | `/demo` | 中文演示页 |

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `AISEEK_MODE` | `mock` | `mock` / `real` |
| `AISEEK_PORT` / `AISEEK_HOST` | `8099` / `127.0.0.1` | |
| `AISEEK_POOL_SIZE` | `4` | 首次启动生成的 secret 数 |
| `AISEEK_CONCURRENCY` | `8` | 上游并发闸门 |
| `AISEEK_PROXY` | 空 | 上游代理，如 `http://127.0.0.1:7890` |
| `AISEEK_DEFAULT_MODEL` | `deepseek/deepseek-chat` | 请求未带 model 时 |
| `AISEEK_MOCK_FIRST_TOKEN` / `AISEEK_MOCK_TOKEN_INTERVAL` | `0.18` / `0.02` | mock 首字延迟与分块间隔 |
| `AISEEK_GATEWAY_KEY` | 空 | 入站 key 校验（见对接文档 §4） |

## 验证与压测

```bash
# 网关压测（mock）
python stress_test.py --levels 4,16,32 --per 30

# 上游证据采集：注册 N 账号各发一条，统计业务码分布
python stress_test.py --upstream --accounts 8

# 演示页真实浏览器验收（截图落到 bench/demo_ui.png）
python verify_ui.py
```

本轮实测结果：

```
网关 (mock):
  health / models / 400 错误路径      100% (1~2ms p50)
  chat 并发 32 (stream)    32/32  100%  p50 947ms  rps 33.6
  chat 并发 32 (nonstream) 32/32  100%  p50 2075ms rps 15.4

上游 (real, 8 账号):
  业务码分布 {10007: 8}          ← 100% 订阅拦截
  注册 p50 2174ms / 对话 p50 2032ms（含本机代理隧道）
```

## 目录

```
aiseek_client.py   上游协议封装（注册/JWT/设备/模型/SSE）
server.py          FastAPI 网关（mock/real、账号池、OpenAI 兼容、原生透传）
demo.html          中文演示页（流式渲染 + 实时指标）
stress_test.py     网关压测 + 上游证据采集
verify_ui.py       Playwright 端到端验收
accounts.json      账号池（secret→uid→token；本地生成，勿提交）
bench/             压测与截图产物
docs/              逆向报告 / new-api 对接指南
```

## 合规边界

* 本仓库只做**协议互操作研究**：不绕过付费墙、不伪造订阅回调、不触碰支付链路。
* `real` 模式下上游返回的 `10007` 原样透出，网关**不会**用 mock 内容伪装成功。
* 未发起任何真实付费请求（无 Play/Stripe 交易）。
