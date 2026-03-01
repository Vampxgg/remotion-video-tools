一。你当前的 JSON 结构本身是对的，但问题不在「结构」，而在检索责任边界不清晰：
当前隐含问题
用户选择 ≠ 检索强约束
用户选了 document_id
但 embedding / query 阶段仍然是“关键词驱动”
容易出现：
用户选了 A 文档 → 实际召回 B 文档里更相似的片段
全文 / 片段 是执行策略，不是检索意图
retrieval_mode 现在只是参数
没有影响：
query rewrite
embedding strategy
ranking strategy
多模态内容在 pipeline 中没有“结构化地位”
视频 / 音频 / 图像 → 最终都退化为文本
丢失：
时间轴
画面语义
操作步骤关联


二、全新检索架构设计（核心思想）
把 RAG 拆成三层责任：
「用户意图层」 → 「召回控制层」 → 「执行检索层」


三、整体架构图（逻辑架构）
┌──────────────────────────────┐
│        User Selection        │
│  (DB / Doc / Mode / TopK)    │
└─────────────┬────────────────┘
              │
              ▼
┌──────────────────────────────┐
│ ① Retrieval Control Layer    │  ← 核心新增
│ - 强约束解析（数据库/文档） │
│ - 检索模式策略生成          │
│ - Query Rewrite              │
└─────────────┬────────────────┘
              │
              ▼
┌──────────────────────────────┐
│ ② Hybrid Retrieval Engine    │
│ ┌──────────┐  ┌──────────┐  │
│ │ Metadata │  │ Vector   │  │
│ │ Filter   │  │ Search   │  │
│ └──────────┘  └──────────┘  │
│        │         │           │
│        └───┬─────┘           │
│            ▼                 │
│     Multi-Stage Ranking      │
└─────────────┬────────────────┘
              │
              ▼
┌──────────────────────────────┐
│ ③ Context Assembly Layer     │
│ - 全文拼接 / 片段拼接        │
│ - 多模态上下文封装           │
│ - Token Budget Control       │
└─────────────┬────────────────┘
              │
              ▼
┌──────────────────────────────┐
│            LLM               │
└──────────────────────────────┘



四、核心模块详细设计（重点）
① Retrieval Control Layer（这是你现在缺失的核心）
作用一句话：
把“用户选择”转化为不可违背的检索规则
输入
{
  "database_id": "...",
  "document_id": "...",
  "retrieval_mode": "full | segment",
  "top_k": 5,
  "user_query": "理想L9动力电池怎么维修？"
}

输出（给检索引擎用）
{
  "metadata_filter": {
    "database_id": "...",
    "document_id": "..."
  },
  "retrieval_strategy": {
    "mode": "segment",
    "embedding_scope": "document_only",
    "ranking_profile": "precision_first"
  },
  "rewritten_query": [
    "理想L9 动力电池 维修 步骤",
    "L9 电池 拆装 风险"
  ]
}

✅ 关键设计点
document_id 必须进入 metadata filter（强约束）
禁止“跨文档相似度抢占”
query rewrite 根据 retrieval_mode 不同而不同


② Hybrid Retrieval Engine（真正执行召回）
2.1 Metadata First（先过滤，再相似）
永远先做：
WHERE database_id = X
AND document_id = Y

再做：
向量检索
BM25（如果有）
❗这是精度提升的“最便宜红利”


2.2 分模式召回策略
retrieval_mode实际策略
full_document_retrieval
document embedding + chunk 顺序展开
segment_retrieval
chunk embedding + semantic ranking
full_document_retrieval
不做 top_k
直接返回：
文档摘要
章节结构
关键片段（LLM 后处理）
segment_retrieval
top_k 生效
加 语义去重 + 时间连续性（视频）


③ Context Assembly Layer（LLM 前的“最后一刀”）
这是很多 RAG 精度不稳的真正原因。
不同模态的封装方式
文本
{
  "type": "text",
  "source": "doc_x",
  "content": "..."
}

视频 / 音频
{
  "type": "video",
  "source": "doc_y",
  "timestamp": "03:12 - 04:05",
  "speech": "...",
  "visual_summary": "..."
}

图像
{
  "type": "image",
  "caption": "...",
  "related_step": "电池拆卸第3步"
}

