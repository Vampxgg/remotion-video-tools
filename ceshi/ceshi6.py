# -*- coding: utf-8 -*-
# @File：ceshi6.py
# @Time：2025/9/29 17:40
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com
import requests
import time
import json
import random
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np  # 用于计算百分位数，可选

# ============================ 配置区域 START ============================

# API 服务配置
API_HOST = "http://35.232.154.66:5125"
API_BASE_URL = f"{API_HOST}/v1"
API_KEY = "app-xABhiZILiP5ie76QWyqdLDR3"
ENDPOINT = "/chat-messages"

# 并发测试配置
TOTAL_REQUESTS = 50  # 总共要发送的请求数量
MAX_WORKERS = 50  # 最大并发线程数 (模拟的并发用户数)

# 请求内容配置
RESPONSE_MODE = "streaming"  # "blocking" 或 "streaming"
QUERIES = [  # 将从这个列表中随机选择问题 (query)
    "你好，请做个自我介绍。",
    "今天天气怎么样？",
    "给我讲一个关于太空旅行的简短故事。",
    "What is the capital of France?",
    "Explain the theory of relativity in simple terms.",
    "写一首关于秋天的诗。",
    "如何制作一杯美味的卡布чино？",
    "推荐三部科幻电影。",
]

# --- 新增配置：为 'inputs' 字段提供动态数据 ---
# 根据您提供的图片，为 inputs 中的变量准备一些随机数据
TOPICS = [
    "Onboarding: A Guide to Our Company's Remote Work Tools",
    "Cybersecurity Awareness: How to Spot Phishing Emails",
    "Emotional Intelligence (EQ) in the Workplace",
    "Giving Effective Feedback: The STAR Method",
    "Time Management: Mastering the Pomodoro Technique",
    "Diversity, Equity, and Inclusion (DEI) Fundamentals",
    "Conflict Resolution Strategies for Teams",
    "Introduction to Sustainable Business Practices (ESG)",
    "Public Speaking: How to Craft a Compelling Presentation",
    "Financial Literacy for Employees: Understanding Your 401(k)",
    "Health and Safety Protocols in a Lab Environment",
    "A Guide to Agile Methodologies Beyond Scrum",
    "Customer Support Training: De-escalation Techniques",
    "Mastering Business Writing: From Emails to Reports",
    "Introduction to Design Thinking for Non-Designers",
    "Media Training for Executives: Handling Press Interviews",
    "The Art of Negotiation: Key Principles for Success",
    "Mental Health and Well-being in the Workplace",
    "A Guide to Using Our New Internal CRM Software",
    "Leadership Training: From Manager to Coach",
    "How mRNA Vaccines Work: A Cellular Story",
    "The James Webb Space Telescope: Peering into the Dawn of Time",
    "Gravitational Waves: How We Listen to the Universe's Echoes",
    "The Human Genome Project: Mapping the Blueprint of Life",
    "The Science of Sleep: What Happens in Your Brain When You Dream?",
    "Carbon Capture Technology: Can We Bury Climate Change?",
    "GPS & Relativity: Why Your Phone Needs Einstein to Work",
    "The Mycorrhizal Network: The 'Wood Wide Web' of Forests",
    "How Large Language Models (LLMs) Actually Learn",
    "The Water Cycle on Mars: A Story of a Lost Ocean",
    "The Structure of a Virus: How They Hijack Our Cells",
    "Sonar and Echolocation: The 'Vision' of Bats and Submarines",
    "The Maillard Reaction: The Chemistry of Delicious Food",
    "The Aerodynamics of a Formula 1 Car",
    "How Solar Panels Convert Sunlight into Electricity (The Photovoltaic Effect)",
    "A Brief History of the Internet: From ARPANET to the World Wide Web",
    "The Lifecycle of a Star: From Stellar Nebula to Supernova",
    "Geothermal Energy: Tapping into the Earth's Core",
    "How Neural Networks Create Art (StyleGAN explained)",
    "Comparing Biological and Artificial Neural Networks",
    "The Water Cycle: A Journey from Ocean to Cloud",
    "Photosynthesis: How Plants Make Their Own Food",
    "A Tour of Our Solar System's Planets",
    "Plate Tectonics and Why Earthquakes Happen",
    "Key Battles of the American Revolutionary War",
    "The Rise and Fall of the Roman Empire",
    "Shakespeare's 'Romeo and Juliet': A Plot Summary",
    "How a Bill Becomes a Law in the United States",
    "Introduction to Coding with Scratch",
    "The Food Chain: Predators and Prey in an Ecosystem",
    "Why Do We Have Seasons? Understanding Earth's Tilt",
    "The Three States of Matter: Solid, Liquid, Gas",
    "A Journey Through the Human Digestive System",
    "The Evolution of Writing: From Hieroglyphs to the Alphabet",
    "Adding and Subtracting Fractions: A Visual Guide",
    "An Introduction to the Periodic Table of Elements",
    "The Three Branches of U.S. Government",
    "The Great Artists of the Renaissance",
    "The Spark of World War I: A Chain of Events",
    "Understanding Basic Electrical Circuits",
    "How to Use Your New Smartwatch to Track Health Metrics",
    "Mastering Your DSLR Camera: Aperture, Shutter Speed, and ISO",
    "A Guide to Your Smart Home System: Controlling Lights with One App",
    "Getting Started with Your New Instant Pot Multi-Cooker",
    "Key Features of an Electric Vehicle: One-Pedal Driving and Autopark",
    "How to Play the New 'Catan: Starfarers' Board Game",
    "Your Robot Vacuum's Mapping and Auto-Charging Features",
    "Step-by-Step Assembly of the LEGO Millennium Falcon set",
    "A Tour of Our Banking App's New Smart Budgeting Feature",
    "How Your Smart Thermostat Learns Your Habits to Save Energy",
    "Unique Features of a Smart Suitcase: GPS Tracking and Self-Weighing",
    "The Cushioning Technology Behind the New Nike Air Max",
    "Your First Print with a Home 3D Printer",
    "How a Memory Foam Mattress Adapts to Your Body",
    "A Guide to Using the Rowing Machine at the Gym",
    "How to Set Up and Pack Your New 4-Person Camping Tent",
    "The Process of Our Online Legal Consultation Service",
    "How to Use a Portable Projector for a Movie Night",
    "The Modular Design of Our New Sofa: Endless Combinations",
    "Your Smart Fridge's Food Management and Recipe Features",
    "The Global Semiconductor Supply Chain: A Fragile Lifeline",
    "Central Bank Digital Currencies (CBDCs): The Future of Money?",
    "The Business Models of Streaming: Netflix vs. Spotify",
    "Universal Basic Income (UBI): A Thought Experiment",
    "The Psychology of Social Media Algorithms",
    "How Misinformation Spreads Online: A Network Analysis",
    "The 'Metaverse': What Is It, Really?",
    "The Principles of a Circular Economy",
    "The Gig Economy: Pros and Cons for Workers and Companies",
    "The History and Future of Remote Work",
    "The Technological and Ethical Challenges of Colonizing Mars",
    "An Introduction to Stoic Philosophy: Controlling What You Can",
    "The Impact of Globalization on Local Cultures",
    "The Rise of ESG Investing: Can Business Save the Planet?",
    "How Gene Editing Will Change Humanity",
    "The Business of 'Big Pharma': A Look Inside the Industry",
    "The Art and Science of Political Polling: Why It's So Often Wrong",
    "Understanding International Trade Agreements: From WTO to CPTPP",
    "The Future of Cities: Smart Cities and the 15-Minute Neighborhood",
    "The Global Energy Crisis: Causes and Solutions",
    "Decoding 'Attention Is All You Need': The Transformer Architecture",
    "AlphaFold Explained: How AI Solved Protein Folding",
    "Daniel Kahneman's Prospect Theory: Understanding Our Biases",
    "The GAN Paper Explained: How Generative Adversarial Networks Work",
    "The Coase Theorem and the Problem of Externalities",
    "Breaking Down the Latest Nature Paper on [Specific Topic]",
    "A Look at the Original 'Deep Learning' Paper by Hinton et al.",
    "The Milgram Experiment: An Analysis of Obedience to Authority",
    "Satoshi Nakamoto's Bitcoin Whitepaper: A Line-by-Line Breakdown",
    "The Discovery of Penicillin: Fleming's Serendipitous Finding",
    "The Theory of Plate Tectonics: How It Was Proven",
    "John Nash's Game Theory and the Nash Equilibrium",
    "Breaking Down the Latest Breakthrough in Battery Technology",
    "The Loftus and Palmer Study on False Memories",
    "How DeepMind's AI Mastered Atari Games (The DQN Paper)",
    "The Latest Research on the Human Gut Microbiome",
    "The Discovery of CRISPR-Cas9: A Scientific Revolution",
    "The Key Scientific Breakthroughs Behind mRNA Vaccines",
    "The 'Endowment Effect': A Classic of Behavioral Economics",
    "How to Turn Your PhD Thesis into a Video Abstract",
    "A Visual Proof of the Pythagorean Theorem",
    "An Intuitive Introduction to Logarithms",
    "Solving Quadratic Equations: Factoring, Completing the Square, and the Formula",
    "An Introduction to Imaginary Numbers and the Complex Plane",
    "The Concept of Infinity and Its Paradoxes",
    "Visualizing Matrix Transformations: Rotation, Scaling, and Shearing",
    "Solving Systems of Linear Equations with Gaussian Elimination",
    "The Fibonacci Sequence and the Golden Ratio in Nature",
    "An Intuitive Explanation of Standard Deviation",
    "Introduction to Probability with Cards and Dice",
    "The Fundamental Theorem of Calculus Explained",
    "The Art of Adding Auxiliary Lines in Geometry Problems",
    "The Logic of Mathematical Induction",
    "The Seven Bridges of Königsberg: An Introduction to Graph Theory",
    "How to Solve a Rubik's Cube with Algorithms",
    "The Monty Hall Problem: A Probability Puzzle Explained",
    "Introduction to Cryptography: RSA Encryption and Prime Numbers",
    "How to Read and Interpret a Box Plot",
    "Euler's Polyhedron Formula Explained (V-E+F=2)",
    "A Challenging Math Olympiad Problem, Solved Step-by-Step",
    "An Introduction to 3D Modeling in Blender",
    "Building Your First Website with Webflow (No Code)",
    "Mastering Layers and Masks in Adobe Photoshop",
    "Setting Up Your First E-commerce Store with Shopify",
    "How to Use Anki for Effective Spaced Repetition Learning",
    "A Tour of Salesforce: Managing Your Sales Funnel",
    "Getting Started with QuickBooks for Small Business Accounting",
    "Making Your First Beat in Ableton Live",
    "Collaboration Tricks in Google Docs: Comments, Suggestions, and Version History",
    "How to Run a Remote Brainstorming Session with Miro",
    "Introduction to Data Analysis with Python and Pandas",
    "How Grammarly Improves Your English Writing",
    "Building Your First Web App Without Code Using Bubble",
    "Version Control for Beginners: Git and GitHub",
    "Introduction to 2D Drafting in AutoCAD",
    "How to Set Up a Livestream with OBS Studio",
    "Managing Your Passwords with 1Password/LastPass",
    "How to Design Effective Questions in SurveyMonkey",
    "Creating Your First Motion Graphic in Adobe After Effects",
    "Managing Projects with Jira: Sprints and Kanban Boards",
    "新人销售入职第一周：产品知识与销售话术",
    "《数字营销实战》：SEO入门与关键词策略",
    "团队协作培训：如何高效使用飞书/Slack进行项目沟通",
    "谈判技巧：双赢谈判的五个核心原则",
    "时间管理：用“四象限法则”规划你的一天",
    "企业网络安全意识培训：如何识别钓鱼邮件",
    "门店服务礼仪：从迎客到送客的全流程规范",
    "设计思维工作坊：从用户共情到原型测试",
    "财务知识普及：给非财务人员的预算管理课",
    "远程工作最佳实践：如何保持高效与专注",
    "情绪智力（EQ）在职场中的应用",
    "公开演讲技巧：如何克服紧张情绪并吸引听众",
    "内容营销策略：如何为你的品牌讲故事",
    "多元化与包容性（D&I）企业文化建设",
    "用户访谈技巧：如何提出不带偏见的好问题",
    "危机管理与媒体应对预案",
    "员工心理健康：压力识别与应对策略",
    "办公室急救知识（CPR）入门",
    "商业写作指南：如何撰写清晰有力的商务邮件",
    "新晋管理者培训：从技术专家到团队领导的转型",
    "詹姆斯·韦伯太空望远镜如何 peering into the early universe",
    "mRNA疫苗的工作原理：给你的细胞发送指令",
    "引力波：聆听宇宙的“声音”",
    "人类基因组计划：我们是如何绘制生命蓝图的？",
    "量子计算机的核心：叠加态与纠缠的直观解释",
    "碳捕捉技术：我们能把二氧化碳“塞”回地下吗？",
    "GPS全球定位系统：它为何离不开爱因斯坦的相对论？",
    "睡眠科学：为什么我们需要REM睡眠周期？",
    "菌根网络：森林里的“地下互联网”",
    "LLM大语言模型是如何学习和“思考”的？",
    "火星上的水循环：红色星球的过去与未来",
    "病毒的结构：它们是如何入侵我们细胞的？",
    "声纳与回声定位：蝙蝠和潜艇的“视觉”",
    "烹饪中的化学：美拉德反应的秘密",
    "F1赛车的空气动力学：看不见的下压力",
    "太阳能电池板：光伏效应的微观世界",
    "互联网简史：从ARPANET到万维网",
    "恒星的生命周期：从诞生到死亡",
    "地热能：来自地球核心的清洁能源",
    "人脑的神经网络：生物智能与人工智能的对比",
    "自然课：水循环的奇妙旅程",
    "生物课：光合作用，植物的“秘密厨房”",
    "天文课：太阳系的家庭成员",
    "地理课：板块构造与地震的成因",
    "美国历史：独立战争的关键战役",
    "世界历史：古罗马帝国的崛起与衰落",
    "文学课：莎士比亚《罗密欧与朱丽叶》剧情解析",
    "公民课：民主的诞生与三权分立",
    "编程入门：用Scratch制作你的第一个动画",
    "生物课：食物链与生态系统的平衡",
    "地理课：四季的成因——地球的倾斜之旅",
    "物理课：物质的三种状态（固、液、气）",
    "生物课：人体的消化系统之旅",
    "历史课：文字的演变——从象形文字到字母",
    "数学课：分数的加减法可视化教学",
    "化学课：元素周期表入门",
    "政治课：一项法案是如何成为法律的？",
    "艺术史：文艺复兴时期的三大巨匠",
    "世界历史：第一次世界大战的导火索",
    "物理课：电路的基本原理",
    "新品发布：智能手表如何监测你的健康数据",
    "专业相机使用指南：光圈、快门与ISO的平衡",
    "智能家居系统演示：用一个App控制全屋灯光",
    "多功能料理锅（Instant Pot）的使用与食谱",
    "电动汽车核心功能讲解：单踏板模式与自动泊车",
    "新桌游规则讲解与开箱",
    "机器人吸尘器的路径规划与自动回充功能",
    "乐高复杂套装（如千年隼）的拼装步骤演示",
    "银行App新功能：智能理财与账单分析",
    "智能恒温器如何学习你的习惯并节省能源",
    "高科技旅行箱的独特功能：GPS追踪与自称重",
    "一款新发布的运动鞋的缓震技术解析",
    "如何使用家用3D打印机开始你的第一个创作",
    "记忆棉床垫如何适应你的睡姿",
    "健身房新器械：划船机的使用指南",
    "高端帐篷的搭建与打包教学",
    "服务介绍：我们的线上法律咨询流程",
    "如何使用便携式投影仪打造家庭影院",
    "新品发布：模块化沙发的多种组合方式",
    "智能冰箱的食材管理与食谱推荐功能",
    "全球半导体供应链：脆弱的科技命脉",
    "央行数字货币 (CBDC) 的崛起与未来",
    "流媒体商业模式解析：Netflix vs. Spotify",
    "全民基本收入 (UBI) 的思想实验与社会影响",
    "社交媒体算法的心理学：我们是如何被“塑造”的",
    "虚假信息是如何在线上传播的？",
    "“元宇宙”的真正含义与技术挑战",
    "循环经济的基本原则与商业案例",
    "零工经济（Gig Economy）的利与弊",
    "远程工作的历史与未来趋势",
    "太空殖民的技术与伦理挑战",
    "斯多葛主义哲学入门：控制你能控制的",
    "全球化对本土文化的影响",
    "ESG投资的兴起与争议",
    "基因编辑将如何改变人类的未来？",
    "“大型制药公司”的商业模式解析",
    "政治民调的艺术与科学：为何它总是不准？",
    "理解国际贸易协定：从WTO到CPTPP",
    "未来的城市：智慧城市与15分钟生活圈",
    "全球能源危机的成因与解决方案",
    "解读Geoffrey Hinton的胶囊网络（Capsule Networks）",
    "香农《通信的数学理论》核心思想",
    "沃森与克里克：DNA双螺旋结构的发现之旅",
    "LIGO首次探测到引力波的论文解读",
    "丹尼尔·卡尼曼《思考，快与慢》的核心概念",
    "《科学》期刊最新气候变化论文摘要",
    "“深度学习”概念的首次提出与意义",
    "米尔格拉姆的“服从权威”实验解读",
    "中本聪的比特币白皮书核心解读",
    "青霉素的发现：一个意外的科学奇迹",
    "板块构造理论的建立与证据",
    "约翰·纳什的博弈论与纳什均衡",
    "最新电池技术突破论文解读",
    "“商场迷路”实验与虚假记忆研究",
    "强化学习论文：DeepMind如何用AI玩转雅达利游戏",
    "人类肠道菌群的最新研究发现",
    "CRISPR-Cas9基因编辑技术的发现论文",
    "mRNA疫苗背后的关键科学突破",
    "行为经济学经典：解读“禀赋效应”",
    "考古学最新发现：[具体发现]论文解读",
    "视觉化讲解勾股定理 (毕达哥拉斯定理)",
    "对数（Logarithms）的直观理解",
    "二次方程求解：配方法与公式法",
    "虚数i与复平面入门",
    "无穷大（Infinity）的概念与悖论",
    "矩阵变换的可视化：旋转、缩放与剪切",
    "高斯消元法解线性方程组",
    "斐波那契数列与自然界中的黄金分割",
    "标准差的直观解释：数据有多“分散”？",
    "概率计算入门：扑克牌与骰子",
    "导数与积分的“微积分基本定理”",
    "几何难题：如何巧妙添加辅助线",
    "数学归纳法的逻辑与应用",
    "图论入门：七桥问题与欧拉回路",
    "如何用算法思维解决魔方",
    "“三门问题”（Monty Hall Problem）的概率解析",
    "密码学入门：RSA加密与素数",
    "箱形图（Box Plot）的解读与绘制",
    "3D几何：欧拉多面体定理 (V-E+F=2)",
    "奥数经典题：[具体题目]的解题思路",
    "3D建模入门：Blender的核心界面与操作",
    "如何用Wix/Squarespace搭建你的个人网站",
    "Adobe Photoshop：图层蒙版的强大功能",
    "Shopify开店指南：从零到一发布你的第一个商品",
    "Anki入门：如何用间隔重复法高效学习",
    "Salesforce核心功能：销售漏斗与客户管理",
    "QuickBooks入门：自动化你的小企业记账",
    "Ableton Live音乐制作：从鼓点到旋律",
    "Google Docs协作技巧：评论、建议与版本历史",
    "Miro远程协作白板：团队头脑风暴的最佳实践",
    "Python数据分析：Pandas库入门",
    "Grammarly语法检查：提升你的英文写作水平",
    "Bubble无代码开发：构建你的第一个Web App",
    "Git与GitHub：程序员的版本控制入门",
    "AutoCAD建筑绘图：二维平面图绘制基础",
    "OBS Studio直播串流设置指南",
    "1Password/LastPass密码管理器：告别忘记密码",
    "SurveyMonkey问卷设计：如何提出有效的问题",
    "Adobe After Effects：制作你的第一个动态图标",
    "Jira项目管理：看板（Kanban）与冲刺（Sprint）"
]
PROMPTS = ["生成英文版本", "生成中文版本"]
LARGE_TEXT_SAMPLES = [
    ""
]
# --- 新增配置结束 ---


