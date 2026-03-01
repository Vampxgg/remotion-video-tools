---
name: ignore-bs4-xml-warning
overview: 在 `数据获取.py` 中添加代码以忽略 BeautifulSoup 的 `XMLParsedAsHTMLWarning` 警告，防止因抓取 XML 页面而产生干扰信息。
todos:
  - id: ignore-warning
    content: 在数据获取.py中引入warnings并忽略XMLParsedAsHTMLWarning
    status: completed
---

## 需求概述

在现有爬虫脚本 `数据获取.py` 中添加警告过滤机制，屏蔽 BeautifulSoup 解析 XML 内容时产生的 `XMLParsedAsHTMLWarning` 警告，保持控制台输出整洁。

## 核心功能

- **引入警告处理模块**：导入 Python 标准库 `warnings`。
- **配置过滤规则**：设置忽略 `XMLParsedAsHTMLWarning` 类型的警告，防止因 lxml 解析器处理 XML 内容时触发误报。

## 技术栈

- **Python**: 使用内置 `warnings` 模块控制警告输出。
- **BeautifulSoup (bs4)**: 目标库，针对其抛出的 `XMLParsedAsHTMLWarning` 进行特定过滤。

## 实现细节

### 代码修改

在 `d:/pythonprojects/script_tools/数据pipeline/数据获取.py` 文件头部导入部分：

1. 导入 `warnings` 模块。
2. 从 `bs4` 导入 `XMLParsedAsHTMLWarning`（或直接通过类别过滤）。
3. 调用 `warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)`。