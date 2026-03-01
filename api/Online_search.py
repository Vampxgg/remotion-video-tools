# -*- coding: utf-8 -*-
# @File：Online_search.py
# @Time：2025/5/14 18:17
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com
from fastapi import APIRouter, UploadFile, File
from typing import List
from utils.utils import extract_ordered_segments

router = APIRouter()


@router.post("/parse_sources")
async def parse_sources(files: List[UploadFile] = File(...)):
    results = []

    for file in files:
        content = (await file.read()).decode("utf-8")
        segments = extract_ordered_segments(content)
        results.append({
            "filename": file.filename,
            "segments": [{"type": kind, "content": val} for kind, val in segments]
        })

    return results
