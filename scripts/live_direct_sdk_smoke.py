# -*- coding: utf-8 -*-
"""直接验证 GeminiLiveSession 与 Google Gen AI SDK 的真实连接。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from services.gemini_live_client import GeminiLiveConfig, GeminiLiveSession  # noqa: E402
from utils.settings import settings  # noqa: E402


async def run(args) -> int:
    session = GeminiLiveSession(
        GeminiLiveConfig(
            project_id=settings.GCP_PROJECT_ID,
            location=settings.GCP_LOCATION_ID,
            model=args.model or settings.GEMINI_LIVE_MODEL,
            response_modalities=["audio"],
            language_code=settings.GEMINI_LIVE_LANGUAGE_CODE,
            system_instruction="你是一个测试助手，请用一句话回答。",
            enable_transcription=True,
            enable_affective_dialog=not args.disable_affective_dialog,
            session_timeout_sec=args.timeout,
        )
    )
    await session.start()
    try:
        event_iter = session.events()
        for index, text in enumerate(args.text, start=1):
            await session.send_text(text)
            if not await wait_for_turn(event_iter, index, args.timeout):
                return 1
    finally:
        await session.close()
    return 0


async def wait_for_turn(event_iter, turn_index: int, timeout: int) -> bool:
    seen_payload = False
    while True:
        event = await asyncio.wait_for(anext(event_iter), timeout=timeout)
        printable = dict(event)
        if printable.get("type") == "audio" and isinstance(printable.get("data"), str):
            printable["data"] = f"<base64 {len(printable['data'])} chars>"
        printable["turn"] = turn_index
        print(json.dumps(printable, ensure_ascii=True))
        if event.get("type") == "fatal_error":
            return False
        if event.get("type") in {"audio", "transcription"}:
            seen_payload = True
        if event.get("type") == "turn_complete":
            return seen_payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model")
    parser.add_argument(
        "--text",
        action="append",
        default=None,
        help="可重复传入多次以验证多轮对话",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--disable-affective-dialog", action="store_true")
    args = parser.parse_args()
    if not args.text:
        args.text = ["你好，请回复：第一轮连接成功。", "请继续回复：第二轮也成功。"]
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
