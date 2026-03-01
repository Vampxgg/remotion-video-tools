# -*- coding: utf-8 -*-
# @File：utils.py
# @Time：2025/5/14 18:22
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com
import re
from typing import Any, Dict, Optional
import json


def extract_first_json_object(s: str) -> str:
    start = s.find("{")
    if start == -1:
        return ""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "\"":
                in_string = False
        else:
            if ch == "\"":
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i + 1]
    return ""


def merge_generated_content(component: Dict[str, Any], generated_str: str, *, check_id: bool = False) -> Dict[str, Any]:
    """
    只做最小解析：去掉 // 注释后，json.loads；若失败则提取首个 JSON 对象再 loads；
    然后取 parsed['generated_content'] 原样插入 component，删除旧字段，不做额外包装。
    """
    # 去掉 // 注释
    cleaned = re.sub(r'//.*', '', generated_str)
    # 尝试解析 JSON
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # 提取第一个 JSON 对象子串
        json_sub = extract_first_json_object(cleaned)
        if not json_sub:
            raise ValueError("无法从 raw 中提取 JSON 对象")
        try:
            parsed = json.loads(json_sub)
        except json.JSONDecodeError as e:
            raise ValueError(f"提取后的 JSON 仍无法解析: {e}")

    if not isinstance(parsed, dict):
        raise ValueError(f"解析后对象类型错误，期望 dict，收到: {type(parsed).__name__}")
    if "generated_content" not in parsed:
        raise ValueError("parsed JSON 中缺少 'generated_content' 字段")
    if check_id:
        comp_id_orig = component.get("component_id")
        comp_id_parsed = parsed.get("component_id")
        if comp_id_parsed is None:
            raise ValueError("parsed JSON 中缺少 'component_id' 字段，无法校验")
        if comp_id_orig != comp_id_parsed:
            raise ValueError(f"component_id 不匹配: component={comp_id_orig} vs parsed={comp_id_parsed}")

    # 浅拷贝 component，不修改原对象
    new_obj = component.copy()
    # 删除旧字段
    for key in ("generation_instruction", "required_knowledge", "multimedia_requirements"):
        new_obj.pop(key, None)
    # 原样插入 generated_content
    new_obj["generated_content"] = parsed["generated_content"]
    return new_obj


def extract_ordered_segments(md: str):
    img_pattern = re.compile(r'!\[.*?\]\((.*?)\)')
    segments = []
    last_end = 0

    for m in img_pattern.finditer(md):
        text_chunk = md[last_end:m.start()]
        clean_text = clean_text_chunk(text_chunk)
        if clean_text:
            segments.append(("text", clean_text))

        img_url = m.group(1)
        segments.append(("image", img_url))
        last_end = m.end()

    tail = md[last_end:]
    clean_tail = clean_text_chunk(tail)
    if clean_tail:
        segments.append(("text", clean_tail))

    return segments


def clean_text_chunk(txt: str) -> str:
    txt = re.sub(r'\[([^\]]+)\]\((?:https?://[^\)]+)\)', r'\1', txt)
    txt = re.sub(r'https?://\S+', '', txt)

    lines = []
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('- ') or line.startswith('['):
            continue
        lines.append(line)

    return '\n'.join(lines)
