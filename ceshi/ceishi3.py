# 文件名: merge_assets_into_script.py (输出增强版)

import json
import copy
import traceback
from typing import Union, List, Dict


# 辅助函数1: 替换图片对象 (保持不变)
def _replace_image_objects(script_dict: dict, images: list) -> dict:
    if not isinstance(images, list):
        print("警告: 传入的图片数据不是一个列表，跳过图片替换流程。")
        return script_dict
    for image_data in images:
        if not isinstance(image_data, dict):
            print(f"警告: 跳过格式不正确的图片数据项: {image_data}")
            continue
        unique_id = image_data.get("id")
        image_url = image_data.get("image_url")
        if not unique_id or not image_url:
            print(f"警告: 跳过缺少'id'或'image_url'的图片数据项: {image_data}")
            continue
        try:
            path_keys = unique_id.split('.')
            parent_obj = script_dict
            for key in path_keys[:-1]:
                parent_obj = parent_obj[int(key)] if key.isdigit() else parent_obj[key]
            final_key = path_keys[-1]
            target_container = parent_obj
            original_obj = target_container[int(final_key)] if final_key.isdigit() else target_container[final_key]
            if isinstance(original_obj, dict) and original_obj.get("type") == "ai-generation-image":
                new_image_obj = {"type": "ai-generation-image", "value": image_url}
                if final_key.isdigit():
                    target_container[int(final_key)] = new_image_obj
                else:
                    target_container[final_key] = new_image_obj
            else:
                print(f"警告: 路径 '{unique_id}' 存在，但目标对象不是一个 'ai-generation-image'。跳过替换。")
        except (KeyError, IndexError, TypeError):
            print(f"警告: 无法在剧本中找到图片路径 '{unique_id}'。跳过此项。")
        except Exception as e:
            print(f"严重错误: 在替换图片路径 '{unique_id}' 时发生未知异常: {e}")
            traceback.print_exc()
    return script_dict


# 辅助函数2: 添加音频URL到字幕 (保持不变)
def _add_audio_urls_to_script(script_dict: dict, tts_results: list) -> dict:
    if not isinstance(tts_results, list):
        print("警告: 传入的TTS结果不是一个列表，跳过音频添加流程。")
        return script_dict
    audio_map = {}
    for item in tts_results:
        if isinstance(item, dict) and 'id' in item and 'audio_path' in item:
            audio_map[item['id']] = item['audio_path']
            audio_map[item['id']] = item['audio_path']
        else:
            print(f"警告: 跳过格式不正确的TTS结果项 (缺少 'id' 或 'audio_path'): {item}")
    if not audio_map:
        print("信息: 未提供有效的TTS结果，无需添加音频。")
        return script_dict
    try:
        scenes = script_dict.get('scenes')
        if not isinstance(scenes, list):
            return script_dict
        for scene in scenes:
            if not isinstance(scene, dict): continue
            subtitles = scene.get('subtitles')
            if not isinstance(subtitles, list): continue
            for subtitle_item in subtitles:
                if not isinstance(subtitle_item, dict): continue
                sub_id = subtitle_item.get('id')
                if sub_id in audio_map:
                    subtitle_item['audio_url'] = audio_map[sub_id]
    except Exception as e:
        print(f"严重错误: 在添加audio_url到字幕时发生未知异常: {e}")
        traceback.print_exc()
    return script_dict


# ----------------- 主函数: 工作流节点入口 (已修改) -----------------
def main(
        original_script_input: Union[str, Dict],
        generated_images_input: Union[str, Dict, List],
        model_id: str,  # 【修改点 1】: 增加 model_id 作为新的输入参数
        copyright_text: str
) -> dict:
    """
    主函数，协调处理图片、音频和配置(model_id)的整合，并始终返回统一的对象结构。
    """
    # 1. 健壮地解析所有输入
    try:
        if isinstance(original_script_input, str):
            first_pass = json.loads(original_script_input)
            if isinstance(first_pass, dict) and "original_script_string" in first_pass:
                original_script = json.loads(first_pass["original_script_string"])
            else:
                original_script = first_pass
        else:
            original_script = original_script_input
        if isinstance(generated_images_input, str):
            images_data = json.loads(generated_images_input)
        else:
            images_data = generated_images_input
        generated_images = images_data.get("updated_array") if isinstance(images_data,
                                                                          dict) and "updated_array" in images_data else images_data
        # tts_results = json.loads(tts_results_input) if isinstance(tts_results_input, str) else tts_results_input
    except json.JSONDecodeError as e:
        error_msg = f"输入解析失败 (JSONDecodeError): {e}"
        print(f"错误: {error_msg}")
        return {"error": error_msg, "final_script_str": None}
    except Exception as e:
        error_msg = f"加载输入时发生未知错误: {e}"
        print(f"错误: {error_msg}")
        return {"error": error_msg, "final_script_str": None}
    final_script = copy.deepcopy(original_script)
    # 【修改点 2】: 在处理流程的早期，更新或添加 model_id
    if model_id and isinstance(model_id, str):
        print(f"--- 开始更新/添加音频模型ID: {model_id} ---")
        try:
            # 确保 config 对象存在且为字典，如果不存在则创建它
            if not isinstance(final_script.get('config'), dict):
                print("警告: 剧本中缺少 'config' 字典或格式不正确，将自动创建。")
                final_script['config'] = {}

            # 设置 model_id
            final_script['config']['model_id'] = model_id
            print("--- 音频模型ID更新完成 ---")
        except Exception as e:
            # 即使这里出错，也只打印警告，不中断整个脚本流程
            print(f"警告: 在更新 model_id 时发生非预期错误: {e}")
    else:
        print("信息: 未提供有效的 model_id 输入，跳过 config 更新。")
        # 【修改点 3】: 在处理流程的早期，更新或添加 copyright_text
    if copyright_text and isinstance(copyright_text, str):
        print(f"--- 开始更新/添加水印: {copyright_text} ---")
        try:
            # 确保 config 对象存在且为字典，如果不存在则创建它
            if not isinstance(final_script.get('config'), dict):
                print("警告: 剧本中缺少 'config' 字典或格式不正确，将自动创建。")
                final_script['config'] = {}

            # 设置 copyright_text
            final_script['config']['copyright_text'] = copyright_text
            print("--- 水印更新完成 ---")
        except Exception as e:
            # 即使这里出错，也只打印警告，不中断整个脚本流程
            print(f"警告: 在更新 copyright_text 时发生非预期错误: {e}")
    else:
        print("信息: 未提供有效的 copyright_text 输入，跳过 config 更新。")
    # 2. 调用辅助函数，按顺序处理
    print("\n--- 开始替换图片URL ---")
    final_script = _replace_image_objects(final_script, generated_images)
    print("--- 图片URL替换完成 ---")
    print("\n--- 开始添加音频URL ---")
    # final_script = _add_audio_urls_to_script(final_script, tts_results)
    print("--- 音频URL添加完成 ---")
    # 3. 准备并返回最终结果
    try:
        final_script_str = json.dumps(final_script, ensure_ascii=False, indent=2)
        return {"error": None, "final_script_str": final_script_str}
    except TypeError as e:
        error_msg = f"序列化最终剧本为JSON字符串时失败: {e}"
        print(f"严重错误: {error_msg}")
        return {"error": error_msg, "final_script_str": None}