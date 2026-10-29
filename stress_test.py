"""
本地压测 / 证据采集脚本。

两种目标:
  1) 网关压测 (默认): 打本地 server.py 的 /health /v1/models /v1/chat/completions
  2) 上游真探 (--upstream): 直接打 AI Seek 线上协议，量化 10007 命中率与延迟

用法:
  python stress_test.py                          # 网关 mock 模式全量压测
  python stress_test.py --levels 4,16,32 --per 40
  python stress_test.py --upstream --accounts 8  # 上游证据采集(注册+对话)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
import urllib.parse
from pathlib import Path

import aiohttp

OUT = Path(__file__).resolve().parent / "bench"
OUT.mkdir(exist_ok=True)


def pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, int(len(s) * p))
    return round(s[i], 2)


def summarize(name: str, ok: int, total: int, lat: list[float], extra: dict | None = None) -> dict:
    row = {
        "case": name,
        "total": total,
        "ok": ok,
        "fail": total - ok,
        "successRate": round(ok / total * 100, 2) if total else 0.0,
        "p50_ms": pct(lat, 0.5),
        "p90_ms": pct(lat, 0.9),
        "p99_ms": pct(lat, 0.99),
        "avg_ms": round(statistics.fmean(lat), 2) if lat else 0.0,
    }
    if extra:
        row.update(extra)
    return row


# --------------------------------------------------------------------------- 网关压测
async def one_http(session: aiohttp.ClientSession, method: str, url: str, payload=None, want_stream=False):
    t0 = time.perf_counter()
    async with session.request(method, url, json=payload) as r:
        if want_stream:
            body = b""
            async for chunk in r.content.iter_any():
                body += chunk
            ok = r.status == 200 and b"data:" in body
            text = body[:200].decode("utf-8", "ignore")
        else:
            text = await r.text()
            ok = r.status == 200
    return ok, (time.perf_counter() - t0) * 1000, text


async def benchmark_gateway(base: str, levels: list[int], per_level: int, model: str) -> list[dict]:
    rows: list[dict] = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120), trust_env=True) as s:
        # 1. 基线端点
        for name, method, path, payload, stream in [
            ("health", "GET", "/health", None, False),
            ("models", "GET", "/v1/models", None, False),
        ]:
            ok = 0
            lat: list[float] = []
            for _ in range(per_level):
                o, ms, _ = await one_http(s, method, base + path, payload)
                ok += 1 if o else 0
                lat.append(ms)
            rows.append(summarize(f"{name} (serial x{per_level})", ok, per_level, lat))

        # 2. 错误路径
        bad = {"model": model, "messages": []}
        ok = 0
        lat = []
        for _ in range(min(per_level, 20)):
            t0 = time.perf_counter()
            async with s.post(base + "/v1/chat/completions", json=bad) as r:
                _ = await r.text()
                ok += 1 if r.status == 400 else 0
            lat.append((time.perf_counter() - t0) * 1000)
        rows.append(summarize("chat empty-messages -> 400", ok, min(per_level, 20), lat))

        # 3. 并发档位
        for lvl in levels:
            for kind, stream in [("chat nonstream", False), ("chat stream", True)]:
                payload = {"model": model, "messages": [{"role": "user", "content": "压测: 用一句话说明你是谁"}], "stream": stream}
                ok = 0
                lat = []
                t_start = time.perf_counter()

                async def worker():
                    nonlocal ok
                    o, ms, _ = await one_http(s, "POST", base + "/v1/chat/completions", payload, want_stream=stream)
                    lat.append(ms)
                    if o:
                        ok += 1

                await asyncio.gather(*(worker() for _ in range(lvl)))
                wall = time.perf_counter() - t_start
                rows.append(
                    summarize(
                        f"{kind} (concurrency {lvl})",
                        ok,
                        lvl,
                        lat,
                        {"wall_s": round(wall, 2), "rps": round(lvl / wall, 1) if wall else 0},
                    )
                )
    return rows


# --------------------------------------------------------------------------- 上游真探
APP_ID = "ai-seek"
AUTH = "https://saas.castbox.fm/auth/api/v1/tokens/provider/secret"
CHAT = "https://ai-seek.thebetter.ai/v3/chat/send"


async def upstream_probe(accounts: int, model: str) -> dict:
    """注册 N 个账号，每个发 1 条对话，统计上游业务码分布。"""
    codes: dict[str, int] = {}
    reg_lat: list[float] = []
    chat_lat: list[float] = []
    detail: list[dict] = []

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120), trust_env=True) as s:
        sem = asyncio.Semaphore(4)

        async def one(i: int):
            async with sem:
                secret = str(uuid.uuid4())
                t0 = time.perf_counter()
                async with s.post(AUTH, json={"appSource": "APP", "secret": secret},
                                  headers={"Content-Type": "application/json", "X-APP-ID": APP_ID}) as r:
                    j = await r.json()
                reg_lat.append((time.perf_counter() - t0) * 1000)
                d = (j or {}).get("data") or {}
                tok, uid = d.get("token"), d.get("uid")
                if not tok:
                    codes["REGISTER_FAIL"] = codes.get("REGISTER_FAIL", 0) + 1
                    detail.append({"i": i, "error": str(j)[:160]})
                    return
                t1 = time.perf_counter()
                async with s.post(CHAT, json={"messages": [{"role": "user", "content": "hi"}], "model": model},
                                  headers={"Content-Type": "application/json", "X-App-Id": APP_ID, "X-Access-Token": tok}) as r:
                    body = await r.text()
                chat_lat.append((time.perf_counter() - t1) * 1000)
                code = "UNKNOWN"
                content = ""
                for line in body.splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        try:
                            ev = json.loads(line[5:].strip())
                        except Exception:
                            continue
                        code = str(ev.get("code", "no-code"))
                        content = ev.get("content") or ""
                codes[code] = codes.get(code, 0) + 1
                detail.append({"i": i, "uid": uid, "code": code, "content": content[:40], "http": r.status})

        await asyncio.gather(*(one(i) for i in range(accounts)))

    return {
        "accounts": accounts,
        "model": model,
        "businessCodeDistribution": codes,
        "registerLatency_ms": {"p50": pct(reg_lat, 0.5), "p99": pct(reg_lat, 0.99)},
        "chatLatency_ms": {"p50": pct(chat_lat, 0.5), "p99": pct(chat_lat, 0.99)},
        "detail": detail,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.getenv("AISEEK_BASE", "http://127.0.0.1:8099"))
    ap.add_argument("--levels", default="4,16,32")
    ap.add_argument("--per", type=int, default=30)
    ap.add_argument("--model", default="deepseek/deepseek-chat")
    ap.add_argument("--upstream", action="store_true")
    ap.add_argument("--accounts", type=int, default=6)
    args = ap.parse_args()

    if args.upstream:
        res = asyncio.run(upstream_probe(args.accounts, args.model))
        path = OUT / f"upstream_probe_{int(time.time())}.json"
        path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(res, ensure_ascii=False, indent=2)[:3000])
        print(f"\n[saved] {path}")
        return 0

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    try:
        rows = asyncio.run(benchmark_gateway(args.base, levels, args.per, args.model))
    except aiohttp.ClientConnectorError:
        print(f"[FATAL] 网关未启动: {args.base} —— 先运行 python server.py", file=sys.stderr)
        return 2

    print(f"\n=== 网关压测 {args.base} (model={args.model}) ===")
    print(f"{'case':34}{'ok/total':>12}{'succ%':>8}{'p50':>9}{'p90':>9}{'p99':>9}{'rps':>8}")
    for r in rows:
        print(
            f"{r['case']:34}{r['ok']:>6}/{r['total']:<5}{r['successRate']:>8}"
            f"{r['p50_ms']:>9}{r['p90_ms']:>9}{r['p99_ms']:>9}{r.get('rps', '-'):>8}"
        )
    path = OUT / f"gateway_{int(time.time())}.json"
    path.write_text(json.dumps({"base": args.base, "model": args.model, "rows": rows}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\n[saved] {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
