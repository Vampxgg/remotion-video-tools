# -*- coding: utf-8 -*-
"""Gemini Live API WebSocket 中继：SOP 实训实时评估。

工作流
-----
1. 浏览器 WS 连上 ``/api/gemini-live/ws``；
2. 前端发送 ``setup`` 消息时必须携带 ``sop`` 字段（dict / JSON 串 / Markdown），
   并可附加 ``voiceCoach=true|false`` 切换是否允许模型口头点评；
3. 后端用 :class:`services.sop_assessor.SopAssessor` 渲染系统指令与工具声明，
   建立 Live 会话；
4. 摄像头帧 + 麦克风 PCM 由前端 ``realtime_input`` 上传；
5. 模型通过 ``log_step_event`` / ``detect_spoken_keyword`` / ``flag_forbidden_action``
   工具调用上报评估事件；
6. 服务端拦截工具调用 → 状态机校验 → 转译为前端 ``assessment`` 事件 +
   把 correction 文本回灌给模型；同时 append 到 NDJSON 日志；
7. 周期性把 ``[STATUS]`` 状态简报回灌给模型，防止"沉默跳过"；
8. 会话结束（前端 close / WS 断开）时调 ``assessor.finalize()`` 推 ``final_summary``。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse

from services.gemini_live_client import (
    GeminiLiveConfig,
    GeminiLiveSession,
    MockGeminiLiveSession,
)
from services.sop_assessor import (
    SopAssessor,
    SopDocument,
    build_sop_tools,
    build_system_instruction,
    parse_sop,
)
from utils.logger import setup_module_logger
from utils.responses import create_standard_response
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/live/gemini_live.log")

router = APIRouter()


# ---------------------------------------------------------------------------
# Lifespan & HTTP utility endpoints
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan_resources(app):
    logger.info("Gemini Live (SOP) router 就绪")
    yield
    logger.info("Gemini Live (SOP) router 已关闭")


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
            "mode": "sop_assessment",
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
            "mediaResolution": _settings.GEMINI_LIVE_MEDIA_RESOLUTION,
            "voiceCoachDefault": _settings.GEMINI_LIVE_SOP_VOICE_COACH_DEFAULT,
            "statusHeartbeatMs": _settings.GEMINI_LIVE_SOP_STATUS_HEARTBEAT_MS,
            "maxSopPayloadBytes": _settings.GEMINI_LIVE_SOP_MAX_PAYLOAD_BYTES,
            "mockQuery": "mock=true",
        }
    )


@router.post("/gemini-live/sop/validate")
async def validate_sop(payload: Dict[str, Any]):
    """让前端在连接前先校验/规范化 SOP，并拿回步骤列表用于渲染。

    入参 ``{"sop": <dict|str>}``；返回规范化后的步骤数组与统计信息。
    """
    sop_payload = payload.get("sop")
    try:
        sop = parse_sop(sop_payload)
    except Exception as exc:
        return create_standard_response(
            data={"ok": False, "message": str(exc)},
            code=400,
            message=str(exc),
        )
    return create_standard_response(
        data={
            "ok": True,
            "sopName": sop.name,
            "totalScoringPoints": sop.total_scoring_points,
            "stepCount": len(sop.steps),
            "steps": [
                {
                    "id": s.id,
                    "name": s.name,
                    "weight": s.weight,
                    "description": s.description,
                    "scoringCriteria": s.scoring_criteria,
                    "deductionRule": s.deduction_rule,
                    "keywordsRequired": s.keywords_required,
                    "aiRecognitionClues": s.ai_recognition_clues,
                    "forbiddenAction": s.forbidden_action,
                }
                for s in sop.steps
            ],
        }
    )


@router.get(
    "/gemini-live/ws",
    summary="Gemini Live WebSocket endpoint documentation",
    description=(
        "Swagger 占位说明。实际接口为 WebSocket：``ws://<host>/api/gemini-live/ws``。"
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
                    "sop": {
                        "sop_name": "示例 SOP",
                        "total_scoring_points": 10,
                        "steps": [
                            {
                                "id": 1,
                                "step_name": "佩戴防护用具",
                                "description": "进入操作区前必须佩戴绝缘手套与护目镜",
                                "scoring_criteria": "双手戴绝缘手套且戴护目镜",
                                "deduction_rule": "缺一项扣 2 分",
                                "keywords_required": ["环境安全", "确认"],
                                "ai_recognition_clues": "检测双色绝缘手套、护目镜佩戴位置",
                                "forbidden_action": ["未戴手套直接接触线路"],
                                "weight": 5,
                            }
                        ],
                    },
                    "voiceCoach": False,
                },
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
                "manualStatus": {"type": "request_status"},
                "close": {"type": "close"},
            },
            "serverEvents": [
                {"type": "status", "status": "connected | setup_received | session_started"},
                {"type": "sop_ready", "stepCount": 10, "sopName": "..."},
                {"type": "transcription", "source": "input | output", "text": "..."},
                {
                    "type": "assessment",
                    "assessment": {
                        "kind": "step_event | keyword_hit | forbidden_action",
                        "stepId": "1",
                        "eventType": "STEP_START | PROCESSING | STEP_COMPLETE",
                        "snapshot": "<state machine snapshot>",
                    },
                },
                {"type": "audio", "mimeType": "audio/pcm;rate=24000", "data": "<base64 pcm>"},
                {"type": "interrupted"},
                {"type": "final_summary", "summary": "<snapshot + elapsedSec>"},
                {"type": "fatal_error", "message": "..."},
                {"type": "closed"},
            ],
        },
        message="此端点仅用于 Swagger 文档；真实接口是同路径的 WebSocket。",
    )


@router.get("/gemini-live/ui", include_in_schema=False)
async def live_ui():
    return RedirectResponse(url="/frontend/index.html")


# ---------------------------------------------------------------------------
# WebSocket 主入口
# ---------------------------------------------------------------------------


@router.websocket("/gemini-live/ws")
async def gemini_live_ws(
    websocket: WebSocket,
    mock: bool = Query(False, description="启用 Mock 会话，不连 Google"),
):
    await websocket.accept()
    session_id = uuid.uuid4().hex[:12]
    await websocket.send_json(
        {"type": "status", "status": "connected", "sessionId": session_id}
    )

    session: Optional[GeminiLiveSession] = None
    assessor: Optional[SopAssessor] = None
    event_task: Optional[asyncio.Task] = None
    heartbeat_task: Optional[asyncio.Task] = None
    send_lock = asyncio.Lock()

    try:
        # 第一条消息必须是 setup，且携带 sop
        payload = await websocket.receive_json()
        if payload.get("type") != "setup":
            await websocket.send_json(
                {"type": "error", "message": "first message must be type=setup"}
            )
            return

        try:
            assessor = _build_assessor(session_id, payload)
        except Exception as exc:
            logger.warning(f"[{session_id}] SOP 解析失败: {exc}")
            await websocket.send_json({"type": "error", "message": f"SOP 解析失败: {exc}"})
            return

        await websocket.send_json(
            {
                "type": "sop_ready",
                "sessionId": session_id,
                "sopName": assessor.sop.name,
                "stepCount": len(assessor.sop.steps),
                "totalMax": assessor.total_max,
                "snapshot": assessor.snapshot(),
                "logPath": str(assessor.log_path.relative_to(_settings.project_root))
                if assessor.log_path.is_relative_to(_settings.project_root)
                else str(assessor.log_path),
            }
        )

        session_config = _build_session_config(payload, assessor)
        session = (
            MockGeminiLiveSession(session_config) if mock else GeminiLiveSession(session_config)
        )
        await session.start()
        event_task = asyncio.create_task(
            _forward_session_events(session, websocket, send_lock, assessor),
            name=f"gemini-live-forward-{session_id}",
        )
        heartbeat_task = asyncio.create_task(
            _status_heartbeat(session, assessor),
            name=f"gemini-live-heartbeat-{session_id}",
        )
        async with send_lock:
            await websocket.send_json(
                {
                    "type": "status",
                    "status": "setup_received",
                    "sessionId": session_id,
                    "voiceCoach": assessor.voice_coach,
                }
            )

        # 主消息循环
        while True:
            payload = await websocket.receive_json()
            msg_type = payload.get("type")

            if msg_type == "audio":
                raw = _decode_base64_payload(payload, "audio")
                if len(raw) > _settings.GEMINI_LIVE_MAX_AUDIO_BYTES_PER_MESSAGE:
                    async with send_lock:
                        await websocket.send_json(
                            {"type": "error", "message": "audio message too large"}
                        )
                    continue
                await session.send_audio(
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
                await session.send_video(
                    raw,
                    str(payload.get("mimeType") or "image/jpeg"),
                )
            elif msg_type == "audio_stream_end":
                await session.audio_stream_end()
            elif msg_type == "request_status":
                async with send_lock:
                    await websocket.send_json(
                        {"type": "snapshot", "snapshot": assessor.snapshot()}
                    )
                # 同时也戳一下模型自检
                with contextlib.suppress(Exception):
                    await session.send_text(assessor.status_report())
            elif msg_type == "close":
                break
            elif msg_type == "setup":
                async with send_lock:
                    await websocket.send_json(
                        {"type": "error", "message": "session already initialized"}
                    )
            else:
                async with send_lock:
                    await websocket.send_json(
                        {"type": "error", "message": f"unknown message type: {msg_type}"}
                    )

    except WebSocketDisconnect:
        logger.info(f"[{session_id}] WebSocket client disconnected")
    except Exception as exc:
        logger.error(f"[{session_id}] Gemini Live error: {exc}", exc_info=True)
        with contextlib.suppress(Exception):
            async with send_lock:
                await websocket.send_json({"type": "error", "message": str(exc)})
    finally:
        # 1) 先推 final_summary（如果有 assessor）
        if assessor is not None:
            with contextlib.suppress(Exception):
                summary = assessor.finalize()
                async with send_lock:
                    with contextlib.suppress(Exception):
                        await websocket.send_json(
                            {"type": "final_summary", "summary": summary}
                        )
        # 2) 关 heartbeat & forward
        for task in (heartbeat_task, event_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        # 3) 关 Live session
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()
        with contextlib.suppress(Exception):
            await websocket.close()


# ---------------------------------------------------------------------------
# 事件转发：拦截 tool_call → assessor → assessment 事件 + tool_response + correction
# ---------------------------------------------------------------------------


async def _forward_session_events(
    session: GeminiLiveSession,
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    assessor: SopAssessor,
) -> None:
    async for event in session.events():
        evt_type = event.get("type")
        if evt_type == "tool_call":
            await _handle_tool_call(session, websocket, send_lock, assessor, event)
            continue
        async with send_lock:
            with contextlib.suppress(Exception):
                await websocket.send_json(event)


async def _handle_tool_call(
    session: GeminiLiveSession,
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    assessor: SopAssessor,
    event: Dict[str, Any],
) -> None:
    function_calls: List[Dict[str, Any]] = list(event.get("functionCalls") or [])
    function_responses: List[Dict[str, Any]] = []
    pending_corrections: List[str] = []

    for call in function_calls:
        name = str(call.get("name") or "")
        args = call.get("args") or {}
        call_id = call.get("id")

        outcome = assessor.handle_tool_call(name, args)
        assessment = outcome.get("assessment") or {}
        tool_response = outcome.get("tool_response") or {"result": "ok"}
        correction = outcome.get("correction")

        async with send_lock:
            with contextlib.suppress(Exception):
                await websocket.send_json({"type": "assessment", "assessment": assessment})

        function_responses.append(
            {"id": call_id, "name": name, "response": tool_response}
        )
        if correction:
            pending_corrections.append(correction)

    if function_responses:
        with contextlib.suppress(Exception):
            await session.send_tool_response(function_responses)
    for correction in pending_corrections:
        with contextlib.suppress(Exception):
            await session.send_text(correction)


async def _status_heartbeat(session: GeminiLiveSession, assessor: SopAssessor) -> None:
    interval_ms = int(_settings.GEMINI_LIVE_SOP_STATUS_HEARTBEAT_MS or 0)
    if interval_ms <= 0:
        return
    interval = max(5.0, interval_ms / 1000.0)
    try:
        while True:
            await asyncio.sleep(interval)
            if assessor.current_step_id is None:
                # 已结束
                continue
            with contextlib.suppress(Exception):
                await session.send_text(assessor.status_report())
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _build_assessor(session_id: str, payload: Dict[str, Any]) -> SopAssessor:
    sop_payload = payload.get("sop")
    if not sop_payload:
        raise ValueError("setup payload missing required field: sop")

    max_bytes = int(_settings.GEMINI_LIVE_SOP_MAX_PAYLOAD_BYTES or 0)
    if max_bytes > 0 and isinstance(sop_payload, str):
        if len(sop_payload.encode("utf-8")) > max_bytes:
            raise ValueError(
                f"SOP payload exceeds {max_bytes} bytes; please split or upload via attachment"
            )

    sop = parse_sop(sop_payload)
    voice_coach = bool(
        payload.get("voiceCoach", _settings.GEMINI_LIVE_SOP_VOICE_COACH_DEFAULT)
    )
    log_dir = _settings.project_root / _settings.GEMINI_LIVE_SOP_LOG_SUBDIR
    return SopAssessor(
        sop=sop,
        session_id=session_id,
        log_dir=Path(log_dir),
        voice_coach=voice_coach,
    )


def _build_session_config(
    payload: Dict[str, Any], assessor: SopAssessor
) -> GeminiLiveConfig:
    model_name = str(payload.get("model") or _settings.GEMINI_LIVE_MODEL)
    is_native_audio_model = "native-audio" in model_name.lower()
    # native-audio 模型不支持 text 输出，必须使用 audio。
    if is_native_audio_model:
        modalities = ["audio"]
    elif assessor.voice_coach:
        modalities = _as_list(
            payload.get("responseModalities"),
            default=["audio"],
        )
    else:
        modalities = ["text"]

    system_instruction = build_system_instruction(
        assessor.sop, voice_coach=assessor.voice_coach
    )
    tools = build_sop_tools()

    media_resolution = str(
        payload.get("mediaResolution") or _settings.GEMINI_LIVE_MEDIA_RESOLUTION or ""
    ).strip() or None

    return GeminiLiveConfig(
        project_id=_settings.GCP_PROJECT_ID,
        location=_settings.GCP_LOCATION_ID,
        model=model_name,
        response_modalities=modalities,
        language_code=str(
            payload.get("languageCode") or _settings.GEMINI_LIVE_LANGUAGE_CODE
        ),
        system_instruction=system_instruction,
        enable_transcription=bool(
            payload.get("enableTranscription", _settings.GEMINI_LIVE_ENABLE_TRANSCRIPTION)
        ),
        enable_affective_dialog=bool(
            payload.get(
                "enableAffectiveDialog", _settings.GEMINI_LIVE_ENABLE_AFFECTIVE_DIALOG
            )
        ),
        proactive_audio=bool(
            payload.get("proactiveAudio", _settings.GEMINI_LIVE_PROACTIVE_AUDIO)
        ),
        context_trigger_tokens=int(
            payload.get(
                "contextTriggerTokens", _settings.GEMINI_LIVE_CONTEXT_TRIGGER_TOKENS
            )
        ),
        context_target_tokens=int(
            payload.get(
                "contextTargetTokens", _settings.GEMINI_LIVE_CONTEXT_TARGET_TOKENS
            )
        ),
        max_audio_bytes_per_message=_settings.GEMINI_LIVE_MAX_AUDIO_BYTES_PER_MESSAGE,
        max_video_bytes_per_message=_settings.GEMINI_LIVE_MAX_VIDEO_BYTES_PER_MESSAGE,
        session_timeout_sec=_settings.GEMINI_LIVE_SESSION_TIMEOUT_SEC,
        tools=tools,
        media_resolution=media_resolution,
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
