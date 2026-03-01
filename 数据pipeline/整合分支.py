# Dify 依赖管理: json-repair (可选，但建议添加)
import json
import traceback
from typing import Any, Dict, List, Union
from copy import deepcopy

# 建议在Dify依赖中添加 json_repair
try:
    import json_repair
except ImportError:
    class JsonRepairWrapper:
        def loads(self, s: str, *args, **kwargs) -> Any:
            return json.loads(s, *args, **kwargs)
    json_repair = JsonRepairWrapper()


# --- 1. 健壮的输入解析模块 (无需修改，可复用) ---

def _parse_input_as_dict(raw_input: Any) -> Dict[str, Any]:
    """
    【目标明确】通用的、健壮的输入解析器，确保输出是一个字典。
    """
    if isinstance(raw_input, dict):
        return raw_input
    
    if isinstance(raw_input, str):
        if not raw_input.strip():
             return {} # 空字符串解析为空字典
        try:
            data = json_repair.loads(raw_input)
            if isinstance(data, dict):
                return data
            else:
                raise ValueError(f"解析出的JSON类型为 {type(data).__name__}, 但期望的是对象(dict)。")
        except Exception as e:
            raise ValueError(f"无法将输入字符串解析为对象(dict): {e}")
            
    raise TypeError(f"期望的输入类型是 dict 或其JSON字符串表示, 但收到了 {type(raw_input).__name__}")


# --- 2. Dify 节点主入口 (【核心重构】) ---

def main(base_array: Any, scraped_datas_input: Any) -> Dict[str, Any]:
    """
    Dify 主函数：
    将抓取到的数据列表（scraped_datas_input）合并到基础对象列表（base_array）中。
    """
    try:
        # 步骤 1: 解析输入
        print("▶️ [Phase 1/3] Parsing Inputs...")
        
        # 解析 base_array
        if isinstance(base_array, list):
            parsed_base_list = base_array
        elif isinstance(base_array, str):
            if not base_array.strip():
                parsed_base_list = []
            else:
                try:
                    parsed_base_list = json_repair.loads(base_array)
                except:
                    parsed_base_list = []
        else:
            parsed_base_list = []
            
        if not isinstance(parsed_base_list, list):
            # 如果解析出来不是列表（比如是单个对象），尝试转为列表
            if isinstance(parsed_base_list, dict):
                parsed_base_list = [parsed_base_list]
            else:
                parsed_base_list = []

        # 解析 scraped_datas_input
        # 这里的输入可能是 {"scraped_datas": [...]} 或 {"datas": [...]} 这种结构，也可能是直接的列表
        parsed_scraped_list = []
        if isinstance(scraped_datas_input, list):
            parsed_scraped_list = scraped_datas_input
        elif isinstance(scraped_datas_input, str):
            if not scraped_datas_input.strip():
                parsed_scraped_list = []
            else:
                try:
                    parsed_scraped_data = json_repair.loads(scraped_datas_input)
                    if isinstance(parsed_scraped_data, list):
                        parsed_scraped_list = parsed_scraped_data
                    elif isinstance(parsed_scraped_data, dict):
                        # 尝试多种可能的键名
                        if "scraped_datas" in parsed_scraped_data and isinstance(parsed_scraped_data["scraped_datas"], list):
                             parsed_scraped_list = parsed_scraped_data["scraped_datas"]
                        elif "datas" in parsed_scraped_data and isinstance(parsed_scraped_data["datas"], list):
                             parsed_scraped_list = parsed_scraped_data["datas"]
                        else:
                             parsed_scraped_list = [parsed_scraped_data]
                    else:
                         parsed_scraped_list = []
                except:
                    parsed_scraped_list = []
        elif isinstance(scraped_datas_input, dict):
            if "scraped_datas" in scraped_datas_input and isinstance(scraped_datas_input["scraped_datas"], list):
                 parsed_scraped_list = scraped_datas_input["scraped_datas"]
            elif "datas" in scraped_datas_input and isinstance(scraped_datas_input["datas"], list):
                 parsed_scraped_list = scraped_datas_input["datas"]
            else:
                 parsed_scraped_list = [scraped_datas_input]
        else:
             parsed_scraped_list = []

        print(f"  - ✅ Parsed base list: {len(parsed_base_list)} items.")
        print(f"  - ✅ Parsed scraped list: {len(parsed_scraped_list)} items.")

        # 步骤 2: 合并逻辑
        print("🔄 [Phase 2/3] Merging Data...")
        enriched_results = []
        
        # 确保两个列表长度一致，或者根据索引对应合并
        # 如果 base_list 为空，则直接使用 scraped_list 构造结果
        # 如果 scraped_list 为空，则保留 base_list
        
        max_len = max(len(parsed_base_list), len(parsed_scraped_list))
        
        for i in range(max_len):
            # 获取 base 对象
            base_obj = parsed_base_list[i] if i < len(parsed_base_list) else {}
            if not isinstance(base_obj, dict): base_obj = {}
            
            # 获取 scraped 对象
            scraped_obj = parsed_scraped_list[i] if i < len(parsed_scraped_list) else {}
            if not isinstance(scraped_obj, dict): scraped_obj = {}
            
            # 合并
            enriched_item = base_obj.copy()
            
            # 如果 scraped_obj 内部还有 scraped_datas/datas 字段（嵌套情况），尝试提取出来
            # 优先检查 scraped_datas，然后是 datas
            inner_data = None
            if "scraped_datas" in scraped_obj:
                inner_data = scraped_obj["scraped_datas"]
            elif "datas" in scraped_obj:
                inner_data = scraped_obj["datas"]
            
            if inner_data is not None:
                 # 这是一个兼容性处理，防止多层嵌套
                 if isinstance(inner_data, dict):
                     enriched_item['web_data'] = deepcopy(inner_data)
                 elif isinstance(inner_data, list) and len(inner_data) > 0:
                     enriched_item['web_data'] = deepcopy(inner_data[0])
                 else:
                     enriched_item['web_data'] = deepcopy(scraped_obj)
            else:
                 enriched_item['web_data'] = deepcopy(scraped_obj)
            
            # 移除 web_queries
            enriched_item.pop("web_queries", None)
            
            enriched_results.append(enriched_item)

        print(f"✅ Data merged successfully. Total items: {len(enriched_results)}")

        # 步骤 3: 格式化输出
        print("📦 [Phase 3/3] Formatting Output...")
        output_str = json.dumps(enriched_results, ensure_ascii=False, indent=2)
        
        return {
            "enriched_objects": enriched_results,
            "enriched_objects_str": output_str
        }

    except Exception as e:
        print(f"💥 UNEXPECTED ERROR: {e}\n{traceback.format_exc()}")
        error_payload = {
            "error": "NODE_EXECUTION_ERROR",
            "message": str(e),
            "traceback": traceback.format_exc()
        }
        return {
            "enriched_objects": [error_payload],
            "enriched_objects_str": json.dumps([error_payload], ensure_ascii=False, indent=2)
        }

