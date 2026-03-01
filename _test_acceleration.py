import requests
import time
import uuid
import json
import os
import re
from pydub import AudioSegment

# --- [ 1. 请在这里配置你的参数 ] ---

# 你的 Fish Audio API Key
FISH_API_KEY = "dae51de32a0743f6b4f2f7b6366747bf"  # <--- 请替换成你的真实API Key

# 你要使用的TTS模型ID
MODEL_ID = "5c353fdb312f4888836a9a5680099ef0"  # <--- 请替换成你的模型ID

# Fish Audio TTS API 的端点
FISH_API_ENDPOINT = "https://api.fish.audio/v1/tts"

# 用于测试的原始脚本 (包含时间和文本)
RAW_SCRIPT = """
# 主题：微积分之核：导数的诞生\n\n# 核心任务\n根据以下详尽的设计蓝图，生成一个单一、完整、自包含的HTML文件。此文件将渲染一个具有丰富视觉层次、精确动画同步和深度信息的和真正播放视频完全一致效果的伪视频风格。\n\n# **1. 全局设计规范 (Global Design Specification)**\n* **技术准则**:\n    * **自包含**: 最终的HTML文件必须包含所有运行所需的CSS和JavaScript。\n    * **资源引用**: 图片、字体等外部资源必须通过标准的HTTPS链接进行引用。\n    * **响应式设计**: 确保在桌面和移动设备上都有良好的观看体验。\n* **艺术风格**: \n    * 整体采用深邃的暗色调背景（如#0A0A1A），营造出宇宙或数字空间的神秘感与科技感。\n    * 核心视觉元素（如图形、线条、公式）使用明亮、发光的霓虹色（如青色#00FFFF、品红#FF00FF、亮黄色#FFFF00），形成强烈对比，突出知识重点。\n    * 动画效果要求流畅、优雅，多使用缓动函数（ease-in-out），避免生硬的线性运动。\n* **字体规范 (Typography)**: \n    * **主标题/关键信息**: 使用 \"Exo 2\" 或类似的科技感无衬线字体，加粗。\n    * **字幕/正文**: 使用 \"Lato\" 或 \"Open Sans\"，保证清晰易读。\n    * **数学公式**: 使用 \"KaTeX\" 或 \"MathJax\" 库进行渲染，或使用具有良好数学符号支持的等宽字体如 \"Fira Code\"。\n\n# **2. 播放器与封面 (Player & Cover)**\n* **播放器**: 实现一个功能完整的底部播放器，包括播放/暂停按钮、可拖拽的进度条，外观简洁。进度条和按钮在鼠标悬停时应有微光效果，与整体霓虹风格保持一致。\n* **封面**:\n    * **背景图**: [深邃的数学曲线背景：image]（https://images.unsplash.com/photo-1509233725247-49e657c54213?q=80&w=1949&auto=format&fit=crop）\n    * **主标题**: \"微积分之核\" (使用主标题字体，带有轻微的霓虹发光效果)\n    * **副标题**: \"导数的诞生\" (字体稍小，位于主标题下方)\n    * **播放按钮**: 屏幕中心一个大的、半透明的圆形播放按钮，内部的三角形播放图标有呼吸式发光动画。\n    * **署名**: 播放按钮下方小字显示 \"Created by X-Pilot\"。\n\n# **3. 字幕与视频脚本 ( Video&Subtitle Script)** \n## 字幕脚本\n0.1秒 - 8.0秒: 我们如何描述变化？比如，一辆赛车在赛道上飞驰，它的速度在每一瞬间都在改变。\n8.1秒 - 17.0秒: 我们可以计算它在一段时间内的平均速度，这很简单，就是位移除以时间。\n17.1秒 - 29.0秒: 在数学上，这就像是在函数图像上取两个点，画一条连接它们的直线，也就是割线。这条割线的斜率，就代表了平均变化率。\n29.1秒 - 42.0秒: 但我们真正想知道的，是赛车在“某一瞬间”的速度。如何捕捉这个“瞬间”？\n42.1秒 - 55.0秒: 答案是：让两个点无限地靠近。当时间间隔趋近于零，割线就变成了那一点的切线。\n55.1秒 - 68.0秒: 这条切线的斜率，就是函数在该点的瞬时变化率。这个值，我们称之为“导数”。\n68.1秒 - 75.0秒: 导数，就是描述任何系统在特定瞬间变化快慢的通用语言。从物理到金融，无处不在。\n\n## 详细分镜脚本\n### 初始状态\n封面展示： 包含高质量背景图 [深邃的数学曲线背景：image]（https://images.unsplash.com/photo-1509233725247-49e657c54213?q=80&w=1949&auto=format&fit=crop），主标题“微积分之核”，副标题“导数的诞生”，以及中心带有悬浮动画的播放按钮，按钮下方小字 \"Created by X-Pilot\"。\n点击播放后\n视觉: 过渡动画：封面元素（标题、按钮）优雅地淡出，背景图模糊并平滑过渡到视频的深色背景。视频开始自动播放。\n文本: 无\n\n### 0.1秒 - 8.0秒\n**字幕**: 我们如何描述变化？比如，一辆赛车在赛道上飞驰，它的速度在每一瞬间都在改变。\n**视频**: 场景从一个纯黑的背景开始。一束霓虹青色的光线从左侧平滑地划入屏幕，形成一条优美的、非线性的曲线，代表赛车的行驶轨迹或一个抽象函数 `y=f(x)`。这条曲线从左到右缓慢绘制出来，仿佛时间在流逝。当曲线绘制过半时，一个发光的点（代表赛车）出现在曲线上，并沿着曲线加速、减速地移动，视觉化地表现出“速度在每一瞬间都在改变”。整个场景富有动感，但节奏舒缓，旨在引发观众的思考，将现实世界的问题与抽象的数学曲线联系起来。\n\n### 8.1秒 - 17.0秒\n**字幕**: 我们可以计算它在一段时间内的平均速度，这很简单，就是位移除以时间。\n**视频**: 赛车光点消失。在之前绘制的曲线上，两个新的、明亮的品红色光点 P 和 Q 出现，它们之间有明显的距离。一条霓虹黄色的直线（割线）动态地连接 P 和 Q。同时，从 P 点和 Q 点分别向 X 轴和 Y 轴投射出虚线，在坐标轴上标记出 `x1`, `x2` 和 `y1`, `y2`。屏幕一侧，公式 `(y2 - y1) / (x2 - x1)` 以打字机效果逐字出现，并伴随轻微的发光。这个动画清晰地将“平均速度”这个物理概念，与“割线斜率”这个几何概念对应起来，通过标记和公式，为观众建立起初步的数学模型。\n\n### 17.1秒 - 29.0秒\n**字幕**: 在数学上，这就像是在函数图像上取两个点，画一条连接它们的直线，也就是割线。这条割线的斜率，就代表了平均变化率。\n**视频**: 镜头缓缓推近，聚焦于 P、Q 两点和它们之间的割线。坐标轴和标记暂时淡出，以减少干扰。当字幕提到“割线”时，这条黄色的线闪烁一下以示强调。接着，屏幕右侧出现一个文本框，标题为“平均变化率”，下方是之前出现的斜率公式。整个场景的视觉焦点都集中在 [函数曲线上的割线：image]（https://upload.wikimedia.org/wikipedia/commons/thumb/c/c5/Secant_line.svg/800px-Secant_line.svg.png） 的核心概念上。动画应平滑、清晰，帮助观众理解割线斜率就是两个独立观测点之间的平均变化情况，为后续引入“瞬时”概念做好铺垫。\n\n### 29.1秒 - 42.0秒\n**字幕**: 但我们真正想知道的，是赛车在“某一瞬间”的速度。如何捕捉这个“瞬间”？\n**视频**: 场景发生戏剧性变化。文本框和公式淡出。镜头重新拉远，我们看到完整的曲线。点 P 保持不动，而点 Q 开始沿着曲线平滑地向 P 点滑动。随着 Q 的移动，连接 P、Q 的黄色割线也随之实时改变其角度，像一根围绕 P 点摆动的指针。这个过程需要用流畅的动画来表现，展示割线斜率的连续变化。当字幕问出“如何捕捉这个‘瞬间’？”时，点 Q 的移动速度减慢，停在离 P 很近但仍有微小距离的地方，割线也随之定格，营造出一种悬念感，引导观众思考极限的概念。\n\n### 42.1秒 - 55.0秒\n**字幕**: 答案是：让两个点无限地靠近。当时间间隔趋近于零，割线就变成了那一点的切线。\n**视频**: 这是整个视频的视觉高潮。点 Q 继续向 P 点移动，最终与 P 点完全重合。在这个重合的瞬间，黄色的割线平滑地、无缝地变换成一条新的、代表最终位置的青色直线——切线。这个变换过程是关键，必须做到极致流畅。切线只在 P 点这一个点上接触曲线。屏幕上出现一个视觉特效，仿佛一个能量波从 P 点扩散开来，强调这一转变的重要性。同时，屏幕一侧出现极限符号 `lim (Q→P)` 的动画，与 Q 点的移动同步，直观地展示了极限过程。这个场景的核心是展示 [曲线的切线：image]（https://upload.wikimedia.org/wikipedia/commons/thumb/1/15/Tangent_to_a_curve.svg/800px-Tangent_to_a_curve.svg.png）是如何从割线演变而来的。\n\n### 55.1秒 - 68.0秒\n**字幕**: 这条切线的斜率，就是函数在该点的瞬时变化率。这个值，我们称之为“导数”。\n**视频**: 场景稳定下来，只留下曲线、P 点和那条青色的切线。之前用于计算割线斜率的公式 `(y2 - y1) / (x2 - x1)` 再次出现，但这次它的前面被加上了极限符号 `lim (x2→x1)`。然后，整个公式优雅地变形、化简，最终变成导数的经典定义 `f'(x)`。这个变形过程需要精心设计的动画，让观众理解两个公式之间的联系。最后，“导数” (Derivative) 这个词以醒目的主标题字体出现在屏幕中央，并伴有发光效果，将整场讲解的核心概念正式命名并烙印在观众心中。\n\n### 68.1秒 - 75.0秒\n**字幕**: 导数，就是描述任何系统在特定瞬间变化快慢的通用语言。从物理到金融，无处不在。\n**视频**: 初始的曲线和切线背景逐渐模糊淡化。屏幕上开始浮现出多个代表不同领域的微小动态图标或关键词，如代表物理的“速度/加速度”、代表金融的“增长率”、代表工程的“应力变化”等。这些元素围绕着中心“导数” `f'(x)` 的符号旋转，形成一个星云般的结构。这个场景通过丰富的视觉元素，将导数的应用从单一的数学概念扩展到更广阔的现实世界，强调其普适性和重要性，给观众留下深刻印象。\n\n### 视频结束场景\n所有图标和文字淡出，背景回归纯黑。屏幕中央用优雅的动画效果依次浮现文字：“Video created by X-Pilot”，字体发光，最终定格2-3秒后，整个画面淡出。
"""

