# -*- coding: utf-8 -*-
"""Gemini Live 后端 mock 协议测试。

使用进程内 FastAPI TestClient 挂载新增 router，不依赖整站启动，也不会访问 Google
或消耗配额。
"""

import argparse
import base64
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api import gemini_live  # noqa: E402


def run(args) -> int:
    app = FastAPI()
    app.include_router(gemini_live.router, prefix="/api")

    with TestClient(app) as client:
        with client.websocket_connect("/api/gemini-live/ws?mock=true") as ws:
            ws.send_json({"type": "setup", "responseModalities": ["audio", "text"]})
            ws.send_json({"type": "text", "text": "ping"})
            ws.send_json(
                {
                    "type": "audio",
                    "mimeType": "audio/pcm;rate=16000",
                    "data": base64.b64encode(b"\x00\x00" * 160).decode("ascii"),
                }
            )
            ws.send_json({"type": "audio_stream_end"})

            types = set()
            for _ in range(args.max_events):
                event = ws.receive_json()
                print(json.dumps(event, ensure_ascii=False))
                types.add(event.get("type"))
                if {"status", "text", "transcription", "interrupted", "turn_complete"} <= types:
                    return 0
                if event.get("type") == "fatal_error":
                    return 1
    print(f"事件不完整: {sorted(types)}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-events", type=int, default=20)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
