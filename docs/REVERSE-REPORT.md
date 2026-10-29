# AI Seek 逆向报告 · R1

> 目标: `AI_Seek_v1.6.5-25052251_(25052251)_Mod.apk`
> 结论一句话: **协议 100% 打通（注册、签发 JWT、模型目录、对话 SSE 全部可用），但对话被服务端订阅校验（PRO）前置拦截，无订阅账号在全部 28 个模型上统一返回 `code=10007`。**

---

## 1. 应用指纹

| 项 | 值 |
|---|---|
| 包名 | `ai.chatbot.ask.chat.deep.seek.assistant.search.free` |
| versionName / Code | `1.6.5-25052251` / `25052251` |
| minSdk / targetSdk / compileSdk | 24 / 34 / 34 |
| 架构 | Flutter（`libapp.so` = Dart AOT 快照，Dart **3.3.4**），仅 `arm64-v8a` |
| 签名相关 native | `libsigner.so` = **Adjust SDK** 归因签名，**不是** App 请求签名 |
| 广告/分析 SDK | Pangle（`libpglarmor`、`libnms`、`tt_ugen_layout`）、AppLovin、Moloco、Explorestack、Adjust、Firebase(Analytics/Auth/RC/Storage)、Bytedance APM |
| Firebase 项目 | `ai-seek-ai-chat-assistant`；Web API Key `AIzaSyA8ycCz5FG3tkuigwc-IPNr0_g9VOv8MWs`（匿名登录已被服务端关闭: `ADMIN_ONLY_OPERATION`） |
| 业务框架 | 白牌 SaaS「Guru」(Castbox)：`guru_app` / `guru_ai_ui` / `guru_api` |
| 明文流量 | `network_security_config.xml` 未放开 cleartext；全程 HTTPS |

静态证据: `libapp.so` 字符串表 47,313 条（`D:\aiseek_strings.txt`），包内资源 `resources.arsc` 取 Firebase key。

---

## 2. 主机与服务拓扑

```
App
 ├─ 对话网关     https://ai-seek.thebetter.ai        (Go, /v1/chat/*, /v3/chat/send, /healthz, /metrics)
 │   └─ 测试环境  https://test.ai-seek.thebetter.ai   (同协议, 行为一致)
 ├─ SaaS 账号     https://saas.castbox.fm/auth        (Spring Boot, context=/auth)
 ├─ 设备档案      https://saas.castbox.fm/device      (context=/device)
 ├─ 订单/订阅     https://saas.castbox.fm/order       (context=/order)
 └─ 其它微服务    /app /push /cms /tool /motivation /third-party-data
   CDN           https://d3g322f8itkvhj.cloudfront.net  (模型 logo)
   Firebase      ai-seek-ai-chat-assistant.firebasestorage.app
```

微服务发现（`/auth/actuator/health` 的 discoveryComposite）：
`leaderboard-service, motivation-service, appsflyer-service, event-service, auth-service, gateway-service, job-service, tool-service, order-service, app-service, cms-service, third-party-data-service, push-service, device-service`

> ⚠️ 安全观察（非利用，仅记录）: 这些 Spring 服务在公网暴露 `/actuator/mappings`、`/actuator/health`，且 `/<svc>/v2/api-docs`（springfox）对匿名开放，因此**全部内部接口都能被自描述枚举**。本报告的路由表即来自该处的官方自描述，非爆破。

---

## 3. 认证协议（关键突破口）

### 3.1 设备/匿名注册 → JWT

```http
POST https://saas.castbox.fm/auth/api/v1/tokens/provider/secret
X-APP-ID: ai-seek
Content-Type: application/json

{"appSource":"APP","secret":"<任意唯一串，App 侧=device 指纹>"}
```

响应：

```json
{"data":{"uid":"AS-XXXXXX","uidInt":7411XXX,"token":"<JWT RS256, 345B>"}}
```

* **`secret` 不做校验**，只作为「设备指纹」参与 uid 派生：同 secret 重复注册 → **同 uid**（幂等），token 每次重新签发。不同 secret → 新 uid。⇒ 账号**可无限生成**，无需手机号/邮箱/Google/Firebase。
* `appSource` 可取 `APP | WEB | FACEBOOK | FB_JIGSORT_PUZZLE`，实测对后续权限无差异。
* `X-APP-ID` 必须是已注册 appId，否则 `400 appId not exists: xxx`。

JWT payload（实测）：

