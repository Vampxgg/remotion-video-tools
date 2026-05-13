# -*- coding: utf-8 -*-
"""Gemini Live WebSocket 文本 smoke test。

默认使用后端 mock 模式，不消耗 Google 配额。传入 --live 才会连接真实 Live API。
"""

import argparse
import asyncio
import json
import sys
from urllib.parse import urlencode


async def run(args) -> int:
    try:
        import websockets
    except Exception:
        print("缺少 websockets 依赖，请先运行：pip install -r requirements-live.txt")
        return 1

    query = "" if args.live else "?" + urlencode({"mock": "true"})
    url = args.url + query
    print(f"connecting: {url}")

    async with websockets.connect(url) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "setup",
                    "responseModalities": ["audio"],
                    "enableTranscription": True,
                    "systemInstruction": "你是一个测试助手，请用一句话回答。",
                },
                ensure_ascii=False,
            )
        )
        await ws.send(json.dumps({"type": "text", "text": args.text}, ensure_ascii=False))

        seen_text = False
        for _ in range(args.max_events):
            raw = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
            event = json.loads(raw)
            print(json.dumps(event, ensure_ascii=True))
            if event.get("type") == "text":
                seen_text = True
            if event.get("type") == "fatal_error":
                return 1
            if event.get("type") == "turn_complete":
                return 0 if seen_text or args.live else 1
        print("未等到 turn_complete")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:2906/api/gemini-live/ws")
    parser.add_argument("--text", default="你好，请简单介绍一下 Gemini Live API。")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-events", type=int, default=30)
    parser.add_argument("--live", action="store_true", help="连接真实 Google Live API")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
