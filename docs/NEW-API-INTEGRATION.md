# AI Seek 2API → new-api 对接指南

网关对本项目暴露 **OpenAI 兼容**接口，因此不需要新写渠道类型，用内置 `OpenAI` 渠道即可。

## 0. 前置

```bash
cd D:/aiseek-2api-app
pip install fastapi uvicorn httpx aiohttp playwright   # 已具备可跳过
AISEEK_MODE=mock python -m uvicorn server:app --host 127.0.0.1 --port 8099
curl -s http://127.0.0.1:8099/v1/models | head -c 200
```

> **务必先确认 `AISEEK_MODE`。** `mock` 返回本地合成文本（明确带 `[mock:...]` 前缀），`real` 才是打上游。
> 当前上游对无订阅账号统一 `10007 PRO_REQUIRED`，`real` 模式下客户端会收到 `HTTP 402`。**在 entitlement 解决前不要把 real 模式挂到生产渠道。**

## 1. 新建渠道

| 字段 | 值 |
|---|---|
| 类型 | `OpenAI`（type=1） |
| 名称 | `AI-Seek-2API` |
| 代理地址 / Base URL | `http://127.0.0.1:8099`（同机容器则用宿主机内网 IP，如 `http://172.17.0.1:8099`） |
| 密钥 | 任意非空串（网关不校验入站 key；**建议**在网关前置一层 key 校验，见 §4） |
| 模型 | 见 §2，按需勾选；或填 `*` 再在「模型重定向」里收敛 |
| 分组 | 自定义分组，例如 `aiseek` |

保存后用「测试」按钮打 `/v1/models`，应返回 28 个模型。

## 2. 模型名映射

上游 28 个模型对 new-api 来说可直接透传（id 中带 `/`，需确认 new-api 版本对斜杠模型名无过滤；本仓库版本按原样透传）。

推荐只放行通用对话 10 个：

```
qwen/qwen-coder-32b
deepseek/deepseek-chat
deepseek/deepseek-r1
qwen/qwen3-32b
openai/gpt-5-mini
openai/gpt-5
google/gemini-2.5-flash-lite
google/gemini-2.0
google/gemini-3-pro
anthropic/claude-sonnet-4.5
grok/grok-4
moonshotai/kimi-k2.5
```

垂类人设模型（`relationship/*`、`destiny/*`、`storyteller/*`、`wealth/*`、`tourism/*`）是 App 内的角色扮演预设，**建议不接入**——它们带固定系统提示，和通用对话语义冲突。

## 3. 计费

上游不返回 usage（`chat/send` 只吐 `content` 增量）。网关非流式响应里填的是**估算值**：

```
prompt_tokens   = len(prompt)//4
completion_tokens = len(text)//4
```

⇒ 在 new-api 侧**不要**用「按 usage 计费」，请用**按次计费**或**按输入/输出长度倍率计费**，否则会算错。

推荐：渠道倍率 `1`，模型定价用「固定价/次」，并在渠道备注写明 `estimated usage`。

## 4. 入站鉴权（建议加）

网关默认不校验入站 key，仅适合本机闭环。要在公网暴露前至少做一层：

```python
# server.py 顶部加
import hashlib
GATEWAY_KEY = os.getenv("AISEEK_GATEWAY_KEY", "")

@app.middleware("http")
async def auth(request, call_next):
    if GATEWAY_KEY and request.url.path.startswith(("/v1", "/native")):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if token != GATEWAY_KEY:
            return JSONResponse({"error": {"message": "invalid gateway key"}}, status_code=401)
    return await call_next(request)
```

然后 new-api 渠道「密钥」填同一个 `AISEEK_GATEWAY_KEY`。

## 5. 验证四步

```bash
# 1 直连网关
curl -s http://127.0.0.1:8099/health

# 2 非流式
curl -s -X POST http://127.0.0.1:8099/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek/deepseek-chat","messages":[{"role":"user","content":"ping"}]}'

# 3 流式
curl -N -X POST http://127.0.0.1:8099/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek/deepseek-chat","messages":[{"role":"user","content":"ping"}],"stream":true}'

# 4 经 new-api 打（把 sk-xxx 换成 new-api 令牌）
curl -s -X POST http://127.0.0.1:3000/v1/chat/completions \
  -H 'Authorization: Bearer sk-xxx' -H 'Content-Type: application/json' \
  -d '{"model":"deepseek/deepseek-chat","messages":[{"role":"user","content":"ping"}]}'
```

## 6. 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `HTTP 402` + `PRO_REQUIRED` / `code:10007` | 上游订阅校验（预期行为） | entitlement 未解决前只能 mock |
| `403 app_id can't be null` | 网关到上游缺 `X-App-Id` | 检查 `aiseek_client.APP_ID` |
| `401 access_token invalid` | 账号 token 失效 | 网关会自动重注册；看 `server.log` |
| `400 invalid JSON body` | 请求体编码非 UTF-8（Windows curl 发中文易踩） | 用 `--data-binary @file` 或 Python 客户端 |
| new-api 侧 500 但网关日志正常 | 模型名含 `/` 被上游/中间层处理异常 | 先在「模型重定向」里换成别名验证 |
| 账号池一直增长 | 每个 secret 一个新 uid（设计如此） | `accounts.json` 里控制数量，`AISEEK_POOL_SIZE` 管初始值 |
