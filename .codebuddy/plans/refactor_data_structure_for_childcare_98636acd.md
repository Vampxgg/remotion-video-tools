---
name: refactor_data_structure_for_childcare
overview: "Refactor the input/output JSON structure in the data acquisition scripts to align with the Childcare industry requirements: Web Public Data, Institution Data, and Job Data."
todos:
  - id: refactor-link-script
    content: 重构 `ceshi/多数据源获取链接.py`，更新输入解析器以支持 `web_public_data_query` 等新键名，并调整输出结构为 `web_public_data` 等。
    status: completed
  - id: refactor-data-script
    content: 重构 `ceshi/多数据源获取数据.py`，适配上一步的新输出结构作为输入，并将最终搜刮结果输出为 `web_public_data`、`institution_data` 和 `job_data`。
    status: completed
    dependencies:
      - refactor-link-script
---

## 需求概述

对数据获取脚本的输入输出 JSON 结构进行重构，使其与托育行业（Childcare）的业务领域要求对齐。主要涉及 `ceshi/多数据源获取链接.py` 和 `ceshi/多数据源获取数据.py` 两个文件。

## 核心功能

将原有的通用/汽车行业遗留结构映射为托育行业的三大核心数据板块：

1.  **Web Public Data (Web公开数据)**

    -   原 `comprehensive_query` / `comprehensive_data`
    -   新键名：`web_public_data_query` (输入) / `web_public_data` (输出)

2.  **Institution Data (机构数据)**

    -   原 `tianyan_check_enterprise` / `tianyan_check_data`
    -   新键名：`institution_data_query` (输入) / `institution_data` (输出)

3.  **Job Data (岗位数据)**

    -   原 `career_query` / `career_data`
    -   新键名：`job_data_query` (输入) / `job_data` (输出)

重构范围包括输入参数解析（Parser Logic）和最终结果组装（Output Construction）。

## 技术实现方案

### 1. JSON 结构变更设计

#### 阶段一：链接获取脚本 (`多数据源获取链接.py`)

**输入结构 (Input):**

```
{
  "web_public_data_query": ["查询词1", "查询词2"],
  "institution_data_query": ["机构名称1", "机构名称2"],
  "job_data_query": { "keywords": "...", "provinces": "..." }
}
```

**输出结构 (Output):**

```
{
  "datas": {
    "web_public_data": [...],
    "institution_data": [...],
    "job_data": {...}
  }
}
```

#### 阶段二：数据搜刮脚本 (`多数据源获取数据.py`)

**输入结构 (Input):**
承接阶段一的输出结构。

**输出结构 (Output):**

```
{
  "scraped_datas": {
    "web_public_data": { "all_source_list": [...], "all_video_list": [...] },
    "institution_data": { "status": "...", "data": [...] },
    "job_data": { "status": "...", "data": [...] }
  }
}
```

### 2. 代码修改点

- **`ceshi/多数据源获取链接.py`**:
    - 修改 `_intelligent_input_parser` 函数，适配新的 `_query` 后缀键名。
    - 修改 `main_async` 和 `main` 函数中的变量名及 `final_output` 组装逻辑。
- **`ceshi/多数据源获取数据.py`**:
    - 修改 `_parse_input_data` 函数，解析新的 `web_public_data` 等键名。
    - 修改 `main_async` 中的结果处理逻辑。
    - 修改最终返回的 `final_output` 字典键名。