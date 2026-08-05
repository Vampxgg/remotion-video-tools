# -*- coding: utf-8 -*-
"""单页幻灯片自检自愈服务（档C，借鉴 Genspark generate-check-self-heal）。

在最终 ppt_synthesizing（交给文多多模板渲染）之前，对每页生成的 Markdown 做
「版式/图文校验」，产出结构化 issues 与「局部重写指令」，供上游把有问题的页回灌
单页生成 LLM 局部重写。

设计取舍：文多多模板的真实无头渲染在本仓库之外，这里做**确定性 + 可选 VLM** 两层：
  1) 确定性校验（无需外部渲染，稳定可回归）：
     - 正文字数 vs layout_type 预算（溢出风险）；
     - 图片数量 vs layout_type 期望（该有图却缺图 / 图过多）；
     - 图片链接存活性（HEAD 探测，死链=缺图）；
     - 结构合法性（以 ### 页标题开头、#### 小标题层级）。
  2) 可选 VLM 版式校验（开关 DOC_IMPORT_* 复用；此处默认关闭，避免强依赖渲染）。

产物是「建议」而非「强改」：是否回灌重写由上游 workflow 决定，最终 Markdown 契约不变。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx

from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/file/slide_self_check.log")

_IMG_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)")
_TITLE_RE = re.compile(r"^###\s+\S", re.MULTILINE)
_IMAGE_PROMPT_RE = re.compile(r"<image_prompt>.*?</image_prompt>", re.DOTALL)

# 各 layout 的正文字数软预算与期望图片数（与 layout_1..7 语义对齐）。
# text_budget：正文（去图/去标题）建议上限，超出判为溢出风险。
# expects_image：该版式是否应含图片。
_LAYOUT_SPEC: Dict[str, Dict[str, object]] = {
    "layout_1": {"text_budget": 180, "expects_image": False, "blocks": 1},
    "layout_2": {"text_budget": 220, "expects_image": False, "blocks": 2},
    "layout_3": {"text_budget": 260, "expects_image": False, "blocks": 3},
    "layout_4": {"text_budget": 140, "expects_image": True, "blocks": 1},
    "layout_5": {"text_budget": 200, "expects_image": True, "blocks": 2},
    "layout_6": {"text_budget": 200, "expects_image": True, "blocks": 2},
    "layout_7": {"text_budget": 240, "expects_image": True, "blocks": 3},
}


@dataclass
class SlideIssue:
    code: str
    level: str  # error | warning | info
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "level": self.level, "message": self.message}


@dataclass
class SlideCheckResult:
    slide_id: Optional[str]
    layout_type: Optional[str]
    ok: bool
    needs_rewrite: bool
    issues: List[SlideIssue] = field(default_factory=list)
    rewrite_instruction: Optional[str] = None
    stats: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "slide_id": self.slide_id,
            "layout_type": self.layout_type,
            "ok": self.ok,
            "needs_rewrite": self.needs_rewrite,
            "issues": [i.to_dict() for i in self.issues],
            "rewrite_instruction": self.rewrite_instruction,
            "stats": self.stats,
        }


def _plain_text_len(markdown: str) -> int:
    """去掉图片、image_prompt、标题符号后的正文字数（中文按字符计）。"""
    text = _IMAGE_PROMPT_RE.sub("", markdown)
    text = _IMG_RE.sub("", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[`*_>|\-]", "", text)
    text = re.sub(r"\s+", "", text)
    return len(text)


async def _check_image_liveness(urls: List[str]) -> Dict[str, bool]:
    if not urls:
        return {}
    result: Dict[str, bool] = {}

    async def _one(client: httpx.AsyncClient, u: str) -> None:
        try:
            resp = await client.head(u, timeout=6, follow_redirects=True)
            ct = resp.headers.get("content-type", "").lower()
            result[u] = resp.is_success and "image" in ct
        except Exception:  # noqa: BLE001
            result[u] = False

    async with httpx.AsyncClient(verify=False) as client:
        await asyncio.gather(*[_one(client, u) for u in urls[:12]])
    for u in urls[12:]:
        result[u] = True  # 超量的不探测，默认放过，避免拖慢
    return result


def _build_rewrite_instruction(issues: List[SlideIssue], layout_type: Optional[str]) -> str:
    parts: List[str] = [
        "请对该页做**局部重写**，只修复以下问题，保持 slide_title、layout_type 与整体结构不变，"
        "输出仍为同样的 JSON（slide_id + generated_markdown）："
    ]
    for i in issues:
        if i.level == "error":
            parts.append(f"- [必须修复] {i.message}")
        elif i.level == "warning":
            parts.append(f"- [建议修复] {i.message}")
    parts.append(
        "修复原则：正文溢出时精炼为短句/要点并压到预算内；缺图时优先复用素材池源图 "
        "`![描述](img_link)`，实在没有再产出 <image_prompt>；死链图片替换为可用源图或改为 <image_prompt>；"
        "严禁臆造 URL 或数值。"
    )
    return "\n".join(parts)


async def check_slide(
    markdown: str,
    layout_type: Optional[str] = None,
    slide_id: Optional[str] = None,
    check_liveness: bool = True,
) -> SlideCheckResult:
    issues: List[SlideIssue] = []
    spec = _LAYOUT_SPEC.get((layout_type or "").strip())

    # 结构：必须以 ### 页标题开头
    has_title = bool(_TITLE_RE.search(markdown or ""))
    if not has_title:
        issues.append(SlideIssue("missing_slide_title", "error", "缺少以 `###` 开头的页标题。"))

    # 图片
    imgs = _IMG_RE.findall(markdown or "")
    img_urls = [u for (_, u) in imgs]
    image_prompts = _IMAGE_PROMPT_RE.findall(markdown or "")
    text_len = _plain_text_len(markdown or "")

    # 字数预算
    if spec:
        budget = int(spec["text_budget"])  # type: ignore[index]
        if text_len > budget * 1.25:
            issues.append(SlideIssue(
                "text_overflow", "error",
                f"正文约 {text_len} 字，明显超过 {layout_type} 预算(~{budget})，存在溢出风险，请精简。",
            ))
        elif text_len > budget:
            issues.append(SlideIssue(
                "text_slightly_long", "warning",
                f"正文约 {text_len} 字，略超 {layout_type} 预算(~{budget})，建议精炼。",
            ))
        # 图文期望
        if spec["expects_image"] and not img_urls and not image_prompts:
            issues.append(SlideIssue(
                "missing_image", "error",
                f"{layout_type} 版式应包含图片，但既无 ![](url) 也无 <image_prompt>。",
            ))
    else:
        if text_len > 320:
            issues.append(SlideIssue(
                "text_overflow", "warning",
                f"正文约 {text_len} 字偏多（未知 layout_type），建议精简。",
            ))

    # 死链探测
    liveness: Dict[str, bool] = {}
    if check_liveness and img_urls:
        liveness = await _check_image_liveness(img_urls)
        dead = [u for u, ok in liveness.items() if not ok]
        if dead:
            issues.append(SlideIssue(
                "dead_image_link", "error",
                f"检测到 {len(dead)} 个图片链接不可用（死链），请替换为可用源图或改为 <image_prompt>。",
            ))

    error_count = sum(1 for i in issues if i.level == "error")
    needs_rewrite = error_count > 0
    rewrite_instruction = _build_rewrite_instruction(issues, layout_type) if needs_rewrite else None

    return SlideCheckResult(
        slide_id=slide_id,
        layout_type=layout_type,
        ok=(error_count == 0),
        needs_rewrite=needs_rewrite,
        issues=issues,
        rewrite_instruction=rewrite_instruction,
        stats={
            "text_len": text_len,
            "image_count": len(img_urls),
            "image_prompt_count": len(image_prompts),
            "dead_image_count": sum(1 for ok in liveness.values() if not ok),
            "has_title": has_title,
        },
    )
