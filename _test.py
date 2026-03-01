import requests
import time
import concurrent.futures
import uuid
import json
from typing import Dict, Any, List
from tqdm import tqdm

# --- [ 配置区 ] ---

# 你的 FastAPI 服务地址和端口
API_ENDPOINT = "http://127.0.0.1:2906/api/generate_audio"

# --- [ 你需要在这里填写的参数 ] ---
# 这是每个并发请求都会使用的 "模板" Payload
# 'workflow_id' 会被脚本动态替换
REQUEST_PAYLOAD_TEMPLATE = {
    "raw_script": """
    # 主题：深入理解数据结构：二叉树入门\n\n# 核心任务\n根据以下详尽的导演执行手册，生成一个单一、自包含的HTML文件，渲染一个专业、清晰、以教学效果为核心的伪视频。\n\n# **1. 全局艺术与动画导演阐述 (Global Art & Animation Direction)**\n* **艺术风格**: 现代、简洁、科技感。主色调采用深邃的科技蓝 (`#0D1B2A`) 与中性灰 (`#E0E0E0`)，强调色使用充满活力的科技绿 (`#00F5D4`)。所有图表和UI元素都应保持干净、一致的视觉风格，避免不必要的装饰。\n* **字体规范 (Typography)**: 标题使用无衬线粗体 (如 Inter Bold)，正文使用常规体 (Inter Regular)。确保所有文本清晰易读，对比度高。\n\n# 2. 播放器样式规范 (Player Style Specification)\n- **目标**: 为标准化的播放器定义视觉风格。\n- **进度条颜色**:\n    - **已播放部分**: `#00F5D4`\n    - **背景部分**: `#333333`\n- **控制按钮 (播放/暂停/重播)**:\n    - **图标颜色**: `#FFFFFF`\n    - **背景色**: `transparent`\n- **时间显示文本**:\n    - **颜色**: `#E0E0E0`\n    - **字体**: `14px`\n- **字幕**:\n    - **文字颜色**: `#FFFFFF`\n    - **背景色**: `rgba(0, 0, 0, 0.6)`\n\n# **3. 叙事字幕脚本 (Narrative Subtitle Script)**\n**字幕脚本**\n0.1秒 - 7.0秒: 欢迎学习二叉树！本节课，你将了解二叉树的核心定义、关键术语，并掌握其与线性结构的区别。\n7.1秒 - 18.0秒: 什么是二叉树？它是一种非线性的树形数据结构，每个节点最多有两个子节点，称为左孩子和右孩子。\n18.1秒 - 30.0秒: 让我们来认识一下二叉树的家庭成员：根节点是树的起点，没有子节点的叫叶子节点。\n30.1秒 - 42.0秒: 二叉树也有不同形态。比如“满二叉树”，所有节点要么是叶子，要么有两个孩子；还有“完全二叉树”，除了最后一层，所有层都是满的。\n42.1秒 - 55.0秒: 遍历是二叉树最核心的操作之一，它能让我们不重不漏地访问所有节点。例如“中序遍历”，它会按“左-根-右”的顺序访问。\n55.1秒 - 65.0秒: 二叉树无处不在！从你的电脑文件系统，到编译器的语法分析，再到数据压缩算法，都有它的身影。\n65.1秒 - 70.0秒: 恭喜你完成了二叉树入门！下节课，我们将深入学习一种特殊的二叉树——二叉搜索树。\n\n**详细分镜脚本**\n**初始状态**\n封面展示： 包含高质量背景图 [科技感的抽象数据网络:image]（https://media.geeksforgeeks.org/wp-content/uploads/20240811023816/Introduction-to-Binary-Tree.webp）、主标题“深入理解数据结构：二叉树入门”、副标题“掌握计算机科学的基石”，以及带悬浮动画的中心播放按钮和“created by X-Pilot”标识。\n**点击播放后**\n视觉: 过渡动画：封面平滑淡出。视频开始自动播放\n文本: 无\n\n**0.1秒 - 7.0秒**\n字幕: <subtitle>欢迎学习二叉树！本节课，你将了解二叉树的核心定义、关键术语，并掌握其与线性结构的区别。</subtitle>\n- **场景类型**: 学习目标展示 (Learning Objectives)\n- **核心信息**: 清晰列出本视频的核心学习目标。\n- **视觉建议**:\n  - **布局**: 居中列表布局。\n  - **元素**:\n    - 一个代表“目标”的图标 (如 `target`)，颜色为 `#00F5D4`。\n    - 主标题：“本节学习目标”。\n    - 项目符号列表，逐条展示目标：“1. 定义二叉树及其优势”、“2. 识别核心术语（根、叶、节点）”、“3. 理解并区分不同类型的二叉树”。\n  - **动画**: 整体采用自下而上的淡入动画 (fadeInUp)，列表项有0.2秒的延迟，形成交错感。\n\n**7.1秒 - 18.0秒**\n字幕: <subtitle>什么是二叉树？它是一种非线性的树形数据结构，每个节点最多有两个子节点，称为左孩子和右孩子。</subtitle>\n- **场景类型**: 核心概念对比 (Core Concept & Comparison)\n- **核心信息**: 定义二叉树，并将其与线性结构（数组、链表）进行对比，突出其优势。\n- **视觉建议**:\n  - **布局**: 三列对比布局。\n  - **元素**:\n    - **左列**: 卡片标题“数组”，图标`array`，描述“访问快，增删慢”。\n    - **中列**: 卡片标题“链表”，图标`link`，描述“增删快，访问慢”。\n    - **右列**: 卡片标题“二叉树”，图标`git-merge`，描述“兼具快速访问与增删的潜力”，背景色用强调色 `#00F5D4` 的半透明版突出。\n  - **动画**: 三个卡片从底部依次滑入，右侧的二叉树卡片最后出现并有轻微的放大效果。\n\n**18.1秒 - 30.0秒**\n字幕: <subtitle>让我们来认识一下二叉树的家庭成员：根节点是树的起点，没有子节点的叫叶子节点。</subtitle>\n- **场景类型**: 术语图解 (Terminology Diagram)\n- **核心信息**: 通过一张带标注的图，清晰解释二叉树的各个组成部分。\n- **视觉建议**:\n  - **布局**: 全屏图示。\n  - **元素**:\n    - 背景是一张清晰的、标注了各项术语的二叉树结构图 [二叉树术语图解:image]（https://media.geeksforgeeks.org/wp-content/uploads/20240808120231/Terminologies-in-Binary-Tree-in-Data-Structure_1.webp）。\n    - 动画效果：图先出现，然后“Root”, “Parent”, “Child”, “Leaf”, “Internal Node”等标签和指示线依次、动态地浮现，引导观众视线。\n  - **动画**: 标签以“弹出” (pop-up) 效果出现。**备选方案**: 如果无法实现动态标签，则使用已包含所有标签的静态图片，但用一个动态的高亮圆圈依次扫过每个术语及其对应部分。\n\n**30.1秒 - 42.0秒**\n字幕: <subtitle>二叉树也有不同形态。比如“满二叉树”，所有节点要么是叶子，要么有两个孩子；还有“完全二叉树”，除了最后一层，所有层都是满的。</subtitle>\n- **场景类型**: 类型对比 (Types Comparison)\n- **核心信息**: 并列展示几种典型的二叉树类型，帮助学习者直观区分。\n- **视觉建议**:\n  - **布局**: 三列并排卡片。\n  - **元素**:\n    - **左卡片**: 标题“满二叉树 (Full)”，下方是其结构图 [满二叉树示例:image]（https://media.geeksforgeeks.org/wp-content/uploads/20221125111700/full.png）。\n    - **中卡片**: 标题“完全二叉树 (Complete)”，下方是其结构图 [完全二叉树示例:image]（https://media.geeksforgeeks.org/wp-content/uploads/20221130172411/completedrawio.png）。\n    - **右卡片**: 标题“完美二叉树 (Perfect)”，下方是其结构图 [完美二叉树示例:image]（https://media.geeksforgeeks.org/wp-content/uploads/20221124094547/perfect.png）。\n  - **动画**: 三张卡片从屏幕中心向外展开，并列排布。\n\n**42.1秒 - 55.0秒**\n字幕: <subtitle>遍历是二叉树最核心的操作之一，它能让我们不重不漏地访问所有节点。例如“中序遍历”，它会按“左-根-右”的顺序访问。</subtitle>\n- **场景类型**: 动画演示 (Animated Demonstration)\n- **核心信息**: 动态演示“中序遍历” (Inorder Traversal) 的过程。\n- **视觉建议**:\n  - **布局**: 全屏动画。\n  - **元素**:\n    - 一个清晰的二叉树图例 [用于遍历演示的二叉树:image]（https://www.freecodecamp.org/news/content/images/2022/02/ex-binary-search-tree.png）。\n    - 屏幕底部有一个“遍历结果”的空序列框。\n    - 动画开始，一个高亮光标（颜色为 `#00F5D4`）按照“左-根-右”的顺序在节点间移动。每当“访问”到一个节点（光标停留在节点上），该节点的数值就会被复制并飞入下方的结果序列框中。\n    - 最终结果序列应为：**D, B, E, A, F, C, G**\n  - **动画**: 光标移动平滑，数字飞入序列框的动画要清晰利落。**备选方案**: 如果动画复杂，则简化为：节点按遍历顺序依次改变颜色（高亮），同时其数值直接出现在下方的结果序列中。\n\n**55.1秒 - 65.0秒**\n字幕: <subtitle>二叉树无处不在！从你的电脑文件系统，到编译器的语法分析，再到数据压缩算法，都有它的身影。</subtitle>\n- **场景类型**: 应用场景展示 (Applications Showcase)\n- **核心信息**: 展示二叉树的真实世界应用，提升学习兴趣。\n- **视觉建议**:\n  - **布局**: 居中的图标网格。\n  - **元素**:\n    - **中心标题**: “二叉树的应用”。\n    - 四个带图标和简短文字的卡片环绕标题出现：\n      - `folder-open` 图标: \"文件系统\"\n      - `code` 图标: \"表达式求值\"\n      - `compress` 图标: \"数据压缩 (霍夫曼编码)\"\n      - `brain-circuit` 图标: \"AI决策树\"\n  - **动画**: 四个应用卡片从中心标题处向四周飞出并定位。\n\n**65.1秒 - 70.0秒**\n字幕: <subtitle>恭喜你完成了二叉树入门！下节课，我们将深入学习一种特殊的二叉树——二叉搜索树。</subtitle>\n- **场景类型**: 总结与预告 (Summary & Next Up)\n- **核心信息**: 快速回顾并预告下一课内容。\n- **视觉建议**:\n  - **布局**: 居中。\n  - **元素**:\n    - **标题**: \"本节回顾\"\n    - **清单**: √ 核心定义 √ 关键术语 √ 主要类型 √ 遍历操作 √ 实际应用\n    - **下方**: \"下节预告：性能优化的关键——二叉搜索树 (BST)\"\n  - **动画**: 清单项逐条打勾出现，然后下节预告文字淡入。\n\n**视频结束场景**\n显示背景，使用动画元素编排一个**“本节回顾”**的关键知识点总结列表，并最终定格在【Video created by X-Pilot】。
    """,
    "model_id": "5c353fdb312f4888836a9a5680099ef0",  # 替换成你的模型ID
    "fish_api_key": "dae51de32a0743f6b4f2f7b6366747bf"  # 替换成你的Fish Audio API Key
}
# --- [ 压力测试参数 ] ---
# 并发线程数 (模拟同时有多少个用户在请求)
CONCURRENT_WORKERS = 20