```json
{"sub":"AS-XXXXXX","aud":"ai-seek","provider":"uuid","iss":"ai-seek",
 "exp":2108644657,"iat":1793284657}
```

`exp` ≈ 2036 年（超长有效期），无需刷新即可长期使用 `/auth/api/v1/renewals/token` 续签。

### 3.2 设备档案（可选）

```http
POST https://saas.castbox.fm/device/api/v1/devices
X-APP-ID: ai-seek
X-UID: AS-XXXXXX
X-ACCESS-TOKEN: <JWT>
{"deviceId":"<uuid>","androidId":"<16hex>","appIdentifier":"<包名>","appVersion":"1.6.5","uid":"AS-XXXXXX"}
```
→ `200 {}`。注意：紧接着 `GET /device/api/v1/devices/{deviceId}` 仍是 `404 Device Not Found`，且 `/auth/api/v1/user` 的 `lastDeviceId` 仍为 `null`。
**结论：设备档案与对话权限无关，实测绑定后仍是 10007。**

---

## 4. 对话协议

```http
POST https://ai-seek.thebetter.ai/v3/chat/send
X-App-Id: ai-seek
X-Access-Token: <JWT>
Content-Type: application/json
Accept: text/event-stream

{"messages":[{"role":"user","content":"hi"}],"model":"deepseek/deepseek-chat"}
```

* 响应恒为 `Content-Type: text/plain`，但内容是 SSE 帧：`data: {"content":"...","done":false}\n\n`，末帧 `done:true`。
* **业务错误也走 200 + SSE**，不抛 HTTP 状态：`data: {"content":"","done":true,"code":10007}`。
* 缺头时的错误语义（实测）：
  * 无 `X-App-Id` → `403 app_id can't be null`
  * 无 `X-Access-Token` → `403 access token can't be null`
  * token 非法 → `401 access_token invalid`
* `stream` 字段、`chatId`/`conversationId`、`X-Device-Info`、`X-Platform`、`User-Agent` 均不改变结果。
* `/v1/chat/send`（v1 路径）同样命中该 handler，返回一致。

### 4.1 模型目录（匿名可读）

`GET /v1/chat/list_models` → `200 {"code":0,"data":{"models":[...]}}`，**无需 token**，共 **28 个**（12 通用 + 16 人设/垂类）：

| id | 展示 | paidOnly |
|---|---|---|
| qwen/qwen-coder-32b | Qwen Coder | false |
| google/gemini-3-pro | Gemini 3.1 Pro | true |
| openai/gpt-5-mini | GPT 5-mini | false |
| deepseek/deepseek-chat | DeepSeek V4-Flash | false |
| openai/gpt-5 | GPT 5.4 | true |
| anthropic/claude-sonnet-4.5 | Claude Sonnet 5 | true |
| moonshotai/kimi-k2.5 | Kimi K2.5 | true |
| google/gemini-2.0 | Gemini 3 Flash | true |
| deepseek/deepseek-r1 | DeepSeek V4-Pro | true |
| qwen/qwen3-32b | Qwen 3.7 Plus | false |
| grok/grok-4 | Grok 4.6 | true |
| google/gemini-2.5-flash-lite | Gemini 3.1 Flash Lite | false |
| relationship/luna · john · linda | 情感教练（Junior/Senior/Guru） | false/true/true |
| destiny/default · oracle · sage | 命理（Daily/Western/Eastern） | false/true/true |
| storyteller/story_muse · novel_crafter · plot_architect · roleplay | 故事创作 | false/false/true/false |
| wealth/starter · strategist · architect | 理财 | false/true/true |
| tourism/trip_scout · route_planner · local_concierge | 旅行 | false/true/true |

`GET /v1/chat/get_supported_mime_types` → 11 种（image/jpeg、png、gif、webp、heic、svg、application/pdf、doc、docx、ppt、pptx），⇒ 支持图片与文档附件。

### 4.2 上游真实供给（来自 `/metrics` 标签）

对话网关是自建 LiteLLM 多路由，`gpt_request_total` 标签直接暴露上游池：

`cometapi_litellm`、`deepseek_litellm`、`vertex_litellm`、`azure_litellm`、`ominilink_litellm`、`shubiaobiao_litellm`（+ `unknown`）

同池状态码枚举（`status` 标签）：`OK / PRO_INVALID / PRO_EXPIRED / vip_expiry / CHAT_CONTENT_MODERATION / CHAT_CONTEXT_EXCEED / EMPTY_RESPONSE / TIMEOUT_ON_READING_DATA_FROM_SOCKET / TASK_STATUS_COMPLETED / TASK_STATUS_FAILED …`
另有 `image-generate-llm` / `image-generate-llm-backup` 两条出图模型路由（对应 `generate_image_token` 语义）。

