# -*- coding: utf-8 -*-
# @File：block_generator.py
# @Time：2025/6/19 15:34
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, Optional
import re

from utils.utils import merge_generated_content

router = APIRouter()


class MergeRequest(BaseModel):
    raw: str
    component: Dict[str, Any]
    # 可选：是否校验 component_id
    check_id: Optional[bool] = False


@router.post("/merge_block", summary="合并 generated_content 到 component，保持数据原样")
def merge_content_endpoint(req: MergeRequest):
    raw = req.raw
    component = req.component
    check_id = req.check_id

    # 提取 <think> 部分（可选）
    try:
        m = re.search(r"<think>.*?</think>", raw, flags=re.DOTALL)
    except TypeError:
        raise HTTPException(status_code=400, detail="raw 必须是字符串")
    thout = m.group(0) if m else ""
    # 移除 <think> 段
    res = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    # 去掉 markdown code fence
    res = re.sub(r'^\s*```[^\n]*\n?|```[\s]*$', '', res, flags=re.MULTILINE).strip()

    # 合并 generated_content
    try:
        merged = merge_generated_content(component, res, check_id=check_id)
    except ValueError as e:
        # 返回 400 错误，并包含提示
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "Block_Generator_thout": thout,
        "Block_Generator_res": merged,
    }
