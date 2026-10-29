"""
AI Seek (ai.chatbot.ask.chat.deep.seek.assistant.search.free) 上游协议客户端。

协议还原来源: 静态(Dart AOT 字符串表) + 黑盒(线上端点探测) + SaaS 服务端自描述
(/auth|/device|/order/... /actuator/mappings 与 /v2/api-docs)。详见 docs/REVERSE-REPORT.md。

链路:
    POST {AUTH}/api/v1/tokens/provider/secret  {appSource, secret}   -> {uid, uidInt, token}
    POST {DEVICE}/api/v1/devices              (X-UID, X-ACCESS-TOKEN) -> {}
    POST {CHAT}/v3/chat/send                  (X-App-Id, X-Access-Token) -> SSE: data: {...}

关键结论(实测):
  * `secret` 是设备指纹位，服务端按它幂等映射到同一个 uid（同一 secret 重复注册 -> 同 uid，token 重新签发）。
  * chat 头部 `X-App-Id` / `X-Access-Token`，缺失分别报 `app_id can't be null` / `access_token invalid`。
  * 全部 28 个模型在无订阅账号上直接返回 `code:10007`（PRO 校验前置，早于任何参数校验）。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable

import httpx

APP_ID = "ai-seek"
AUTH_BASE = "https://saas.castbox.fm/auth"
DEVICE_BASE = "https://saas.castbox.fm/device"
ORDER_BASE = "https://saas.castbox.fm/order"
CHAT_BASE = "https://ai-seek.thebetter.ai"

APP_PACKAGE = "ai.chatbot.ask.chat.deep.seek.assistant.search.free"
APP_VERSION = "1.6.5-25052251"
APP_VERSION_NUMBER = 25052251

# 上游业务码（实测/推断标注见 docs）
CODE_OK = 0
CODE_PRO_REQUIRED = 10007  # 实测: 无订阅账号 100% 命中，先于参数校验
CODE_CONTENT_MODERATION = 21010  # 由 /metrics status=CHAT_CONTENT_MODERATION 侧证，未直接复现

CODE_LABELS = {
    CODE_PRO_REQUIRED: "PRO_REQUIRED",
    CODE_CONTENT_MODERATION: "CONTENT_MODERATION",
}


class AISeekError(Exception):
    def __init__(self, message: str, *, status: int | None = None, code: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body

    @property
    def label(self) -> str:
        if self.code in CODE_LABELS:
            return CODE_LABELS[self.code]
        return "UPSTREAM_ERROR"


@dataclass
class Account:
    secret: str
    uid: str = ""
    uid_int: int = 0
    token: str = ""
    issued_at: float = 0.0
    device_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "secret": self.secret,
            "uid": self.uid,
            "uidInt": self.uid_int,
            "token": self.token,
            "issuedAt": self.issued_at,
            "deviceId": self.device_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Account":
        return cls(
            secret=d["secret"],
            uid=d.get("uid", ""),
            uid_int=d.get("uidInt", 0) or 0,
            token=d.get("token", ""),
            issued_at=d.get("issuedAt", 0.0) or 0.0,
            device_id=d.get("deviceId", "") or "",
        )


def new_secret() -> str:
    """`secret` 只需是稳定唯一串；uuid4 与 App 侧设备指纹等效（实测同 uid 幂等）。"""
    return str(uuid.uuid4())


@dataclass
class DeviceProfile:
    device_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    android_id: str = ""

    def __post_init__(self) -> None:
        if not self.android_id:
            self.android_id = uuid.uuid4().hex[:16]

    def payload(self, uid: str) -> dict[str, Any]:
        return {
            "deviceId": self.device_id,
            "androidId": self.android_id,
            "appIdentifier": APP_PACKAGE,
            "appVersion": APP_VERSION,
            "appVersionNumber": APP_VERSION_NUMBER,
            "brand": "samsung",
            "model": "SM-G991B",
            "deviceType": "phone",
            "appCountry": "US",
            "deviceCountry": "US",
            "language": "en",
            "locale": "en_US",
            "timezone": "America/New_York",
            "utcOffset": -300,
            "pushType": "FCM",
            "pushNotificationEnable": True,
            "uid": uid,
        }


class AISeekClient:
    """单账号客户端。一个账号 = 一个 secret。"""

    def __init__(self, account: Account | None = None, *, timeout: float = 90.0, proxy: str | None = None):
        self.account = account or Account(secret=new_secret())
        self.timeout = timeout
        self.proxy = proxy

    # ---------- 传输层 ----------
    def _headers(self, *, auth: bool = False, uid: bool = False) -> dict[str, str]:
        h = {"Content-Type": "application/json", "X-APP-ID": APP_ID}
        if uid:
            h["X-UID"] = self.account.uid
        if auth:
            h["X-ACCESS-TOKEN"] = self.account.token
        return h

    async def _request_json(self, client: httpx.AsyncClient, method: str, url: str, **kw) -> tuple[int, Any]:
        r = await client.request(method, url, **kw)
        try:
            data = r.json()
        except Exception:
            data = None
        if r.status_code >= 400 and not (isinstance(data, dict) and "code" in data):
            raise AISeekError(
                f"HTTP {r.status_code} {url}",
                status=r.status_code,
                body=r.text[:400],
            )
        return r.status_code, data

    # ---------- 协议步骤 ----------
    async def register(self, client: httpx.AsyncClient | None = None) -> Account:
        """POST /auth/api/v1/tokens/provider/secret —— 设备/匿名注册，返回 JWT。"""
        own = client is None
        client = client or httpx.AsyncClient(timeout=self.timeout, proxy=self.proxy)
        try:
            status, data = await self._request_json(
                client,
                "POST",
                f"{AUTH_BASE}/api/v1/tokens/provider/secret",
                headers=self._headers(),
                json={"appSource": "APP", "secret": self.account.secret},
            )
            d = (data or {}).get("data") or {}
            if not d.get("token"):
                raise AISeekError(f"register failed: {data}", status=status, body=json.dumps(data)[:400])
            self.account.uid = d.get("uid", "")
            self.account.uid_int = d.get("uidInt", 0) or 0
            self.account.token = d["token"]
            self.account.issued_at = time.time()
            return self.account
        finally:
            if own:
                await client.aclose()

    async def renew(self, client: httpx.AsyncClient | None = None) -> Account:
        """POST /auth/api/v1/renewals/token —— 换发 JWT（需现有 token 有效）。"""
        own = client is None
        client = client or httpx.AsyncClient(timeout=self.timeout, proxy=self.proxy)
        try:
            status, data = await self._request_json(
                client,
                "POST",
                f"{AUTH_BASE}/api/v1/renewals/token",
                headers=self._headers(auth=True, uid=True),
                json={},
            )
            d = (data or {}).get("data") or {}
            if d.get("token"):
                self.account.token = d["token"]
                self.account.issued_at = time.time()
            return self.account
        finally:
            if own:
                await client.aclose()

    async def register_device(self, device: DeviceProfile, client: httpx.AsyncClient | None = None) -> dict:
        """POST /device/api/v1/devices —— 设备档案上报（chat 侧 entitlement 无关，实测不影响 10007）。"""
        own = client is None
        client = client or httpx.AsyncClient(timeout=self.timeout, proxy=self.proxy)
        try:
            status, data = await self._request_json(
                client,
                "POST",
                f"{DEVICE_BASE}/api/v1/devices",
                headers=self._headers(auth=True, uid=True),
                json=device.payload(self.account.uid),
            )
            self.account.device_id = device.device_id
            return {"status": status, "body": data}
        finally:
            if own:
                await client.aclose()

    async def user_info(self, client: httpx.AsyncClient | None = None) -> dict:
        own = client is None
        client = client or httpx.AsyncClient(timeout=self.timeout, proxy=self.proxy)
        try:
            _, data = await self._request_json(
                client,
                "GET",
                f"{AUTH_BASE}/api/v1/user",
                headers=self._headers(auth=True, uid=True),
            )
            return (data or {}).get("data") or {}
        finally:
            if own:
                await client.aclose()

    # ---------- 模型 ----------
    @staticmethod
    async def list_models(client: httpx.AsyncClient | None = None, *, proxy: str | None = None) -> list[dict]:
        """GET /v1/chat/list_models —— 匿名可读，无需 token。"""
        own = client is None
        client = client or httpx.AsyncClient(timeout=60, proxy=proxy)
        try:
            r = await client.get(f"{CHAT_BASE}/v1/chat/list_models", headers={"X-APP-ID": APP_ID})
            r.raise_for_status()
            return (r.json().get("data") or {}).get("models") or []
        finally:
            if own:
                await client.aclose()

    @staticmethod
    async def supported_mime_types(client: httpx.AsyncClient | None = None, *, proxy: str | None = None) -> list[str]:
        own = client is None
        client = client or httpx.AsyncClient(timeout=60, proxy=proxy)
        try:
            r = await client.get(f"{CHAT_BASE}/v1/chat/get_supported_mime_types", headers={"X-APP-ID": APP_ID})
            r.raise_for_status()
            return (r.json().get("data") or {}).get("mimeTypes") or []
        finally:
            if own:
                await client.aclose()

    # ---------- 对话 ----------
    def _chat_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-App-Id": APP_ID,
            "X-Access-Token": self.account.token,
            "Accept": "text/event-stream",
        }

    async def chat_stream(
        self,
        messages: Iterable[dict],
        model: str,
        *,
        client: httpx.AsyncClient | None = None,
        extra: dict | None = None,
    ) -> AsyncIterator[dict]:
        """POST /v3/chat/send —— SSE。逐条 yield 解析后的事件字典。

        上游一直是 `text/plain` 的 `data: {...}\\n\\n` 流；结束事件带 done=true。
        业务错误也走同一通道（如 {"content":"","done":true,"code":10007}），不抛 HTTP 错误。
        """
        own = client is None
        client = client or httpx.AsyncClient(timeout=self.timeout, proxy=self.proxy)
        payload: dict[str, Any] = {"messages": list(messages), "model": model}
        if extra:
            payload.update(extra)
        try:
            async with client.stream(
                "POST", f"{CHAT_BASE}/v3/chat/send", headers=self._chat_headers(), json=payload
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "ignore")
                    if resp.status_code == 401:
                        raise AISeekError("access_token invalid", status=401, body=body)
                    raise AISeekError(f"HTTP {resp.status_code}", status=resp.status_code, body=body)
                async for raw in resp.aiter_lines():
                    line = raw.strip()
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line or line == "[DONE]":
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    code = ev.get("code")
                    if code not in (None, CODE_OK) and not ev.get("content"):
                        raise AISeekError(
                            CODE_LABELS.get(code, f"upstream code {code}"),
                            code=code,
                            body=json.dumps(ev)[:400],
                        )
                    yield ev
                    if ev.get("done"):
                        return
        finally:
            if own:
                await client.aclose()

    async def chat_once(
        self,
        messages: Iterable[dict],
        model: str,
        *,
        extra: dict | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> dict:
        """聚合整段流，返回 {"text": str, "events": int, "elapsed": float}。"""
        t0 = time.perf_counter()
        parts: list[str] = []
        n = 0
        async for ev in self.chat_stream(messages, model, extra=extra, client=client):
            n += 1
            if ev.get("content"):
                parts.append(str(ev["content"]))
        return {"text": "".join(parts), "events": n, "elapsed": time.perf_counter() - t0}


# ---------- 模型目录（离线快照，token 不可用时仍能给出 /v1/models） ----------
MODEL_SNAPSHOT: list[dict] = [
    {"id": "qwen/qwen-coder-32b", "model": "Qwen", "version": "Coder", "paidOnly": False, "supportVision": False, "supportThinking": False},
    {"id": "google/gemini-3-pro", "model": "Gemini", "version": "3.1 Pro", "paidOnly": True, "supportVision": True, "supportThinking": True},
    {"id": "openai/gpt-5-mini", "model": "GPT", "version": "5-mini", "paidOnly": False, "supportVision": True, "supportThinking": False},
    {"id": "deepseek/deepseek-chat", "model": "DeepSeek", "version": "V4-Flash", "paidOnly": False, "supportVision": False, "supportThinking": False},
    {"id": "openai/gpt-5", "model": "GPT", "version": "5.4", "paidOnly": True, "supportVision": True, "supportThinking": False},
    {"id": "anthropic/claude-sonnet-4.5", "model": "Claude", "version": "Sonnet 5", "paidOnly": True, "supportVision": True, "supportThinking": False},
    {"id": "moonshotai/kimi-k2.5", "model": "Kimi", "version": "K2.5", "paidOnly": True, "supportVision": True, "supportThinking": True},
    {"id": "google/gemini-2.0", "model": "Gemini", "version": "3 Flash", "paidOnly": True, "supportVision": True, "supportThinking": False},
    {"id": "deepseek/deepseek-r1", "model": "DeepSeek", "version": "V4-Pro", "paidOnly": True, "supportVision": False, "supportThinking": True},
    {"id": "qwen/qwen3-32b", "model": "Qwen", "version": "3.7 Plus", "paidOnly": False, "supportVision": False, "supportThinking": True},
    {"id": "grok/grok-4", "model": "Grok", "version": "4.6", "paidOnly": True, "supportVision": True, "supportThinking": False},
    {"id": "google/gemini-2.5-flash-lite", "model": "Gemini", "version": "3.1 Flash Lite", "paidOnly": False, "supportVision": True, "supportThinking": False},
    {"id": "relationship/luna", "model": "Luna", "version": "Junior Coach", "paidOnly": False, "supportVision": True, "supportThinking": False},
    {"id": "relationship/john", "model": "John", "version": "Senior Coach", "paidOnly": True, "supportVision": True, "supportThinking": False},
    {"id": "relationship/linda", "model": "Linda", "version": "Guru Coach", "paidOnly": True, "supportVision": True, "supportThinking": False},
    {"id": "destiny/default", "model": "Destiny", "version": "Daily Reader", "paidOnly": False, "supportVision": True, "supportThinking": False},
    {"id": "destiny/oracle", "model": "Oracle", "version": "Western Reader", "paidOnly": True, "supportVision": True, "supportThinking": False},
    {"id": "destiny/sage", "model": "Sage", "version": "Eastern Reader", "paidOnly": True, "supportVision": True, "supportThinking": False},
    {"id": "storyteller/story_muse", "model": "Story Muse", "version": "Creative Spark", "paidOnly": False, "supportVision": True, "supportThinking": False},
    {"id": "storyteller/novel_crafter", "model": "Novel Crafter", "version": "Rich Prose", "paidOnly": False, "supportVision": True, "supportThinking": False},
    {"id": "storyteller/plot_architect", "model": "Plot Architect", "version": "Story Structure", "paidOnly": True, "supportVision": True, "supportThinking": False},
    {"id": "storyteller/roleplay", "model": "Roleplay", "version": "Interactive Story", "paidOnly": False, "supportVision": True, "supportThinking": False},
    {"id": "wealth/starter", "model": "Wealth Starter", "version": "Money Coach", "paidOnly": False, "supportVision": True, "supportThinking": False},
    {"id": "wealth/strategist", "model": "Wealth Strategist", "version": "Finance Strategist", "paidOnly": True, "supportVision": True, "supportThinking": False},
    {"id": "wealth/architect", "model": "Wealth Architect", "version": "Wealth Planner", "paidOnly": True, "supportVision": True, "supportThinking": False},
    {"id": "tourism/trip_scout", "model": "Trip Scout", "version": "Trip Starter", "paidOnly": False, "supportVision": True, "supportThinking": False},
    {"id": "tourism/route_planner", "model": "Route Planner", "version": "Itinerary Designer", "paidOnly": True, "supportVision": True, "supportThinking": False},
    {"id": "tourism/local_concierge", "model": "Local Concierge", "version": "Travel Concierge", "paidOnly": True, "supportVision": True, "supportThinking": False},
]
