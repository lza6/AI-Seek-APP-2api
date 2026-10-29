# 实测结果（R1）

环境: Windows 10 Pro / Python 3.14 / 本机代理隧道出网。
所有数字均为本轮真实运行产物（`bench/*.json` 为原始记录，已从版本库排除）。

## 1. 网关压测（mock 模式，本地合成流）

命令: `python stress_test.py --levels 4,16,32 --per 30`

| case | ok/total | succ% | p50 | p90 | p99 | rps |
|---|---|---|---|---|---|---|
| health (serial x30) | 30/30 | 100.0 | 1.13ms | 1.42ms | 2.27ms | - |
| models (serial x30) | 30/30 | 100.0 | 1.86ms | 2.15ms | 2.31ms | - |
| chat empty-messages → 400 | 20/20 | 100.0 | 1.42ms | 1.85ms | 2.30ms | - |
| chat nonstream (concurrency 4) | 4/4 | 100.0 | 2063.88ms | 2063.93ms | 2063.93ms | 1.9 |
| chat stream (concurrency 4) | 4/4 | 100.0 | 929.88ms | 930.05ms | 930.05ms | 4.3 |
| chat nonstream (concurrency 16) | 16/16 | 100.0 | 2070.17ms | 2081.51ms | 2081.74ms | 7.7 |
| chat stream (concurrency 16) | 16/16 | 100.0 | 936.40ms | 936.91ms | 937.08ms | 17.1 |
| chat nonstream (concurrency 32) | 32/32 | 100.0 | 2075.24ms | 2076.73ms | 2077.37ms | 15.4 |
| chat stream (concurrency 32) | 32/32 | 100.0 | 947.04ms | 947.57ms | 948.05ms | 33.6 |

说明: mock 的 p50 由 `AISEEK_MOCK_FIRST_TOKEN=0.18s` + 分块间隔决定（非真实模型延迟）。并发 32 时 p99 与 p50 几乎重合 ⇒ 网关层无排队/抖动，`asyncio` 路径无锁竞争。

## 2. 上游真探（证据采集）

命令: `python stress_test.py --upstream --accounts 8`

```json
{
  "accounts": 8,
  "model": "deepseek/deepseek-chat",
  "businessCodeDistribution": { "10007": 8 },
  "registerLatency_ms": { "p50": 2173.95, "p99": 2557.55 },
  "chatLatency_ms":     { "p50": 2032.14, "p99": 3547.84 }
}
```

* 注册 8/8 成功（uid 全部签发），对话 8/8 返回 `code=10007`。
* 延迟含本机代理隧道（curl 侧可见 `200 Connection established`），非上游裸延迟。

## 3. 真实上游模式网关（`AISEEK_MODE=real`）

| 场景 | 结果 |
|---|---|
| 非流式 | `HTTP 402` + `{"error":{"type":"PRO_REQUIRED","code":10007,"upstreamBody":"{\"content\": \"\", \"done\": true, \"code\": 10007}"}}` |
| 流式 | `200 text/event-stream`：role 帧 → `{"error":{"type":"PRO_REQUIRED","code":10007}}` → `[DONE]` |

⇒ 网关不会用合成内容掩盖上游失败。

## 4. 浏览器端到端（Playwright）

命令: `python verify_ui.py`

```
[1] 模型下拉项: 28
[2] 页面模式标识: mode: mock
[3] 流式回答长度: 48
[4] 非流式回答长度: 184
[5] 指标累计请求: 108
[6] 截图: bench/demo_ui.png
RESULT: PASS
```

截图入库: `bench/demo_ui.png`（仅截图，不含原始请求体）。

## 5. 单元测试

命令: `python -c "import pytest,sys; sys.exit(pytest.main(['tests','-q']))"`

```
.....                                                                    [100%]
5 passed in 0.44s
```

覆盖: SSE 增量聚合、`code=10007` → `AISeekError`、401 传播、注册解析、注册失败抛出。全部走 `httpx.MockTransport`，零网络。
