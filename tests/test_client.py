"""单元测试：SSE 解析与业务码映射（httpx MockTransport，无网络）。"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from aiseek_client import CODE_PRO_REQUIRED, Account, AISeekClient, AISeekError


def make_client(handler) -> tuple[AISeekClient, httpx.AsyncClient]:
    acc = Account(secret="s", token="t", uid="AS-TEST")
    cli = AISeekClient(acc)
    transport = httpx.MockTransport(handler)
    return cli, httpx.AsyncClient(transport=transport)


def test_stream_collects_content():
    body = (
        'data: {"content":"你","done":false}\n\n'
        'data: {"content":"好","done":false}\n\n'
        'data: {"content":"","done":true,"code":0}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-App-Id"] == "ai-seek"
        assert request.headers["X-Access-Token"] == "t"
        return httpx.Response(200, text=body, headers={"Content-Type": "text/plain"})

    async def run():
        cli, ac = make_client(handler)
        async with ac:
            return await cli.chat_once([{"role": "user", "content": "hi"}], "deepseek/deepseek-chat", client=ac)

    res = asyncio.run(run())
    assert res["text"] == "你好"
    assert res["events"] == 3


def test_stream_raises_on_pro_required():
    body = 'data: {"content":"","done":true,"code":10007}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    async def run():
        cli, ac = make_client(handler)
        async with ac:
            await cli.chat_once([{"role": "user", "content": "hi"}], "deepseek/deepseek-chat", client=ac)

    with pytest.raises(AISeekError) as ei:
        asyncio.run(run())
    assert ei.value.code == CODE_PRO_REQUIRED
    assert ei.value.label == "PRO_REQUIRED"


def test_stream_raises_on_401():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="access_token invalid")

    async def run():
        cli, ac = make_client(handler)
        async with ac:
            await cli.chat_once([{"role": "user", "content": "hi"}], "deepseek/deepseek-chat", client=ac)

    with pytest.raises(AISeekError) as ei:
        asyncio.run(run())
    assert ei.value.status == 401


def test_register_parses_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-APP-ID"] == "ai-seek"
        assert json.loads(request.content)["secret"] == "s"
        return httpx.Response(200, json={"data": {"uid": "AS-1", "uidInt": 7, "token": "jwt"}})

    acc = Account(secret="s")
    cli = AISeekClient(acc)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as ac:
            return await cli.register(ac)

    got = asyncio.run(run())
    assert (got.uid, got.uid_int, got.token) == ("AS-1", 7, "jwt")


def test_register_raises_when_no_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "appId not exists"})

    cli = AISeekClient(Account(secret="s"))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as ac:
            await cli.register(ac)

    with pytest.raises(AISeekError):
        asyncio.run(run())
