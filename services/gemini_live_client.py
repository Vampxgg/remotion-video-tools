# -*- coding: utf-8 -*-
"""
Gemini Live API 会话封装。

路由层只处理浏览器 WebSocket 协议，本文件负责把统一的输入事件转成
Google Gen AI SDK 的 Live 会话调用，并把 SDK 输出规整为前端可消费的事件。
"""

import asyncio
import base64
import contextlib
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

from utils.gcp_credentials import get_gcp_credentials


@dataclass(slots=True)
class GeminiLiveConfig:
    project_id: str
    location: str
    model: str
    response_modalities: List[str]
    language_code: str
    system_instruction: str
    enable_transcription: bool = True
    enable_affective_dialog: bool = True
    proactive_audio: bool = False
    context_trigger_tokens: int = 10000
    context_target_tokens: int = 2048
    max_audio_bytes_per_message: int = 64 * 1024
    max_video_bytes_per_message: int = 512 * 1024
    session_timeout_sec: int = 600
    # 形如 [{"function_declarations": [{...}, ...]}]，Live API 要求是列表
    tools: Optional[List[Dict[str, Any]]] = None
    # "low" | "medium" | "high"；None 表示不在配置里显式设置
    media_resolution: Optional[str] = None
    extra_config: Dict[str, Any] = field(default_factory=dict)


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _part_text(part: Any) -> Optional[str]:
    text = _get(part, "text")
    return text if isinstance(text, str) and text else None


def _inline_data(part: Any) -> Optional[Dict[str, str]]:
    inline = _get(part, "inline_data", "inlineData")
    if not inline:
        return None

    data = _get(inline, "data")
    mime_type = _get(inline, "mime_type", "mimeType", default="audio/pcm;rate=24000")
    if data is None:
        return None
    if isinstance(data, bytes):
        encoded = base64.b64encode(data).decode("ascii")
    elif isinstance(data, str):
        encoded = data
    else:
        return None
    return {"type": "audio", "data": encoded, "mimeType": mime_type}


def _parts_from_model_turn(model_turn: Any) -> List[Any]:
    parts = _get(model_turn, "parts", default=[])
    return list(parts or [])


def _extract_events(message: Any) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []

    direct_text = _get(message, "text")
    if isinstance(direct_text, str) and direct_text:
        events.append({"type": "text", "text": direct_text})

    server_content = _get(message, "server_content", "serverContent")
    if server_content:
        if _get(server_content, "interrupted", default=False):
            events.append({"type": "interrupted"})

        for attr_name, source in (
            ("input_transcription", "input"),
            ("inputTranscription", "input"),
            ("output_transcription", "output"),
            ("outputTranscription", "output"),
        ):
            transcription = _get(server_content, attr_name)
            text = _get(transcription, "text") if transcription else None
            if isinstance(text, str) and text:
                events.append({"type": "transcription", "source": source, "text": text})

        model_turn = _get(server_content, "model_turn", "modelTurn")
        if model_turn:
            for part in _parts_from_model_turn(model_turn):
                text = _part_text(part)
                if text:
                    events.append({"type": "text", "text": text})
                inline_event = _inline_data(part)
                if inline_event:
                    events.append(inline_event)

        if _get(server_content, "turn_complete", "turnComplete", default=False):
            events.append({"type": "turn_complete"})

    tool_call = _get(message, "tool_call", "toolCall")
    if tool_call:
        function_calls = _get(tool_call, "function_calls", "functionCalls", default=[]) or []
        parsed_calls: List[Dict[str, Any]] = []
        for fc in function_calls:
            parsed_calls.append(
                {
                    "id": _get(fc, "id"),
                    "name": _get(fc, "name"),
                    "args": _to_plain(_get(fc, "args", default={})) or {},
                }
            )
        events.append(
            {
                "type": "tool_call",
                "functionCalls": parsed_calls,
                "toolCall": _to_plain(tool_call),
            }
        )

    tool_call_cancellation = _get(message, "tool_call_cancellation", "toolCallCancellation")
    if tool_call_cancellation:
        events.append(
            {
                "type": "tool_call_cancellation",
                "ids": _to_plain(_get(tool_call_cancellation, "ids", default=[])) or [],
            }
        )

    go_away = _get(message, "go_away", "goAway")
    if go_away:
        events.append({"type": "go_away", "goAway": _to_plain(go_away)})

    session_update = _get(message, "session_resumption_update", "sessionResumptionUpdate")
    if session_update:
        events.append(
            {
                "type": "session_resumption_update",
                "sessionResumptionUpdate": _to_plain(session_update),
            }
        )

    return events


def _to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_plain(v) for v in value]
    if hasattr(value, "model_dump"):
        return _to_plain(value.model_dump(exclude_none=True))
    if hasattr(value, "__dict__"):
        return {
            str(k): _to_plain(v)
            for k, v in vars(value).items()
            if not k.startswith("_") and v is not None
        }
    return str(value)


