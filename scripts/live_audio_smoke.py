# -*- coding: utf-8 -*-
"""Gemini Live WebSocket 音频 smoke test。

默认使用 mock 模式验证协议和大小限制；传入 --live 才会发往真实 Live API。
"""

import argparse
import asyncio
import base64
import json
import math
import struct
from pathlib import Path
from urllib.parse import urlencode


def generate_tone_pcm(duration_sec: float = 0.5, rate: int = 16000) -> bytes:
    frames = int(duration_sec * rate)
    chunks = []
    for i in range(frames):
        sample = int(math.sin(2 * math.pi * 440 * i / rate) * 0.2 * 32767)
        chunks.append(struct.pack("<h", sample))
    return b"".join(chunks)


async def run(args) -> int:
    try:
        import websockets
    except Exception:
        print("缺少 websockets 依赖，请先运行：pip install -r requirements-live.txt")
        return 1

    pcm = Path(args.pcm_file).read_bytes() if args.pcm_file else generate_tone_pcm()
    query = "" if args.live else "?" + urlencode({"mock": "true"})
    url = args.url + query
    print(f"connecting: {url}")

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "setup", "enableTranscription": True}))
        for offset in range(0, len(pcm), args.chunk_size):
            chunk = pcm[offset : offset + args.chunk_size]
            await ws.send(
                json.dumps(
                    {
                        "type": "audio",
                        "mimeType": "audio/pcm;rate=16000",
                        "data": base64.b64encode(chunk).decode("ascii"),
                    }
                )
            )
            await asyncio.sleep(args.chunk_delay)
        await ws.send(json.dumps({"type": "audio_stream_end"}))

        seen_audio_event = False
        for _ in range(args.max_events):
            raw = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
            event = json.loads(raw)
            print(json.dumps(event, ensure_ascii=True))
            if event.get("type") in {"audio", "transcription", "interrupted", "turn_complete"}:
                seen_audio_event = True
            if event.get("type") == "fatal_error":
                return 1
            if seen_audio_event and not args.live:
                return 0
        return 0 if seen_audio_event else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:2906/api/gemini-live/ws")
    parser.add_argument("--pcm-file", help="16kHz little-endian PCM 文件路径")
    parser.add_argument("--chunk-size", type=int, default=3200)
    parser.add_argument("--chunk-delay", type=float, default=0.05)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-events", type=int, default=30)
    parser.add_argument("--live", action="store_true", help="连接真实 Google Live API")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
