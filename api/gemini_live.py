# -*- coding: utf-8 -*-
"""Gemini Live API WebSocket 中继。"""

import asyncio
import base64
import contextlib
import importlib.util
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse

from services.gemini_live_client import (
    GeminiLiveConfig,
    GeminiLiveSession,
    MockGeminiLiveSession,
)
from utils.logger import setup_module_logger
from utils.responses import create_standard_response
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/live/gemini_live.log")

router = APIRouter()


@asynccontextmanager
async def lifespan_resources(app):
    logger.info("Gemini Live router 就绪")
    yield
    logger.info("Gemini Live router 已关闭")


@router.get("/gemini-live/health")
async def health():
    sdk_installed = importlib.util.find_spec("google.genai") is not None
    return create_standard_response(
        data={
            "status": "ok",
            "sdkInstalled": sdk_installed,
            "projectId": _settings.GCP_PROJECT_ID,
            "location": _settings.GCP_LOCATION_ID,
            "model": _settings.GEMINI_LIVE_MODEL,
        }
    )


@router.get("/gemini-live/config")
async def config():
    return create_standard_response(
        data={
            "wsPath": "/api/gemini-live/ws",
            "model": _settings.GEMINI_LIVE_MODEL,
            "location": _settings.GCP_LOCATION_ID,
            "languageCode": _settings.GEMINI_LIVE_LANGUAGE_CODE,
            "responseModalities": _settings.GEMINI_LIVE_RESPONSE_MODALITIES,
            "enableTranscription": _settings.GEMINI_LIVE_ENABLE_TRANSCRIPTION,
            "enableAffectiveDialog": _settings.GEMINI_LIVE_ENABLE_AFFECTIVE_DIALOG,
            "proactiveAudio": _settings.GEMINI_LIVE_PROACTIVE_AUDIO,
            "maxAudioBytesPerMessage": _settings.GEMINI_LIVE_MAX_AUDIO_BYTES_PER_MESSAGE,
            "maxVideoBytesPerMessage": _settings.GEMINI_LIVE_MAX_VIDEO_BYTES_PER_MESSAGE,
            "mockQuery": "mock=true",
        }
    )


@router.get(
    "/gemini-live/ws",
    summary="Gemini Live WebSocket endpoint documentation",
    description=(
        "这是 WebSocket 接口的 Swagger 占位说明。Swagger/OpenAPI 不会原生展示 "
        "`@router.websocket` 路由；真实连接请使用 `ws://<host>/api/gemini-live/ws`，"
        "本 HTTP GET 端点仅用于在 Swagger UI 中展示协议和消息格式。"
    ),
)
async def websocket_docs():
    return create_standard_response(
        data={
            "websocketUrl": "/api/gemini-live/ws",
            "mockWebsocketUrl": "/api/gemini-live/ws?mock=true",
            "ui": "/frontend/index.html",
            "clientMessages": {
                "setup": {
                    "type": "setup",
                    "model": _settings.GEMINI_LIVE_MODEL,
                    "languageCode": _settings.GEMINI_LIVE_LANGUAGE_CODE,
                    "systemInstruction": _settings.GEMINI_LIVE_DEFAULT_SYSTEM_INSTRUCTION,
                    "responseModalities": _settings.GEMINI_LIVE_RESPONSE_MODALITIES,
                    "enableTranscription": _settings.GEMINI_LIVE_ENABLE_TRANSCRIPTION,
                    "enableAffectiveDialog": _settings.GEMINI_LIVE_ENABLE_AFFECTIVE_DIALOG,
                    "proactiveAudio": _settings.GEMINI_LIVE_PROACTIVE_AUDIO,
                },
                "text": {"type": "text", "text": "你好，请介绍一下你自己。"},
                "audio": {
                    "type": "audio",
                    "mimeType": "audio/pcm;rate=16000",
                    "data": "<base64 raw 16-bit PCM>",
                },
                "video": {
                    "type": "video",
                    "mimeType": "image/jpeg",
                    "data": "<base64 jpeg frame>",
                },
                "audioStreamEnd": {"type": "audio_stream_end"},
                "close": {"type": "close"},
            },
            "serverEvents": [
                {"type": "status", "status": "connected | setup_received | session_started"},
                {"type": "audio", "mimeType": "audio/pcm;rate=24000", "data": "<base64 pcm>"},
                {"type": "transcription", "source": "input | output", "text": "..."},
                {"type": "interrupted"},
                {"type": "turn_complete"},
                {"type": "fatal_error", "message": "..."},
                {"type": "closed"},
            ],
        },
        message="此端点仅用于 Swagger 文档展示；真实接口是同路径的 WebSocket。",
    )


@router.get("/gemini-live/ui", include_in_schema=False)
async def live_ui():
    return RedirectResponse(url="/frontend/index.html")