class GeminiLiveSession:
    def __init__(self, config: GeminiLiveConfig):
        self.config = config
        self._input_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._event_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._closed = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="gemini-live-session")

    async def close(self) -> None:
        self._closed.set()
        await self._input_queue.put({"type": "close"})
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def send_text(self, text: str) -> None:
        await self._input_queue.put({"type": "text", "text": text})

    async def send_audio(self, data: bytes, mime_type: str) -> None:
        await self._input_queue.put({"type": "audio", "data": data, "mimeType": mime_type})

    async def send_video(self, data: bytes, mime_type: str) -> None:
        await self._input_queue.put({"type": "video", "data": data, "mimeType": mime_type})

    async def audio_stream_end(self) -> None:
        await self._input_queue.put({"type": "audio_stream_end"})

    async def send_tool_response(self, function_responses: List[Dict[str, Any]]) -> None:
        """提交工具调用结果，function_responses 形如 [{"id","name","response",...}]。"""
        await self._input_queue.put(
            {"type": "tool_response", "functionResponses": list(function_responses or [])}
        )

    async def events(self) -> AsyncIterator[Dict[str, Any]]:
        while not self._closed.is_set():
            event = await self._event_queue.get()
            yield event
            if event.get("type") in {"closed", "fatal_error"}:
                break

    async def _run(self) -> None:
        try:
            genai, types = self._load_sdk()
            client = genai.Client(
                vertexai=True,
                project=self.config.project_id,
                location=self.config.location,
                credentials=get_gcp_credentials(),
            )
            connect_config = self._build_connect_config(types)
            async with client.aio.live.connect(
                model=self.config.model,
                config=connect_config,
            ) as sdk_session:
                await self._event_queue.put({"type": "status", "status": "session_started"})
                send_task = asyncio.create_task(self._send_loop(sdk_session, types))
                recv_task = asyncio.create_task(self._receive_loop(sdk_session))
                done, pending = await asyncio.wait(
                    {send_task, recv_task},
                    return_when=asyncio.FIRST_EXCEPTION,
                    timeout=self.config.session_timeout_sec,
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    exc = task.exception()
                    if exc:
                        raise exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._event_queue.put({"type": "fatal_error", "message": str(exc)})
        finally:
            self._closed.set()
            await self._event_queue.put({"type": "closed"})

    @staticmethod
    def _load_sdk() -> Any:
        try:
            from google import genai  # type: ignore
            from google.genai import types  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "缺少 google-genai 依赖，请先运行 pip install -r requirements-live.txt"
            ) from exc
        return genai, types

    @staticmethod
    def _resolve_media_resolution(types: Any, value: str) -> Any:
        """把 'low/medium/high' 友好值转成 SDK / Vertex 接受的枚举形式。

        - google-genai 提供 ``types.MediaResolution`` 枚举时优先用枚举对象；
        - 否则退化为 Vertex API 期望的 ``MEDIA_RESOLUTION_<LEVEL>`` 字符串。
        """
        key = str(value or "").strip().lower()
        if not key:
            return None
        level_map = {
            "low": "LOW",
            "medium": "MEDIUM",
            "high": "HIGH",
            "media_resolution_low": "LOW",
            "media_resolution_medium": "MEDIUM",
            "media_resolution_high": "HIGH",
        }
        level = level_map.get(key)
        if level is None:
            return None
        enum_cls = getattr(types, "MediaResolution", None)
        if enum_cls is not None:
            with contextlib.suppress(Exception):
                return getattr(enum_cls, f"MEDIA_RESOLUTION_{level}")
            with contextlib.suppress(Exception):
                return getattr(enum_cls, level)
        return f"MEDIA_RESOLUTION_{level}"

    def _build_connect_config(self, types: Any) -> Any:
        config: Dict[str, Any] = {
            "response_modalities": self.config.response_modalities,
            "speech_config": {"language_code": self.config.language_code},
            "system_instruction": {
                "parts": [{"text": self.config.system_instruction}]
            },
            "context_window_compression": {
                "trigger_tokens": self.config.context_trigger_tokens,
                "sliding_window": {"target_tokens": self.config.context_target_tokens},
            },
            **self.config.extra_config,
        }
        if self.config.enable_transcription:
            config["input_audio_transcription"] = {}
            config["output_audio_transcription"] = {}
        if self.config.enable_affective_dialog:
            config["enable_affective_dialog"] = True
        if self.config.proactive_audio:
            config["proactivity"] = {"proactive_audio": True}
        if self.config.tools:
            config["tools"] = self.config.tools
        if self.config.media_resolution:
            resolved = self._resolve_media_resolution(types, self.config.media_resolution)
            if resolved is not None:
                config["media_resolution"] = resolved

        if hasattr(types, "LiveConnectConfig"):
            with contextlib.suppress(Exception):
                return types.LiveConnectConfig(**config)
        return config

    async def _send_loop(self, sdk_session: Any, types: Any) -> None:
        while not self._closed.is_set():
            item = await self._input_queue.get()
            item_type = item.get("type")
            if item_type == "close":
                break
            if item_type == "text":
                await self._send_text_to_sdk(sdk_session, types, item["text"])
            elif item_type == "audio":
                await self._send_realtime_to_sdk(
                    sdk_session, types, "audio", item["data"], item["mimeType"]
                )
            elif item_type == "video":
                await self._send_realtime_to_sdk(
                    sdk_session, types, "video", item["data"], item["mimeType"]
                )
            elif item_type == "audio_stream_end":
                await self._send_audio_stream_end(sdk_session)
            elif item_type == "tool_response":
                await self._send_tool_response_to_sdk(
                    sdk_session, types, item.get("functionResponses") or []
                )

    @staticmethod
    async def _send_tool_response_to_sdk(
        sdk_session: Any,
        types: Any,
        function_responses: List[Dict[str, Any]],
    ) -> None:
        if not function_responses:
            return
        built: List[Any] = []
        for fr in function_responses:
            kwargs = {
                "id": fr.get("id"),
                "name": fr.get("name"),
                "response": fr.get("response") or {"result": "ok"},
            }
            scheduling = fr.get("scheduling")
            if scheduling:
                kwargs["scheduling"] = scheduling
            # 优先用 SDK 类型；不可用则退化为原始 dict
            try:
                built.append(types.FunctionResponse(**kwargs))
            except Exception:
                built.append(kwargs)
        try:
            await sdk_session.send_tool_response(function_responses=built)
        except TypeError:
            await sdk_session.send_tool_response(built)

    @staticmethod
    async def _send_text_to_sdk(sdk_session: Any, types: Any, text: str) -> None:
        content = types.Content(role="user", parts=[types.Part(text=text)])
        try:
            await sdk_session.send_client_content(turns=content, turn_complete=True)
        except TypeError:
            await sdk_session.send_client_content(content=content, turn_complete=True)

    @staticmethod
    async def _send_realtime_to_sdk(
        sdk_session: Any,
        types: Any,
        kind: str,
        data: bytes,
        mime_type: str,
    ) -> None:
        blob = types.Blob(data=data, mime_type=mime_type)
        kwargs = {kind: blob}
        try:
            await sdk_session.send_realtime_input(**kwargs)
            return
        except TypeError:
            pass
        try:
            await sdk_session.send_realtime_input(media=blob)
            return
        except TypeError:
            pass
        await sdk_session.send(input=blob)

    @staticmethod
    async def _send_audio_stream_end(sdk_session: Any) -> None:
        if not hasattr(sdk_session, "send_realtime_input"):
            return
        for kwargs in (
            {"audio_stream_end": True},
            {"audio_stream_end": {}},
            {"activity_end": True},
        ):
            with contextlib.suppress(TypeError, AttributeError):
                await sdk_session.send_realtime_input(**kwargs)
                return

    async def _receive_loop(self, sdk_session: Any) -> None:
        try:
            while not self._closed.is_set():
                received_any = False
                async for message in sdk_session.receive():
                    received_any = True
                    for event in _extract_events(message):
                        await self._event_queue.put(event)
                if not received_any:
                    await asyncio.sleep(0.05)
        except Exception as exc:
            if not self._closed.is_set():
                await self._event_queue.put({"type": "fatal_error", "message": str(exc)})