# --- [ 2. pydub 处理配置 ] ---
OUTPUT_DIR = "audio_comparison_direct"
NORMAL_FILENAME = "final_normal_with_gaps.mp3"
ACCELERATED_FILENAME = "final_accelerated_gaps.mp3"
SILENCE_ACCELERATION_FACTOR = 2.0  # 静音部分的加速倍率 (2.0 表示将静音时长减半)


# --- [ 3. 脚本主逻辑 (无需修改) ] ---

def parse_time_range(time_range_str):
    """从'X.X秒 - Y.Y秒'格式的字符串中解析出开始和结束时间(浮点数)"""
    try:
        # 使用正则表达式匹配数字，更稳健
        matches = re.findall(r"(\d+\.?\d*)", time_range_str)
        if len(matches) == 2:
            return float(matches[0]), float(matches[1])
        else:
            raise ValueError("时间范围格式不正确")
    except (ValueError, IndexError) as e:
        print(f"[!] 无法解析时间范围 '{time_range_str}': {e}")
        return None, None


def main():
    print("--- [ 独立音频效果对比脚本启动 ] ---")
    if "xxxxxxxx" in FISH_API_KEY or "your-tts-model-id" in MODEL_ID:
        print("\n!!! 警告: 请先在脚本顶部配置你的 API Key 和 Model ID !!!\n")
        return

    # 准备工作
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    script_data = json.loads(RAW_SCRIPT)
    audio_tasks = []

    # 步骤 1: 直接调用Fish Audio API生成所有音频片段
    print(f"\n[步骤 1/{len(script_data) + 2}] 正在生成和下载所有音频片段...")
    headers = {"api-key": FISH_API_KEY}

    for i, item in enumerate(script_data):
        text = item["text"]
        time_range = item["time_range"]
        start_sec, end_sec = parse_time_range(time_range)

        if start_sec is None:
            continue

        print(f"  > 正在生成第 {i + 1} 段: \"{text[:20]}...\"")
        payload = {"text": text, "model_id": MODEL_ID}

        try:
            response = requests.post(FISH_API_ENDPOINT, headers=headers, json=payload, timeout=60)
            response.raise_for_status()

            # 直接将返回的音频内容写入文件
            local_path = os.path.join(OUTPUT_DIR, f"segment_{i + 1}.mp3")
            with open(local_path, 'wb') as f:
                f.write(response.content)

            audio_tasks.append({
                "id": i,
                "local_path": local_path,
                "start_sec": start_sec,
                "end_sec": end_sec,
            })
            print(f"    - 已保存到 {local_path}")
            time.sleep(1)  # 礼节性停顿，避免请求过于频繁

        except requests.exceptions.RequestException as e:
            print(f"  [!] 生成第 {i + 1} 段时失败: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"    - 响应内容: {e.response.text}")
            print("    - 跳过此片段。")
            continue

    if not audio_tasks:
        print("\n[!] 没有成功生成任何音频片段，无法继续。请检查API Key和模型ID。")
        return

    # 步骤 2: 生成“原始”版本 (带完整静音间隙)
    print(f"\n[步骤 {len(audio_tasks) + 1}/{len(script_data) + 2}] 正在生成原始版本: {NORMAL_FILENAME}...")
    normal_assembly = AudioSegment.empty()
    last_end_time_ms = 0
    for task in sorted(audio_tasks, key=lambda x: x['id']):
        gap_duration_ms = int((task['start_sec'] * 1000) - last_end_time_ms)
        if gap_duration_ms > 0:
            normal_assembly += AudioSegment.silent(duration=gap_duration_ms)

        clip = AudioSegment.from_file(task['local_path'])
        normal_assembly += clip
        last_end_time_ms = int(task['end_sec'] * 1000)

    normal_output_path = os.path.join(OUTPUT_DIR, NORMAL_FILENAME)
    normal_assembly.export(normal_output_path, format="mp3")
    print(f"  > ✅ 原始版本已保存到: {normal_output_path}")

    # 步骤 3: 生成“加速”版本 (压缩静音间隙)
    print(f"\n[步骤 {len(audio_tasks) + 2}/{len(script_data) + 2}] 正在生成加速版本: {ACCELERATED_FILENAME}...")
    accelerated_assembly = AudioSegment.empty()
    last_end_time_ms = 0
    for task in sorted(audio_tasks, key=lambda x: x['id']):
        gap_duration_ms = int((task['start_sec'] * 1000) - last_end_time_ms)
        if gap_duration_ms > 0:
            accelerated_gap_duration_ms = int(gap_duration_ms / SILENCE_ACCELERATION_FACTOR)
            accelerated_assembly += AudioSegment.silent(duration=accelerated_gap_duration_ms)

        clip = AudioSegment.from_file(task['local_path'])
        accelerated_assembly += clip
        last_end_time_ms = int(task['end_sec'] * 1000)

    accelerated_output_path = os.path.join(OUTPUT_DIR, ACCELERATED_FILENAME)
    accelerated_assembly.export(accelerated_output_path, format="mp3")
    print(f"  > ✅ 加速版本已保存到: {accelerated_output_path}")

    print("\n--- [ 测试完成 ] ---")
    print("请在你喜欢的播放器中打开以下两个文件进行对比：")
    print(f"  1. 原始文件: {os.path.abspath(normal_output_path)}")
    print(f"  2. 加速文件: {os.path.abspath(accelerated_output_path)}")


if __name__ == "__main__":
    main()
