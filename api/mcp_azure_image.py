# -*- coding: utf-8 -*-
"""Azure gpt-image-2 的 Dify MCP 入口。

传输协议对齐本仓库 Dify 客户端 ``core.mcp.client.streamable_client``：
客户端 POST JSON-RPC，Accept 同时包含 ``application/json`` 与 ``text/event-stream``；
服务端对请求返回 ``application/json``，对通知返回 202，不发 session id。
Dify 因此不会再开 GET SSE，导入时直接 ``initialize`` + ``tools/list``。

工具入参按线上 ``GET /openapi.json`` 的 ``GenerateAzureImagePayload`` 收口，
不改写 prompt / negative_prompt 原文。默认在进程内调用
``api.cre_image_azure.generate_image_azure``。设置环境变量
``CRE_IMAGE_AZURE_MCP_UPSTREAM`` 时改为把同一 JSON 转发到该 URL。

Dify 导入：
- 服务器 URL：http://<host>:2906/mcp
- 名称：Azure gpt-image-2
- 服务器标识：azure-gpt-image
- 认证方式选「无」或请求头，不要开动态客户端注册
- SSE 读取超时保持 300 秒。生成读超时由这项控制，默认 30 秒的连接超时不用改
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

router = APIRouter(tags=["mcp_azure_image"])

SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL_VERSION = "2025-03-26"
TOOL_NAME = "generate_image_azure"
UPSTREAM_ENV = "CRE_IMAGE_AZURE_MCP_UPSTREAM"

_ASPECT_RATIOS = ["1:1", "3:2", "2:3", "4:3", "3:4", "4:5", "5:4", "16:9", "9:16", "21:9"]

TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "minLength": 1,
            "description": "生成或编辑说明。原文提交，不会被改写。",
        },
        "negative_prompt": {
            "type": "string",
            "description": "负向提示。原文提交，由生图接口拼入 prompt 文本。",
        },
        "aspect_ratio": {
            "type": "string",
            "enum": _ASPECT_RATIOS,
            "default": "1:1",
            "description": "宽高比。支持 1:1、3:2、2:3、4:3、3:4、4:5、5:4、16:9、9:16、21:9。留空为 1:1。",
        },
        "size": {
            "type": "string",
            "description": "显式尺寸，形如 1024x1024。两边须为 16 的倍数，长边不超过 3840，长短比不超过 3:1，像素 655360–8294400。留空则按 aspect_ratio。",
        },
        "n": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "default": 1,
            "description": "生成张数，1 到 10。",
        },
        "quality": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "low、medium 或 high。留空使用服务端默认。",
        },
        "output_format": {
            "type": "string",
            "enum": ["png", "jpeg"],
            "description": "png 或 jpeg。留空使用服务端默认。",
        },
        "output_compression": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "0 到 100，仅 jpeg 生效。",
        },
        "background": {
            "type": "string",
            "enum": ["auto", "transparent"],
            "description": "auto 或 transparent。transparent 需要 png。",
        },
        "reference_image_url": {
            "type": "string",
            "description": "单张参考图 https URL。填写后走图生图，且不要同时传 reference_images。",
        },
        "reference_images": {
            "type": "array",
            "description": "参考图列表。每项只能包含 image_url、image_base64、gs_uri 其中之一，可选 mime_type。非空时走图生图。",
            "items": {
                "type": "object",
                "properties": {
                    "image_url": {"type": "string", "description": "https 图片地址"},
                    "image_base64": {"type": "string", "description": "Base64 图片数据"},
                    "mime_type": {"type": "string", "description": "image/png 或 image/jpeg"},
                    "gs_uri": {"type": "string", "description": "gs://bucket/object"},
                },
            },
        },
    },
    "required": ["prompt"],
}

TOOL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "integer", "description": "业务状态码，200 为成功"},
        "message": {"type": "string"},
        "data": {
            "type": "object",
            "description": "成功时含 images[].public_url 与 text_parts",
        },
        "text": {"type": "string", "description": "由 public_url 组装的 Markdown 图片"},
    },
    "required": ["code", "message", "text"],
}


def _rpc_result(request_id: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _rpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


def _negotiate_protocol(requested: Any) -> str:
    version = str(requested or "").strip()
    if version in SUPPORTED_PROTOCOL_VERSIONS:
        return version
    return DEFAULT_PROTOCOL_VERSION


def _clean_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """去掉空可选字段。prompt 与 negative_prompt 的非空文本原样保留。"""
    cleaned: dict[str, Any] = {}
    for key, value in arguments.items():
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if key == "reference_images" and text:
                cleaned[key] = json.loads(text)
                continue
            if not text and key != "prompt":
                continue
            cleaned[key] = value if key in ("prompt", "negative_prompt") else text
            continue
        if isinstance(value, list) and not value:
            continue
        cleaned[key] = value
    return cleaned


def _markdown_from_envelope(body: dict[str, Any]) -> str:
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    lines: list[str] = []
    for image in data.get("images") or []:
        if not isinstance(image, dict):
            continue
        url = image.get("public_url")
        if isinstance(url, str) and url.strip():
            lines.append(f"![Generated Image]({url.strip()})")
    if lines:
        return "\n\n".join(lines)
    message = body.get("message")
    return message if isinstance(message, str) else ""


def _tool_result(status: int, body: dict[str, Any]) -> dict[str, Any]:
    code = body.get("code", status)
    text = _markdown_from_envelope(body)
    structured = {
        "code": code,
        "message": body.get("message") or "",
        "data": body.get("data"),
        "text": text,
    }
    is_error = status >= 400 or (isinstance(code, int) and code >= 400)
    content: list[dict[str, str]] = [
        {"type": "text", "text": json.dumps(structured, ensure_ascii=False)}
    ]
    if text:
        content.append({"type": "text", "text": text})
    return {"content": content, "structuredContent": structured, "isError": is_error}


async def _call_in_process(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from fastapi import HTTPException

    from api.cre_image_azure import GenerateAzureImagePayload, generate_image_azure

    try:
        model = GenerateAzureImagePayload.model_validate(payload)
    except ValidationError as exc:
        return 422, {"code": 422, "message": exc.errors()[0]["msg"], "data": None}
    try:
        response = await generate_image_azure(model)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
        return exc.status_code, {"code": exc.status_code, "message": detail, "data": None}
    raw = response.body
    parsed = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    return response.status_code, parsed


async def _call_upstream(payload: dict[str, Any], upstream: str) -> tuple[int, dict[str, Any]]:
    timeout = httpx.Timeout(connect=15.0, read=200.0, write=30.0, pool=15.0)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.post(upstream, json=payload)
    try:
        parsed = response.json()
    except json.JSONDecodeError:
        parsed = {"code": response.status_code, "message": response.text[:800], "data": None}
    if not isinstance(parsed, dict):
        parsed = {"code": response.status_code, "message": str(parsed), "data": None}
    return response.status_code, parsed


async def _invoke_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = _clean_arguments(arguments)
    except json.JSONDecodeError as exc:
        return _tool_result(422, {"code": 422, "message": f"reference_images 不是合法 JSON：{exc}", "data": None})
    if not str(payload.get("prompt") or "").strip():
        return _tool_result(422, {"code": 422, "message": "prompt 不能为空", "data": None})

    upstream = (os.getenv(UPSTREAM_ENV) or "").strip()
    try:
        if upstream:
            status, body = await _call_upstream(payload, upstream)
        else:
            status, body = await _call_in_process(payload)
    except Exception as exc:  # noqa: BLE001
        return _tool_result(500, {"code": 500, "message": f"生图调用失败: {exc}", "data": None})
    return _tool_result(status, body)


def _list_tools_result() -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": TOOL_NAME,
                "title": "Azure gpt-image-2 生图",
                "description": (
                    "调用已接通的 Azure gpt-image-2 接口。只填 prompt 时是文生图；"
                    "填写 reference_image_url 或 reference_images 时是图生图。"
                    "成功结果里的 data.images[].public_url 是可公网访问的图片地址。"
                ),
                "inputSchema": TOOL_INPUT_SCHEMA,
                "outputSchema": TOOL_OUTPUT_SCHEMA,
            }
        ]
    }


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    try:
        message = await request.json()
    except Exception:  # noqa: BLE001
        return _rpc_error(None, -32700, "Parse error")
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "Invalid Request")

    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if isinstance(method, str) and method.startswith("notifications/"):
        return Response(status_code=202)

    if method == "initialize":
        requested = params.get("protocolVersion")
        return _rpc_result(
            request_id,
            {
                "protocolVersion": _negotiate_protocol(requested),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "azure-gpt-image", "version": "1.0.0"},
                "instructions": "文生图只传 prompt。图生图额外传 reference_image_url 或 reference_images。",
            },
        )
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        return _rpc_result(request_id, _list_tools_result())
    if method == "tools/call":
        name = params.get("name")
        if name != TOOL_NAME:
            return _rpc_error(request_id, -32602, f"Unknown tool: {name}")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        return _rpc_result(request_id, await _invoke_tool(arguments))
    return _rpc_error(request_id, -32601, f"Method not found: {method}")


@router.get("/mcp")
async def mcp_get_unsupported() -> JSONResponse:
    return JSONResponse(status_code=405, content={"message": "MCP Streamable HTTP uses POST"})
