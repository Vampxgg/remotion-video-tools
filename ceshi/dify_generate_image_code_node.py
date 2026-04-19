# -*- coding: utf-8 -*-
"""
Dify 工作流 · 代码执行节点（Python）
无缝替换自定义工具节点 generate_image

相较自定义工具的改进：
  - 完整的 HTTP 状态码识别（4xx 立即报错、429/5xx 自动重试）
  - 内置指数退避重试（最多 3 次：间隔 2s / 4s / 8s）
  - 输出同时提供 Object 类型（result）和 String 类型（result_str / images_str）

================== Dify 节点配置指引 ==================

【沙箱前置条件（自托管）】
1. docker/volumes/sandbox/conf/config.yaml：
       enable_network: true
2. docker/volumes/sandbox/dependencies/python-requirements.txt：
       httpx

【节点输入变量】（在节点配置面板声明并绑定上游）
┌─────────────────────┬──────────────┬─────────────────────────────────────────┐
│ 变量名              │ 类型          │ 来源绑定                                │
├─────────────────────┼──────────────┼─────────────────────────────────────────┤
│ prompt              │ String       │ Start.prompt                            │
│ aspect_ratio        │ String       │ Start.aspect_ratio                      │
│ image_size          │ String       │ Start.image_size                        │
│ response_mime_type  │ String       │ Start.response_mime_type                │
│ reference_images    │ Array[Object]│ 代码执行（reference_images_parser）.reference_images │
└─────────────────────┴──────────────┴─────────────────────────────────────────┘

【节点输出变量】（在节点配置面板声明）
┌─────────────┬─────────┬──────────────────────────────────────────────────────┐
│ 变量名      │ 类型    │ 说明                                                  │
├─────────────┼─────────┼──────────────────────────────────────────────────────┤
│ result      │ Object  │ API data 字段：{images:[{public_url,mime_type},...],  │
│             │         │ text_parts:[...]}                                     │
│ result_str  │ String  │ Markdown 格式：![](...) 图片 + 文字说明，供 Answer 节点 │
│ images_str  │ String  │ 换行分隔的纯 URL 列表，方便后续节点二次处理            │
└─────────────┴─────────┴──────────────────────────────────────────────────────┘

【Answer 节点】改为引用 {{#代码节点ID.result_str#}}

======================================================
"""

import json  # noqa: F401（Dify 沙箱中 httpx 内部依赖 json，明确 import 更稳健）
import time

import httpx

# ========== 按实际部署地址修改 ==========
_API_ENDPOINT = "http://your-server:8000/api/v1/generate_image"
# ========================================

# ---------- 工作流固定参数（对应原工具节点的常量/混合参数）----------
_MODEL_ID = "gemini-3.1-flash-image-preview"
_INCLUDE_THOUGHTS = True
_INCLUDE_RESPONSE_TEXT = False
_PERSON_GENERATION = "allow_adult"
_SAFETY_FILTER_LEVEL = "OFF"
_RESPONSE_COUNT = 1
_THINKING_LEVEL = "HIGH"

_SYSTEM_INSTRUCTION = (
    "【合规约束】\n"
    "1. 画面中如有文字，必须为简体中文且字形正确；严禁出现繁体字、英文字母、日文假名。\n"
    "2. 严禁包含裸露、色情、性暗示、赌博、毒品、暴力血腥元素。\n"
    "3. 严禁出现台湾旗帜、台湾地图、领土争议或任何政治敏感符号。\n"
    "4. 严禁出现未成年人的不当或敏感场景。\n"
    "5. 所有涉及知识性内容（如地理、历史、科学常识）必须准确符合事实，不可出现常识性谬误。\n"
    "\n"
    "【画师准则】\n"
    "1. 当用户描述模糊或过于简短时，请你发挥想象力，自行补充合理的场景、构图、光影、色彩细节。\n"
    "2. 画面必须具有层次感：要有明确的前景主体、中景衬托和背景氛围。\n"
    "3. 避免单调的元素堆砌，注重画面的故事性和视觉吸引力。\n"
    "4. 若用户需求涉及精确的物理结构、建筑比例或特定地理特征，必须确保视觉呈现的知识准确性。"
)

_NEGATIVE_PROMPT = (
    "nudity, sexual content, gambling, drugs, gore, weapons, traditional Chinese characters, "
    "English text, watermark, Taiwan flag, political symbols, minors in inappropriate context, "
    "blurry, low resolution, deformed hands, extra fingers, distorted face, artifacts, noise, "
    "cropped, out of frame, English UI, English captions, Latin typography, Western infographic titles"
)