# ============================ 配置区域 END ============================


# 存储统计结果的列表
results = {
    "success": [],
    "failure": [],
    "ttfb": []  # Time to First Byte, 仅用于流式模式
}

# 线程锁，用于安全地更新共享的 results 字典
lock = threading.Lock()


def send_request(request_id):
    """
    发送单个 API 请求并记录结果。
    """
    api_url = f"{API_BASE_URL}{ENDPOINT}"

    headers = {
        'Authorization': f'Bearer {API_KEY}',
        'Content-Type': 'application/json'
    }

    # --- 修改部分：构建包含 'inputs' 变量的请求体 ---
    payload = {
        "inputs": {
            "topic": random.choice(TOPICS),
            "prompt": random.choice(PROMPTS),
            "smart_search": True,  # 根据图片，“必填”，假设为布尔值
            "file_parsing": False,  # 根据图片，“必填”，假设为布尔值
            "large_text": random.choice(LARGE_TEXT_SAMPLES)
        },
        "query": random.choice(QUERIES),
        "response_mode": RESPONSE_MODE,
        "user": f"test-user-{uuid.uuid4()}",  # 为每个请求生成唯一用户
    }
    # --- 修改结束 ---

    start_time = time.monotonic()
    response = None
    first_byte_time = None

    try:
        if RESPONSE_MODE == "streaming":
            response = requests.post(
                api_url,
                headers=headers,
                json=payload,
                timeout=120,
                stream=True
            )
            response.raise_for_status()

            for chunk in response.iter_content(chunk_size=8192):
                if first_byte_time is None:
                    first_byte_time = time.monotonic()
                pass

        else:  # blocking 模式
            response = requests.post(
                api_url,
                headers=headers,
                json=payload,
                timeout=120
            )
            response.raise_for_status()
            _ = response.json()

        end_time = time.monotonic()
        latency = end_time - start_time

        with lock:
            results["success"].append(latency)
            if first_byte_time:
                ttfb = first_byte_time - start_time
                results["ttfb"].append(ttfb)

        return (request_id, "Success", response.status_code, latency)

    except requests.exceptions.RequestException as e:
        end_time = time.monotonic()
        latency = end_time - start_time
        error_message = f"Error: {e}"
        if response is not None:
            error_message += f", Response Body: {response.text[:200]}"

        with lock:
            results["failure"].append(latency)

        return (request_id, "Failure", response.status_code if response else "N/A", latency, error_message)


