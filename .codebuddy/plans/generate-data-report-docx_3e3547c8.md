---
name: generate-data-report-docx
overview: Generate a DOCX file containing the 'Data Source and Update Frequency Report' for the data pipeline.
todos:
  - id: create-report-script
    content: Create `数据pipeline/generate_report.py` to generate the DOCX report using `python-docx`
    status: completed
  - id: execute-report-script
    content: Execute the script to generate `数据来源与更新频率报告.docx`
    status: completed
    dependencies:
      - create-report-script
---

## Product Overview

生成一份 Word 文档（DOCX 格式）的《数据来源与更新频率报告》，用于梳理数据 Pipeline 中四个核心 Agent 的技术细节、数据流向及更新机制。

## Core Features

- **文档生成**: 使用 `python-docx` 库自动生成格式化的 DOCX 报告。
- **内容覆盖**: 报告包含四个核心 Agent：

1. **关键字生成器 (Keyword Generator)**
2. **信息搜刮器 (Info Searcher)**
3. **内容获取器 (Content Fetcher)**
4. **数据整合器 (Data Integrator)**

- **详细信息**: 每个 Agent 需包含以下维度：
- **文件路径**: 对应的源代码文件位置。
- **核心功能**: 主要函数及职责描述。
- **数据源**: 涉及的外部服务（Google, Tavily, Jina, Firecrawl, ZhiLian, Tianyan 等）。
- **更新频率**: 数据获取的时效性说明（如实时、按需等）。
- **输出产物**: 在 `数据pipeline` 目录下生成 `数据来源与更新频率报告.docx`。

## Tech Stack

- **Language**: Python
- **Library**: `python-docx` (用于构建和写入 Word 文档)

## Implementation Details

### Script Logic

创建一个 Python 脚本 `generate_report.py`，内置报告所需的结构化数据，并利用 `python-docx` 将其渲染为文档。

### Data Structure (Internal)

脚本内部将维护一个包含 Agent 信息的列表/字典：

```python
agents = [
    {
        "name": "关键字生成器 (Keyword Generator)",
        "file": "数据pipeline/数据关键字获取.py",
        "description": "解析输入并生成标准化的搜索查询结构（Web、职业、企业）。",
        "sources": "内部逻辑 (无外部数据源)",
        "frequency": "N/A"
    },
    {
        "name": "信息搜刮器 (Info Searcher)",
        "file": "数据pipeline/数据链接信息搜刮.py",
        "description": "执行多源搜索调度，获取链接和元数据。",
        "sources": "Google (via SearchAPI), Tavily, Jina, Firecrawl, ZhiLian (API), Tianyan (API)",
        "frequency": "实时 (按需调用)"
    },
    ...
]
```