➡ LLM 会天然更“相信结构化上下文”


五、你这个 JSON 结构的“进化版本”（推荐）
用户侧（保持你现在的自由度）
{
  "selection": [
    {
      "database_id": "...",
      "documents": [
        {
          "document_id": "...",
          "mode": "full"
        },
        {
          "document_id": "...",
          "mode": "segment",
          "top_k": 5
        }
      ]
    }
  ],
  "query": "理想L9动力电池维修"
}

系统内部拆解为多条强约束检索任务
[
  {
    "doc_id": "...",
    "strategy": "full_doc"
  },
  {
    "doc_id": "...",
    "strategy": "segment",
    "top_k": 5
  }
]

分别检索 → 分别组装 → 最终合并


六、这套架构能解决什么？
✅ 用户选择的文档 100% 生效
✅ 全文 / 片段 不是参数，而是策略
✅ 多模态不再是“文本附庸”
✅ 极适合 Dify 的 API / metadata filter 能力
✅ 后期可以自然加入：
reranker
权重学习
用户偏好记忆


七、如果你愿意，我可以继续帮你做的事
🔹 画一张 可直接给研发用的 UML / 时序图
🔹 设计 Dify Dataset Metadata Schema
🔹 帮你写 retrieval control 的伪代码
🔹 设计 视频 / 音频 chunk 的最佳粒度
你可以直接告诉我：
👉「下一步我想落地的是哪一层？」
都做
好，这一条我直接 “全量交付”，把你要的 4 件事一次性做完整，并且是可以直接落地给研发的版本。
我会严格站在你们现在 基于 Dify 的生产级 RAG 系统视角来设计，而不是论文式 RAG。


一、最终目标（先对齐认知）
你们要的不是「更强的向量检索」，而是：
让“用户对知识的选择权”成为检索系统的最高优先级约束
这意味着：
❌ 不能让 embedding 相似度“越权”
❌ 不能让 chunk 抢走 document 的主导权
❌ 不能让多模态退化成普通文本
✅ 必须“先选定知识范围 → 再谈相似度”


二、完整系统架构图（研发可实现版）
2.1 总体逻辑架构（模块级）
┌──────────────────────────────────────┐
│              Client / UI              │
│  用户选择：                           │
│  - database_id                        │
│  - document_id                        │
│  - retrieval_mode (full / segment)   │
│  - top_k                              │
│  - question                           │
└───────────────────┬──────────────────┘
                    │
                    ▼
┌──────────────────────────────────────┐
│ ① Retrieval Control Layer（核心）     │
│ - Selection 解析                      │
│ - 强约束生成（metadata filter）       │
│ - Query Rewrite                       │
│ - 检索任务拆解                        │
└───────────────────┬──────────────────┘
                    │
                    ▼
┌──────────────────────────────────────┐
│ ② Retrieval Execution Layer           │
│ ┌──────────────┐  ┌──────────────┐  │
│ │ Metadata     │  │ Vector / BM25│  │
│ │ Filtering    │  │ Search       │  │
│ └──────────────┘  └──────────────┘  │
│            │            │            │
│            └─────┬──────┘            │
│                  ▼                   │
│         Multi-Stage Ranking           │
└───────────────────┬──────────────────┘
                    │
                    ▼
┌──────────────────────────────────────┐
│ ③ Context Assembly Layer              │
│ - 文档级拼接                          │
│ - Chunk 级拼接                        │
│ - 多模态结构化封装                    │
│ - Token Budget 控制                   │
└───────────────────┬──────────────────┘
                    │
                    ▼
┌──────────────────────────────────────┐
│               LLM                    │
└──────────────────────────────────────┘



2.2 关键设计原则（必须遵守）
原则说明
用户选择 > 相似度
document_id 是硬约束
先过滤再检索
metadata → vector
全文 ≠ 片段
是两套策略
多模态结构化
不是“拼文本”
可拆任务
每个 document 是独立检索单元


三、Retrieval Control Layer（你们现在缺失的“中枢神经”）
这是整个系统精度跃迁的关键
3.1 核心职责
把用户 JSON 拆成“不可违背的检索任务”
把 retrieval_mode 转成 检索策略
把自然语言 query 改写成 适合当前文档的查询