---

## 5. 10007 证据链（本轮阻塞点）

判定：**`10007` = 订阅/PRO 校验失败，且位于 handler 最前端，早于任何参数校验。**

| # | 实验 | 结果 |
|---|---|---|
| 1 | 8 个新账号 × 各 1 次对话（`stress_test.py --upstream`） | `10007` **8/8** |
| 2 | 28 个模型逐个试（含 `paidOnly=false` 的 5 个） | 全部 `10007` |
| 3 | `appSource` = APP/WEB/FACEBOOK/FB_JIGSORT_PUZZLE | 全部 `10007` |
| 4 | 注册后补 `POST /device/api/v1/devices` + `POST /auth/api/v1/user{lastDeviceId}` | 仍 `10007` |
| 5 | 头部矩阵（X-UID / X-Device-Info / X-Platform / X-App-Version / UA / X-UID2） | 仍 `10007` |
| 6 | 请求体注入 18 个布尔位（rewarded/isPro/vip/trial/free…）+ 8 个枚举位 | 仍 `10007` |
| 7 | 畸形请求（空 messages、不存在的 model） | 仍 `10007`，**说明校验顺序在参数之前** |
| 8 | 生产 `ai-seek.thebetter.ai` vs 测试 `test.ai-seek.thebetter.ai` | 一致 `10007` |
| 9 | `/metrics` 存在 `status="PRO_INVALID"` / `"PRO_EXPIRED"` / `"vip_expiry"` 任务状态 | 语义吻合 |

App 侧文案印证：`"Please upgrade to premium to use this model"`、`"3-day free trial"`、`"Watch an ad for 6 free uses, or subscribe for unlimited access."`
⇒ 免费额度来自 **Google Play 3 天试用 / 激励视频服务端回调**，没有可客户端伪造的开关（本轮穷举已排除）。

**本轮不复现的部分**：付费/试用链路（`/order/api/v1/orders/android` + Play 收据校验），按既定红线不发起任何真实付费。

---

## 6. SaaS 端路由面（节选，来自官方自描述）

| 服务 | 端点 | 用途 |
|---|---|---|
| auth | `POST /api/v1/tokens/provider/secret` | **设备匿名注册（已用）** |
| auth | `POST /api/v1/tokens/provider/{google,apple,facebook,twitter,...}` | 三方登录换 token |
| auth | `POST /api/v1/renewals/{token,firebase,uid2_token}` | 续签 |
| auth | `GET/POST /api/v1/user` | 用户档案（`uid/uidInt/lastDeviceId/createdAt`） |
| auth | `POST /api/v1/tokens/delete` | 注销 |
| auth | `POST /api/v1/bindings/provider/*` | 账号绑定 |
| device | `POST/GET/PUT/DELETE /api/v1/devices[/{id}]` | 设备档案 |
| order | `POST /api/v1/orders/android`、`/api/v2/orders/android/{iap,sub}` | Play 订单/订阅上报 |
| order | `GET /api/v1/orders/app-bundle`、`/subscribe/list`、`POST /subscribe/validity_status` | 订阅态查询（本 app 返回 `data:[]`） |
| order | `POST /api/v1/orders/activation/redeem` | 激活码兑换 |
| order | `GET /api/v1/orders/stripe/products`、`POST /orders/stripe/checkout-sessions` | Stripe 结算 |
| push | `POST /api/v1/push/send` | FCM 推送 |
| cms | `/cms/api/v1/appconfigs/{appId}` | App 配置（需 `X-ADMIN-TOKEN`） |
| tool | `GET /api/v1/system/time` | 校时 |

App 里存在但线上 404 的路径：`/v1/chat/stop`、`/v1/token/generate_image_token`（按开关/区域关闭）。

---

## 7. 本轮结论

* ✅ 完全掌握：注册、身份派生、JWT、模型目录、对话 SSE、错误语义、上游供给池、SaaS 全路由面。
* ✅ 工程化：OpenAI 兼容网关、账号池、SSE 解析、mock/real 双模、演示页、压测与浏览器验收（见 README）。
* ❌ 未打通：**产出真实对话**。缺的是「订阅态」这一项服务端状态，不是协议。

下一步需要用户决策的三条路（成本/合规差异巨大），详见交接讨论。