# ---------- 重试策略 ----------
_RETRIES = 3
_DELAYS = [2, 4, 8]  # 秒，依次用于第 1/2/3 次失败后的等待


def main(
    prompt: str,
    aspect_ratio: str = "1:1",
    image_size: str = "",
    response_mime_type: str = "",
    reference_images: list = None,
) -> dict:
    """
    :param prompt:             画面描述（必填）
    :param aspect_ratio:       宽高比，如 1:1 / 16:9（来自 Start 节点）
    :param image_size:         分辨率档位：512 / 1K / 2K / 4K（来自 Start 节点）
    :param response_mime_type: 输出格式：image/png 或 image/jpeg（来自 Start 节点）
    :param reference_images:   参考图对象列表（来自上游代码节点解析结果）
    :return: {"result": {...}, "result_str": "...", "images_str": "..."}
    """
    body = {
        "prompt": prompt,
        "model_id": _MODEL_ID,
        "system_instruction": _SYSTEM_INSTRUCTION,
        "aspect_ratio": str(aspect_ratio).strip() if aspect_ratio and str(aspect_ratio).strip() else "1:1",
        "negative_prompt": _NEGATIVE_PROMPT,
        "person_generation": _PERSON_GENERATION,
        "include_response_text": _INCLUDE_RESPONSE_TEXT,
        "include_thoughts": _INCLUDE_THOUGHTS,
        "thinking_level": _THINKING_LEVEL,
        "safety_filter_level": _SAFETY_FILTER_LEVEL,
        "response_count": _RESPONSE_COUNT,
    }

    if image_size and str(image_size).strip():
        body["image_size"] = str(image_size).strip()
    if response_mime_type and str(response_mime_type).strip():
        body["response_mime_type"] = str(response_mime_type).strip()
    if reference_images and isinstance(reference_images, list) and len(reference_images) > 0:
        body["reference_images"] = reference_images

    last_err = "未知错误"

    with httpx.Client(
        timeout=httpx.Timeout(360.0, connect=15.0, write=60.0),
        proxies={},  # 绕过沙箱 Squid SSRF 代理，直连内网 API 服务器
    ) as client:
        for i in range(_RETRIES):
            try:
                resp = client.post(_API_ENDPOINT, json=body)
                status = resp.status_code

                if status != 200:
                    err_text = resp.text[:400]
                    # 4xx（非 429）：参数/权限错误，不重试，直接向上抛出
                    if status != 429 and 400 <= status < 500:
                        raise ValueError(f"请求参数错误 HTTP {status}: {err_text}")
                    # 429 / 5xx：服务繁忙或临时故障，计入重试
                    last_err = f"HTTP {status}: {err_text}"
                    if i < _RETRIES - 1:
                        time.sleep(_DELAYS[i])
                    continue

                resp_data = resp.json()
                api_code = resp_data.get("code", 200)
                if api_code != 200:
                    raise ValueError(
                        f"API 返回错误 {api_code}: {resp_data.get('message', '')}"
                    )

                data = resp_data.get("data") or {}
                images = data.get("images") or []
                text_parts = data.get("text_parts") or []
                urls = [img["public_url"] for img in images if img.get("public_url")]

                # result_str：Markdown 图片链接 + 文字说明，直接用于 Answer 节点
                result_str = "\n".join(f"![]({u})" for u in urls)
                if text_parts:
                    result_str += "\n\n" + "\n".join(text_parts)

                return {
                    "result": data,           # Object 类型，包含 images / text_parts
                    "result_str": result_str,  # String 类型，Markdown 格式
                    "images_str": "\n".join(urls),  # String 类型，纯 URL 列表
                }

            except ValueError:
                raise  # 参数错误 / API 业务错误，不重试

            except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as e:
                last_err = f"网络请求失败: {e}"
                if i < _RETRIES - 1:
                    time.sleep(_DELAYS[i])

            except Exception as e:
                last_err = str(e)
                if i < _RETRIES - 1:
                    time.sleep(_DELAYS[i])

    raise ValueError(f"图片生成失败（重试 {_RETRIES} 次后）: {last_err}")


# ========== 本地自测（勿粘贴进 Dify） ==========
# if __name__ == "__main__":
#     result = main(
#         prompt="一只在樱花树下读书的熊猫",
#         aspect_ratio="16:9",
#         image_size="1K",
#         response_mime_type="image/png",
#     )
#     print(result["result_str"])