3.2 输入（来自 UI / API）
{
  "selection": [
    {
      "database_id": "c3517...",
      "document_info": [
        {
          "document_id": "5e81...",
          "retrieval_mode": "full_document_retrieval"
        },
        {
          "document_id": "890b...",
          "retrieval_mode": "segment_retrieval",
          "top_k": 5
        }
      ]
    }
  ],
  "question": "理想L9动力电池怎么维修？"
}



3.3 输出（给检索引擎）
拆解为 多条检索任务
[
  {
    "task_id": "task_001",
    "database_id": "c3517...",
    "document_id": "5e81...",
    "strategy": {
      "type": "full_document",
      "embedding_scope": "document_only"
    },
    "query_variants": [
      "理想L9 动力电池 维修流程",
      "L9 电池 维修 注意事项"
    ]
  },
  {
    "task_id": "task_002",
    "database_id": "c3517...",
    "document_id": "890b...",
    "strategy": {
      "type": "segment",
      "top_k": 5,
      "embedding_scope": "chunk_only"
    },
    "query_variants": [
      "理想L9 电池 拆卸",
      "L9 动力电池 安装 步骤"
    ]
  }
]

⚠️ 注意：document_id 已经成为“搜索空间边界”


3.4 Retrieval Control 伪代码（可直接给后端）
def build_retrieval_tasks(user_selection, question):
    tasks = []

    for db in user_selection:
        for doc in db["document_info"]:
            strategy = resolve_strategy(doc["retrieval_mode"])

            rewritten_queries = rewrite_query(
                question,
                mode=doc["retrieval_mode"]
            )

            tasks.append({
                "database_id": db["database_id"],
                "document_id": doc["document_id"],
                "strategy": strategy,
                "queries": rewritten_queries
            })

    return tasks



四、Retrieval Execution Layer（执行检索）
4.1 强制 Metadata Filter（不可绕过）
{
  "filter": {
    "database_id": "c3517...",
    "document_id": "890b..."
  }
}

❗这一步必须发生在 向量搜索之前


4.2 两种模式 = 两套引擎行为
模式一：全文检索（Full Document）
特点
不追求 recall
追求：结构 + 完整性
流程
拉取 document metadata
按章节 / 时间轴排序
生成文档级摘要（可缓存）
返回
{
  "type": "full_document",
  "sections": [...],
  "summary": "..."
}



模式二：片段检索（Segment）
特点
semantic similarity
top_k 生效
去重 & 连续性
增强规则
chunk overlap 合并
视频按时间连续合并（±15s）


五、Context Assembly Layer（真正决定 LLM 表现）
5.1 为什么必须单独一层？
因为 LLM 不擅长在混乱上下文中找重点


5.2 统一 Context Schema（推荐）
{
  "context_blocks": [
    {
      "source_type": "text",
      "document_id": "...",
      "content": "..."
    },
    {
      "source_type": "video",
      "document_id": "...",
      "timestamp": "03:20-04:05",
      "speech_text": "...",
      "visual_summary": "演示电池拆卸"
    }
  ]
}



5.3 Token Budget 策略（实战经验）
内容策略
全文
先摘要后补细节
视频
只送关键时间段
图片
caption + step
重复 chunk
必须去重


六、多模态 Chunk 设计（非常关键）
6.1 视频 / 音频切分标准（推荐）
类型粒度
教学视频
20–40 秒
操作步骤
1 step = 1 chunk
讲解
语义断点
视频 Chunk Metadata 示例
{
  "chunk_id": "v_003",
  "start": 180,
  "end": 220,
  "step": "拆卸电池固定螺栓",
  "risk_level": "high"
}



6.2 图像 Chunk（别再当附件）
{
  "image_id": "img_12",
  "caption": "电池固定支架位置",
  "related_step": "step_3"
}



七、你们这套方案最终能达到什么水平？
✅ 用户选什么 → 系统就只在什么里找
✅ 全文 / 片段 → 是不同的“认知模式”
✅ 视频 / 图像 → 变成“可推理知识”
✅ 非常适合 Dify 的 Dataset + API 能力
✅ 后期可以自然升级：
reranker
user preference
multi-agent reasoning