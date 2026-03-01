# Dify 知识库代码节点：多输入智能 JSON 解析器
# Author: Senior Software Engineer
# Version: 2.0

import json
import re
from typing import Any, Dict, List, Union

# 确保已在 Dify 的“依赖管理”中添加了 json-repair 依赖
# pip install json-repair
import json_repair

# ==============================================================================
#  核心解析逻辑 (来自于你的版本，因为它非常健壮)
#  无需修改，我们直接复用这个强大的底层函数。
# ==============================================================================
def _extract_most_significant_json(text: str) -> Union[Dict, List]:
    """
    智能地从可能包含噪音、注释或多个JSON片段的文本中提取最主要的JSON对象或列表。
    
    智能性体现在：
    1.  **分层解析**: 优先使用高速标准库，失败后才动用重量级修复工具。
    2.  **候选者识别**: 即使文本中散落着多个JSON片段，也能识别出它们。
    3.  **信息量最大原则**: 通过比较大小和复杂性，选择最可能符合用户意图的那个JSON对象。
    4.  **鲁棒性**: 结合预处理和强大的修复库，最大化解析成功率。
    """
    
    # --- 阶段1: 快速通道 - 尝试直接解析 ---
    try:
        return json_repair.loads(text)
    except Exception:
        pass

    # --- 阶段2: 高级提取 - 寻找并评估候选JSON ---
    potential_json_strings = re.findall(r'(\{[\s\S]*\}|\[[\s\S]*\])', text)
    
    if not potential_json_strings:
        raise ValueError("在输入文本中未能找到任何潜在的JSON结构 (即 {...} 或 [...] 块)")

    valid_jsons = []
    for candidate in potential_json_strings:
        try:
            parsed_json = json_repair.loads(candidate)
            valid_jsons.append(parsed_json)
        except Exception:
            continue
            
    if not valid_jsons:
        raise ValueError("找到了潜在的JSON结构，但没有一个可以被成功修复和解析")

    # --- 智能决策：选择信息量最大的JSON ---
    most_significant_json = max(valid_jsons, key=lambda x: len(json.dumps(x, ensure_ascii=False)))
    
    return most_significant_json


# ==============================================================================
#  主执行函数 (全新设计，支持多输入)
# ==============================================================================
def main(**kwargs: Any) -> Dict[str, Any]:
    """
    Dify 代码执行节点主函数 - 【多输入通用版】
    
    核心功能:
    1. 接受任意数量的命名输入参数 (例如 arg1, arg2, user_profile, 等)。
    2. 对每一个输入进行智能类型检查和解析。
    3. 如果输入是字符串，则调用 _extract_most_significant_json 函数进行解析。
    4. 如果输入已经是 dict 或 list，则直接使用。
    5. 将所有成功解析的对象收集到一个字典中返回。
    6. 输出的键名会自动在原始输入键名后附加 "_obj" 后缀，方便下游引用。
    
    使用示例:
    - 输入1: key="arg1", value="{'name': 'Alice'}"
    - 输入2: key="arg2", value="[1, 2, 3]"
    - 输出: {'arg1_obj': {'name': 'Alice'}, 'arg2_obj': [1, 2, 3]}
    """
    # 最终输出的结果字典
    parsed_outputs = {}

    # kwargs 是一个字典，包含了所有在 Dify 界面上定义的输入变量
    # 例如：{'arg1': "{'name': 'value'}", 'arg2': "some other string"}
    if not kwargs:
        # 如果没有配置任何输入参数，可以返回一个空字典或提示信息
        return {"status": "No inputs provided."}

    # 遍历所有传入的参数
    for key, raw_input in kwargs.items():
        output_key = f"{key}_obj"  # 构造新的输出键名，例如 arg1 -> arg1_obj
        
        try:
            if isinstance(raw_input, (dict, list)):
                # 如果输入已经是正确的 Python 对象，直接赋值
                parsed_outputs[output_key] = raw_input
            
            elif isinstance(raw_input, str):
                if not raw_input.strip():
                    # 对空字符串进行处理，可以设置为 None 或抛出错误
                    # 这里我们选择设置为 None，下游可以判断
                    parsed_outputs[output_key] = None
                    continue # 继续处理下一个输入
                
                # 调用强大的解析函数
                parsed_object = _extract_most_significant_json(raw_input)
                parsed_outputs[output_key] = parsed_object
                
            else:
                # 对于其他意外的类型，记录一个错误或直接赋值
                # 为了健壮性，我们将其原始值放入，并可以在日志中警告
                # 或者直接抛出错误：raise TypeError(...)
                parsed_outputs[output_key] = {
                    "error": f"Unsupported input type for '{key}'",
                    "type": str(type(raw_input)),
                    "value": str(raw_input)
                }

        except Exception as e:
            # 如果在解析过程中发生任何错误，构造一个清晰的错误信息
            # 这样在 Dify 的日志中可以快速定位问题
            error_message = (
                f"Error processing input '{key}': {e}\n"
                f"Original input (first 500 chars): {str(raw_input)[:500]}..."
            )
            # 将错误信息作为输出，而不是让整个工作流失败
            parsed_outputs[output_key] = {"error": error_message}
    
    return parsed_outputs