def print_statistics():
    """
    打印详细的性能统计报告
    """
    success_count = len(results["success"])
    failure_count = len(results["failure"])
    total_run = success_count + failure_count

    if total_run == 0:
        print("没有发送任何请求。")
        return

    print("\n--- 并发测试结果统计 ---")
    print(f"总请求数: {total_run}")
    print(f"成功请求: {success_count} ({(success_count / total_run) * 100:.2f}%)")
    print(f"失败请求: {failure_count} ({(failure_count / total_run) * 100:.2f}%)")

    if success_count > 0:
        total_time = sum(results["success"])
        latencies = results["success"]

        print("\n--- 成功请求响应时间 (Latency) ---")
        print(f"平均响应时间 (Avg): {np.mean(latencies):.4f} 秒")
        print(f"最快响应时间 (Min): {min(latencies):.4f} 秒")
        print(f"最慢响应时间 (Max): {max(latencies):.4f} 秒")

        print(f"P50 (Median):        {np.percentile(latencies, 50):.4f} 秒")
        print(f"P90:                 {np.percentile(latencies, 90):.4f} 秒")
        print(f"P95:                 {np.percentile(latencies, 95):.4f} 秒")
        print(f"P99:                 {np.percentile(latencies, 99):.4f} 秒")

    if RESPONSE_MODE == 'streaming' and len(results["ttfb"]) > 0:
        ttfb_times = results["ttfb"]
        print("\n--- [流式] 首字节到达时间 (TTFB) ---")
        print(f"平均TTFB (Avg): {np.mean(ttfb_times):.4f} 秒")
        print(f"最快TTFB (Min): {min(ttfb_times):.4f} 秒")
        print(f"最慢TTFB (Max): {max(ttfb_times):.4f} 秒")

    print("-" * 25)


