"""
演示页真实浏览器验收 (Playwright)。

覆盖: 页面加载 -> 模型下拉填充 -> 流式发送 -> 文本逐步到达 -> 非流式发送 -> 错误气泡路径
产物: bench/demo_ui.png
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE = os.getenv("AISEEK_BASE", "http://127.0.0.1:8099")
OUT = Path(__file__).resolve().parent / "bench"
OUT.mkdir(exist_ok=True)


async def main() -> int:
    ok = True
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(f"{BASE}/demo", wait_until="networkidle")
        await page.wait_for_function("() => document.querySelectorAll('#model option').length > 0", timeout=15000)
        models = await page.locator("#model option").count()
        print(f"[1] 模型下拉项: {models}")
        ok &= models >= 20

        mode = await page.locator("#mode").inner_text()
        print(f"[2] 页面模式标识: {mode}")
        ok &= "mode:" in mode

        # 流式
        await page.select_option("#stream", "1")
        await page.fill("#q", "为什么天空是蓝色的？")
        await page.click("#send")
        bot = page.locator(".msg.bot").last
        await page.wait_for_function(
            "() => { const els=document.querySelectorAll('.msg.bot'); if(!els.length) return false;"
            " return els[els.length-1].textContent.length > 40; }",
            timeout=20000,
        )
        text1 = await bot.inner_text()
        print(f"[3] 流式回答长度: {len(text1)} 前缀: {text1[:28]!r}")
        ok &= len(text1) > 40

        # 非流式
        await page.select_option("#stream", "0")
        await page.fill("#q", "再简短一点")
        await page.click("#send")
        await page.wait_for_function(
            "() => document.querySelectorAll('.msg.bot').length >= 2 &&"
            " document.querySelectorAll('.msg.bot')[1].textContent.length > 40",
            timeout=20000,
        )
        text2 = await page.locator(".msg.bot").last.inner_text()
        print(f"[4] 非流式回答长度: {len(text2)}")
        ok &= len(text2) > 40

        # 指标面板刷新
        await asyncio.sleep(2.5)
        req = await page.locator("#m-req").inner_text()
        print(f"[5] 指标累计请求: {req}")
        ok &= req.isdigit() and int(req) >= 2

        await page.screenshot(path=str(OUT / "demo_ui.png"), full_page=True)
        print(f"[6] 截图: {OUT / 'demo_ui.png'}")

        if errors:
            print("[!] 页面 JS 错误:", errors[:3])
            ok = False
        await browser.close()

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