# 每个线程发起的总请求数
REQUESTS_PER_WORKER = 1

# 计算得出的总请求数
TOTAL_REQUESTS = CONCURRENT_WORKERS * REQUESTS_PER_WORKER


# --- [ 脚本主体 ] ---

class PressureTester:
    def __init__(self, endpoint: str, payload_template: Dict[str, Any], workers: int, total_requests: int):
        self.endpoint = endpoint
        self.payload_template = payload_template
        self.workers = workers
        self.total_requests = total_requests
        self.results: List[Dict[str, Any]] = []
        self.success_count = 0
        self.failure_count = 0

    def _send_request(self, pbar: tqdm) -> None:
        """发送单个HTTP请求并记录结果"""
        start_time = time.time()

        # 动态生成 workflow_id
        payload = self.payload_template.copy()
        payload['workflow_id'] = f"test-{uuid.uuid4()}"

        response_data = {}
        try:
            response = requests.post(self.endpoint, json=payload, timeout=300)  # 设置一个较长的超时时间
            duration = time.time() - start_time

            response_data = {
                "status_code": response.status_code,
                "duration_ms": round(duration * 1000, 2),
                "workflow_id": payload['workflow_id'],
                "error": None
            }

            if response.status_code == 200:
                # 进一步检查返回的业务逻辑是否成功
                response_json = response.json()
                has_errors_in_tasks = any(task.get("error") for task in response_json.get("audio_tasks", []))
                if has_errors_in_tasks or response_json.get("move_operation_error"):
                    response_data["error"] = f"Business logic error: {response_json}"
                    self.failure_count += 1
                else:
                    self.success_count += 1
            else:
                response_data["error"] = f"HTTP Error: {response.text}"
                self.failure_count += 1

        except requests.exceptions.RequestException as e:
            duration = time.time() - start_time
            response_data = {
                "status_code": None,
                "duration_ms": round(duration * 1000, 2),
                "workflow_id": payload['workflow_id'],
                "error": str(e)
            }
            self.failure_count += 1

        finally:
            self.results.append(response_data)
            pbar.update(1)  # 更新进度条

    def run(self):
        """执行压力测试"""
        print("--- [ 压力测试开始 ] ---")
        print(f"API 端点: {self.endpoint}")
        print(f"并发数 (Workers): {self.workers}")
        print(f"总请求数: {self.total_requests}")
        print("-" * 30)

        start_total_time = time.time()

        with tqdm(total=self.total_requests, desc="Testing Progress") as pbar:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
                # 提交所有任务到线程池
                futures = [executor.submit(self._send_request, pbar) for _ in range(self.total_requests)]

                # 等待所有任务完成
                concurrent.futures.wait(futures)

        end_total_time = time.time()
        self.total_duration_sec = end_total_time - start_total_time

        self.print_summary()

    def print_summary(self):
        """打印测试结果摘要"""
        print("\n--- [ 压力测试完成 ] ---\n")

        print("--- [ 整体统计 ] ---")
        print(f"总耗时: {self.total_duration_sec:.2f} 秒")
        print(f"总请求数: {self.total_requests}")
        print(f"成功请求: {self.success_count}")
        print(f"失败请求: {self.failure_count}")

        if self.total_requests > 0:
            success_rate = (self.success_count / self.total_requests) * 100
            print(f"成功率: {success_rate:.2f}%")

        if self.total_duration_sec > 0:
            qps = self.total_requests / self.total_duration_sec
            print(f"QPS (每秒请求数): {qps:.2f}")

        successful_requests = [r for r in self.results if r["error"] is None]
        if successful_requests:
            durations = [r["duration_ms"] for r in successful_requests]
            avg_duration = sum(durations) / len(durations)
            max_duration = max(durations)
            min_duration = min(durations)
            print(f"\n--- [ 成功请求性能 ] ---")
            print(f"平均响应时间: {avg_duration:.2f} ms")
            print(f"最快响应时间: {min_duration:.2f} ms")
            print(f"最慢响应时间: {max_duration:.2f} ms")

        failed_requests = [r for r in self.results if r["error"] is not None]
        if failed_requests:
            print(f"\n--- [ 失败请求详情 (最多显示5条) ] ---")
            for i, result in enumerate(failed_requests[:5]):
                print(f"  {i + 1}. WorkflowID: {result['workflow_id']}")
                print(f"     Status Code: {result['status_code']}")
                print(f"     Error: {result['error'][:200]}...")  # 截断过长的错误信息

        print("\n--- [ 测试结束 ] ---")


def main():
    # 验证模板 payload 是否完整
    if "your_model_id" in REQUEST_PAYLOAD_TEMPLATE.values() or \
            "your_fish_api_key" in REQUEST_PAYLOAD_TEMPLATE.values():
        print("!!! [错误] !!! 请在脚本中填写真实的 'model_id' 和 'fish_api_key'。")
        return

    tester = PressureTester(
        endpoint=API_ENDPOINT,
        payload_template=REQUEST_PAYLOAD_TEMPLATE,
        workers=CONCURRENT_WORKERS,
        total_requests=TOTAL_REQUESTS
    )
    tester.run()


if __name__ == "__main__":
    main()