@router.websocket("/gemini-live/ws")
async def gemini_live_ws(websocket: WebSocket, mock: bool = Query(False)):
    await websocket.accept()
    await websocket.send_json({"type": "status", "status": "connected"})

    session: Optional[GeminiLiveSession] = None
    event_task: Optional[asyncio.Task] = None
    send_lock = asyncio.Lock()

    async def ensure_session(setup_payload: Optional[Dict[str, Any]] = None) -> GeminiLiveSession:
        nonlocal session, event_task
        if session is not None:
            return session

        session_config = _build_session_config(setup_payload or {})
        session = MockGeminiLiveSession(session_config) if mock else GeminiLiveSession(session_config)
        await session.start()
        event_task = asyncio.create_task(
            _forward_session_events(session, websocket, send_lock),
            name="gemini-live-forward-events",
        )
        return session

    try:
        while True:
            payload = await websocket.receive_json()
            msg_type = payload.get("type")

            if msg_type == "setup":
                await ensure_session(payload)
                async with send_lock:
                    await websocket.send_json({"type": "status", "status": "setup_received"})
                continue

            current_session = await ensure_session(None)
            if msg_type == "text":
                text = str(payload.get("text", "")).strip()
                if text:
                    await current_session.send_text(text)
            elif msg_type == "audio":
                raw = _decode_base64_payload(payload, "audio")
                if len(raw) > _settings.GEMINI_LIVE_MAX_AUDIO_BYTES_PER_MESSAGE:
                    async with send_lock:
                        await websocket.send_json(
                            {"type": "error", "message": "audio message too large"}
                        )
                    continue
                await current_session.send_audio(
                    raw,
                    str(payload.get("mimeType") or "audio/pcm;rate=16000"),
                )
            elif msg_type == "video":
                raw = _decode_base64_payload(payload, "video")
                if len(raw) > _settings.GEMINI_LIVE_MAX_VIDEO_BYTES_PER_MESSAGE:
                    async with send_lock:
                        await websocket.send_json(
                            {"type": "error", "message": "video message too large"}
                        )
                    continue
                await current_session.send_video(
                    raw,
                    str(payload.get("mimeType") or "image/jpeg"),
                )
            elif msg_type == "audio_stream_end":
                await current_session.audio_stream_end()
            elif msg_type == "close":
                break
            else:
                async with send_lock:
                    await websocket.send_json(
                        {"type": "error", "message": f"unknown message type: {msg_type}"}
                    )
    except WebSocketDisconnect:
        logger.info("Gemini Live WebSocket client disconnected")
    except Exception as exc:
        logger.error(f"Gemini Live WebSocket error: {exc}", exc_info=True)
        with contextlib.suppress(Exception):
            async with send_lock:
                await websocket.send_json({"type": "error", "message": str(exc)})
    finally:
        if session:
            await session.close()
        if event_task:
            event_task.cancel()


async def _forward_session_events(
    session: GeminiLiveSession,
    websocket: WebSocket,
    send_lock: asyncio.Lock,
) -> None:
    async for event in session.events():
        async with send_lock:
            await websocket.send_json(event)


def _build_session_config(payload: Dict[str, Any]) -> GeminiLiveConfig:
    modalities = _as_list(
        payload.get("responseModalities"),
        default=_settings.GEMINI_LIVE_RESPONSE_MODALITIES,
    )
    system_instruction = str(
        payload.get("systemInstruction")
        or _settings.GEMINI_LIVE_DEFAULT_SYSTEM_INSTRUCTION
    )
    return GeminiLiveConfig(
        project_id=_settings.GCP_PROJECT_ID,
        location=_settings.GCP_LOCATION_ID,
        model=str(payload.get("model") or _settings.GEMINI_LIVE_MODEL),
        response_modalities=modalities,
        language_code=str(payload.get("languageCode") or _settings.GEMINI_LIVE_LANGUAGE_CODE),
        system_instruction=system_instruction,
        enable_transcription=bool(
            payload.get("enableTranscription", _settings.GEMINI_LIVE_ENABLE_TRANSCRIPTION)
        ),
        enable_affective_dialog=bool(
            payload.get("enableAffectiveDialog", _settings.GEMINI_LIVE_ENABLE_AFFECTIVE_DIALOG)
        ),
        proactive_audio=bool(payload.get("proactiveAudio", _settings.GEMINI_LIVE_PROACTIVE_AUDIO)),
        context_trigger_tokens=int(
            payload.get(
                "contextTriggerTokens",
                _settings.GEMINI_LIVE_CONTEXT_TRIGGER_TOKENS,
            )
        ),
        context_target_tokens=int(
            payload.get("contextTargetTokens", _settings.GEMINI_LIVE_CONTEXT_TARGET_TOKENS)
        ),
        max_audio_bytes_per_message=_settings.GEMINI_LIVE_MAX_AUDIO_BYTES_PER_MESSAGE,
        max_video_bytes_per_message=_settings.GEMINI_LIVE_MAX_VIDEO_BYTES_PER_MESSAGE,
    )


def _as_list(value: Any, default: List[str]) -> List[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return list(default)


def _decode_base64_payload(payload: Dict[str, Any], label: str) -> bytes:
    data = payload.get("data")
    if not isinstance(data, str) or not data:
        raise ValueError(f"missing {label} data")
    try:
        return base64.b64decode(data, validate=True)
    except Exception as exc:
        raise ValueError(f"invalid {label} base64 data") from exc