def main():
    """
    主执行函数
    """
    print(f"开始并发测试...")
    print(f"总请求数: {TOTAL_REQUESTS}, 最大并发数: {MAX_WORKERS}, 模式: {RESPONSE_MODE}")
    print("-" * 40)

    overall_start_time = time.monotonic()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_req_id = {executor.submit(send_request, i): i for i in range(TOTAL_REQUESTS)}

        for i, future in enumerate(as_completed(future_to_req_id)):
            try:
                result = future.result()
                if result[1] == "Success":
                    print(
                        f"请求 {result[0] + 1}/{TOTAL_REQUESTS}: {result[1]} (状态码: {result[2]}, 耗时: {result[3]:.4f}s)")
                else:
                    print(
                        f"请求 {result[0] + 1}/{TOTAL_REQUESTS}: {result[1]} (状态码: {result[2]}, 耗时: {result[3]:.4f}s) - {result[4]}")

            except Exception as exc:
                print(f"请求生成异常: {exc}")

    overall_end_time = time.monotonic()
    total_duration = overall_end_time - overall_start_time

    print("-" * 40)
    print(f"所有请求完成。总耗时: {total_duration:.4f} 秒")

    if total_duration > 0:
        qps = TOTAL_REQUESTS / total_duration
        print(f"吞吐率 (QPS): {qps:.2f} 请求/秒")

    print_statistics()


if __name__ == "__main__":
    main()
