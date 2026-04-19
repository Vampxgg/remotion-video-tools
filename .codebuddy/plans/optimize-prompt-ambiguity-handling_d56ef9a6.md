---
name: optimize-prompt-ambiguity-handling
overview: 优化系统提示词中的“可行性检查”部分，增加对歧义性请求（如“调整语言”）的明确处理逻辑，通过澄清机制避免误解。
todos:
  - id: update-prompt-feasibility
    content: 更新 `ceshi/视频剧本/视频剧本.md` 第9点，增加歧义消除与边界界定逻辑
    status: completed
---

## Product Overview

优化系统提示词 (`视频剧本.md`) 中的“可行性检查”模块，增强 AI 对歧义性指令（如“调整语言”）的识别与处理能力。

## Core Features

- **歧义识别机制**：要求 AI 在遇到模棱两可的指令时（例如涉及“语言”、“声音”等既可能指文本又可能指音频的词汇），暂停盲目执行。
- **能力边界区分**：在思维链（Thought）中明确区分工具支持的功能（如修改字幕/文案）与不支持的功能（如修改配音/背景音乐）。
- **主动澄清交互**：通过 Notification 向用户发起确认，明确用户的具体意图（改字幕 vs 改配音），并告知当前能力范围，避免误解或生硬拒绝。