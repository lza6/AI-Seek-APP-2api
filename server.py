"""
AI Seek → OpenAI 兼容网关 (R1)

模式 (环境变量 AISEEK_MODE):
  mock  默认。本地合成流式响应，用于 UI/压测/链路自测，绝不伪装成真实上游。
  real  真实上游。当前上游对无订阅账号统一返回 code=10007(PRO_REQUIRED)，
        网关会把该错误如实映射为 HTTP 402 并在响应体里带上上游原始码。

启动:
  python -m uvicorn server:app --host 127.0.0.1 --port 8099
  # 或
  python server.py
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from aiseek_client import (
    CODE_PRO_REQUIRED,
    MODEL_SNAPSHOT,
    Account,
    AISeekClient,
    AISeekError,
    new_secret,
)

ROOT = Path(__file__).resolve().parent
ACCOUNTS_FILE = ROOT / "accounts.json"

MODE = os.getenv("AISEEK_MODE", "mock").lower()
CONCURRENCY = int(os.getenv("AISEEK_CONCURRENCY", "8"))
PROXY = os.getenv("AISEEK_PROXY") or None
DEFAULT_MODEL = os.getenv("AISEEK_DEFAULT_MODEL", "deepseek/deepseek-chat")


# --------------------------------------------------------------------------- 账号池
class AccountPool:
    """secret 池。每一个 secret 在服务端幂等映射一个 uid，用尽后按需新建。"""

    def __init__(self, path: Path, size: int = 4):
        self.path = path
        self.size = size
        self._accounts: list[Account] = []
        self._locks: dict[str, asyncio.Lock] = {}
        self._rr = 0
        self._guard = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._accounts = [Account(secret=new_secret()) for _ in range(self.size)]
            self._save()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw.get("accounts") if isinstance(raw, dict) else raw
            self._accounts = [Account.from_dict(i) for i in (items or []) if i.get("secret")]
        except Exception:
            self._accounts = []
        if not self._accounts:
            self._accounts = [Account(secret=new_secret()) for _ in range(self.size)]
            self._save()

    def _save(self) -> None:
        payload = {"appId": "ai-seek", "accounts": [a.as_dict() for a in self._accounts]}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @property
    def accounts(self) -> list[Account]:
        return self._accounts

    def lock_for(self, acc: Account) -> asyncio.Lock:
        return self._locks.setdefault(acc.secret, asyncio.Lock())

    async def pick(self) -> Account:
        """轮询取一个账号。"""
        async with self._guard:
            if not self._accounts:
                self._accounts.append(Account(secret=new_secret()))
            acc = self._accounts[self._rr % len(self._accounts)]
            self._rr += 1
            return acc

    async def ensure_token(self, acc: Account) -> Account:
        """无 token 时注册；注册失败原样抛出。"""
        if acc.token:
            return acc
        async with self.lock_for(acc):
            if acc.token:
                return acc
            cli = AISeekClient(acc, proxy=PROXY)
            await cli.register()
            self._save()
            return acc

    async def refresh(self, acc: Account) -> Account:
        async with self.lock_for(acc):
            acc.token = ""
            cli = AISeekClient(acc, proxy=PROXY)
            await cli.register()
            self._save()
            return acc


POOL: AccountPool


# --------------------------------------------------------------------------- 指标
class Stats:
    def __init__(self) -> None:
        self.started = time.time()
        self.requests = 0
        self.errors = 0
        self.pro_blocked = 0
        self.by_model: dict[str, int] = {}
        self.latency_ms: list[float] = []

    def hit(self, model: str, ms: float, *, error: bool = False, pro: bool = False) -> None:
        self.requests += 1
        self.by_model[model] = self.by_model.get(model, 0) + 1
        self.latency_ms.append(ms)
        if len(self.latency_ms) > 5000:
            self.latency_ms = self.latency_ms[-2000:]
        if error:
            self.errors += 1
        if pro:
            self.pro_blocked += 1

    def snapshot(self) -> dict[str, Any]:
        lat = sorted(self.latency_ms)
        def pct(p: float) -> float:
            if not lat:
                return 0.0
            i = min(len(lat) - 1, int(len(lat) * p))
            return round(lat[i], 1)
        return {
            "mode": MODE,
            "uptimeSec": round(time.time() - self.started, 1),
            "requests": self.requests,
            "errors": self.errors,
            "proBlocked": self.pro_blocked,
            "accounts": len(POOL.accounts),
            "models": len(MODEL_SNAPSHOT),
            "latencyMs": {"p50": pct(0.5), "p90": pct(0.9), "p99": pct(0.99)},
            "byModel": dict(sorted(self.by_model.items(), key=lambda kv: -kv[1])[:10]),
        }


STATS = Stats()
SEM = asyncio.Semaphore(CONCURRENCY)


# --------------------------------------------------------------------------- mock 上游
MOCK_FIRST_TOKEN = float(os.getenv("AISEEK_MOCK_FIRST_TOKEN", "0.18"))
MOCK_TOKEN_INTERVAL = float(os.getenv("AISEEK_MOCK_TOKEN_INTERVAL", "0.02"))


def _mock_answer(model: str, prompt: str) -> list[str]:
    """确定性合成回答：明确标注 mock，不冒充上游内容。"""
    head = f"[mock:{model}] "
    body = (
        f"这是本地 mock 流式响应，未调用上游。收到 {len(prompt)} 字符输入，"
        "首字延迟与分块节奏由环境变量控制（AISEEK_MOCK_FIRST_TOKEN / AISEEK_MOCK_TOKEN_INTERVAL）。"
        "接入真实上游需 AISEEK_MODE=real 且账号具备订阅（当前上游对无订阅账号统一返回 10007）。"
    )
    text = head + body
    return [text[i : i + 8] for i in range(0, len(text), 8)]


# --------------------------------------------------------------------------- 生命周期
@asynccontextmanager
async def lifespan(app: FastAPI):
    global POOL
    POOL = AccountPool(ACCOUNTS_FILE, size=int(os.getenv("AISEEK_POOL_SIZE", "4")))
    yield


app = FastAPI(title="AI Seek 2API", version="1.0.0-r1", lifespan=lifespan)


# --------------------------------------------------------------------------- 路由
@app.get("/")
async def root():
    return RedirectResponse("/demo")


@app.get("/health")
async def health():
    snap = STATS.snapshot()
    return {
        "status": "ok",
        "mode": MODE,
        "concurrency": CONCURRENCY,
        "pool": len(POOL.accounts),
        "models": len(MODEL_SNAPSHOT),
        "stats": snap,
    }


@app.get("/api/stats")
async def api_stats():
    return STATS.snapshot()


@app.get("/v1/models")
async def v1_models():
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": m["id"],
                "object": "model",
                "created": now,
                "owned_by": "ai-seek",
                "meta": {
                    "label": f'{m["model"]} {m["version"]}',
                    "paidOnly": m["paidOnly"],
                    "supportVision": m["supportVision"],
                    "supportThinking": m["supportThinking"],
                },
            }
            for m in MODEL_SNAPSHOT
        ],
    }


@app.get("/native/models")
async def native_models():
    """上游原始模型目录（若可连，则用线上实时数据）。"""
    if MODE == "real":
        try:
            return {"code": 0, "data": {"models": await AISeekClient.list_models(proxy=PROXY)}, "source": "upstream"}
        except Exception as e:  # 网络失败降级到快照
            return {"code": 0, "data": {"models": MODEL_SNAPSHOT}, "source": "snapshot", "error": str(e)[:200]}
    return {"code": 0, "data": {"models": MODEL_SNAPSHOT}, "source": "snapshot"}


def _extract_prompt(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for seg in c:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    parts.append(seg.get("text", ""))
    return "\n".join(p for p in parts if p)


def _oa_chunk(cid: str, model: str, delta: dict, finish: str | None = None) -> bytes:
    return (
        "data: "
        + json.dumps(
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            },
            ensure_ascii=False,
        )
        + "\n\n"
    ).encode()


async def _mock_stream(prompt: str, model: str, cid: str) -> AsyncIterator[bytes]:
    yield _oa_chunk(cid, model, {"role": "assistant", "content": ""})
    await asyncio.sleep(MOCK_FIRST_TOKEN)
    for piece in _mock_answer(model, prompt):
        yield _oa_chunk(cid, model, {"content": piece})
        await asyncio.sleep(MOCK_TOKEN_INTERVAL)
    yield _oa_chunk(cid, model, {}, "stop")
    yield b"data: [DONE]\n\n"


async def _real_stream(prompt: str, model: str, cid: str, messages: list[dict]) -> AsyncIterator[bytes]:
    acc = await POOL.pick()
    await POOL.ensure_token(acc)
    cli = AISeekClient(acc, proxy=PROXY)
    yield _oa_chunk(cid, model, {"role": "assistant", "content": ""})
    try:
        async for ev in cli.chat_stream(messages, model):
            piece = ev.get("content")
            if piece:
                yield _oa_chunk(cid, model, {"content": piece})
        yield _oa_chunk(cid, model, {}, "stop")
        yield b"data: [DONE]\n\n"
    except AISeekError as e:
        # 上游业务错误如实透出，不伪装成成功
        if e.code == CODE_PRO_REQUIRED:
            STATS.hit(model, 0, error=True, pro=True)
        err = {"error": {"message": f"upstream: {e}", "type": e.label, "code": e.code}}
        yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode()
        yield b"data: [DONE]\n\n"


async def _read_json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {e.__class__.__name__}")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return body


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await _read_json(request)
    model = body.get("model") or DEFAULT_MODEL
    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages is required")
    stream = bool(body.get("stream", False))
    prompt = _extract_prompt(messages)
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    t0 = time.perf_counter()

    if not stream:
        if MODE == "mock":
            text = "".join(_mock_answer(model, prompt))
            await asyncio.sleep(MOCK_FIRST_TOKEN + 0.01 * len(text))
            STATS.hit(model, (time.perf_counter() - t0) * 1000)
            return JSONResponse(
                {
                    "id": cid,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": max(1, len(prompt) // 4),
                        "completion_tokens": max(1, len(text) // 4),
                        "total_tokens": max(2, (len(prompt) + len(text)) // 4),
                    },
                    "x_gateway": {"mode": "mock", "upstream": "none", "note": "synthetic response, upstream not called"},
                }
            )
        acc = await POOL.pick()
        await POOL.ensure_token(acc)
        cli = AISeekClient(acc, proxy=PROXY)
        try:
            res = await cli.chat_once(messages, model)
        except AISeekError as e:
            STATS.hit(model, (time.perf_counter() - t0) * 1000, error=True, pro=(e.code == CODE_PRO_REQUIRED))
            status = 402 if e.code == CODE_PRO_REQUIRED else 502
            return JSONResponse(
                {
                    "error": {
                        "message": f"upstream: {e}",
                        "type": e.label,
                        "code": e.code,
                        "upstreamBody": e.body,
                    }
                },
                status_code=status,
            )
        STATS.hit(model, (time.perf_counter() - t0) * 1000)
        return JSONResponse(
            {
                "id": cid,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": res["text"]}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "x_gateway": {"mode": "real", "accounts": len(POOL.accounts), "elapsed": round(res["elapsed"], 3)},
            }
        )

    gen = _mock_stream(prompt, model, cid) if MODE == "mock" else _real_stream(prompt, model, cid, messages)
    STATS.hit(model, 0)
    return StreamingResponse(gen, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Mode": MODE})


@app.post("/v1/chat/send")
async def native_chat_send(request: Request):
    """上游原生协议透传：body/messages/model 原样转发，SSE 原样回吐。"""
    body = await _read_json(request)
    model = body.get("model") or DEFAULT_MODEL
    messages = body.get("messages") or []
    extra = {k: v for k, v in body.items() if k not in ("messages", "model")}
    if MODE == "mock":
        async def mock_native():
            for piece in _mock_answer(model, _extract_prompt(messages)):
                yield f'data: {json.dumps({"content": piece, "done": False}, ensure_ascii=False)}\n\n'.encode()
                await asyncio.sleep(MOCK_TOKEN_INTERVAL)
            yield f'data: {json.dumps({"content": "", "done": True, "code": 0})}\n\n'.encode()
        return StreamingResponse(mock_native(), media_type="text/event-stream", headers={"X-Mode": "mock"})

    acc = await POOL.pick()
    await POOL.ensure_token(acc)
    cli = AISeekClient(acc, proxy=PROXY)

    async def real_native():
        try:
            async for ev in cli.chat_stream(messages, model, extra=extra):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()
        except AISeekError as e:
            yield f'data: {json.dumps({"content": "", "done": True, "code": e.code, "error": str(e)}, ensure_ascii=False)}\n\n'.encode()

    return StreamingResponse(real_native(), media_type="text/event-stream", headers={"X-Mode": "real"})


@app.get("/demo", response_class=HTMLResponse)
async def demo():
    html = ROOT / "demo.html"
    if not html.exists():
        raise HTTPException(404, "demo.html missing")
    return HTMLResponse(html.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("AISEEK_HOST", "127.0.0.1"), port=int(os.getenv("AISEEK_PORT", "8099")))