class MockGeminiLiveSession(GeminiLiveSession):
    """本地协议测试用，不访问 Google。"""

    async def _run(self) -> None:
        await self._event_queue.put({"type": "status", "status": "session_started", "mock": True})
        try:
            while not self._closed.is_set():
                item = await self._input_queue.get()
                item_type = item.get("type")
                if item_type == "close":
                    break
                if item_type == "text":
                    await self._event_queue.put(
                        {"type": "text", "text": f"Mock response: {item.get('text', '')}"}
                    )
                    await self._event_queue.put({"type": "turn_complete"})
                elif item_type == "audio":
                    await self._event_queue.put(
                        {"type": "transcription", "source": "input", "text": "mock audio"}
                    )
                elif item_type == "video":
                    # 模拟一次动作识别：每收到 5 帧就上报一次挥手动作，便于联调
                    self._mock_frame_count = getattr(self, "_mock_frame_count", 0) + 1
                    if self._mock_frame_count % 5 == 0:
                        await self._event_queue.put(
                            {
                                "type": "tool_call",
                                "functionCalls": [
                                    {
                                        "id": f"mock-{self._mock_frame_count}",
                                        "name": "report_user_action",
                                        "args": {
                                            "action": "wave_hand",
                                            "description": "Mock 模式下的挥手动作",
                                            "confidence": 0.9,
                                        },
                                    }
                                ],
                            }
                        )
                    else:
                        await self._event_queue.put(
                            {"type": "text", "text": "Mock received one video frame."}
                        )
                elif item_type == "audio_stream_end":
                    await self._event_queue.put({"type": "interrupted"})
                elif item_type == "tool_response":
                    await self._event_queue.put(
                        {
                            "type": "text",
                            "text": f"Mock 已收到 {len(item.get('functionResponses') or [])} 条工具回执。",
                        }
                    )
        finally:
            self._closed.set()
            await self._event_queue.put({"type": "closed"})
