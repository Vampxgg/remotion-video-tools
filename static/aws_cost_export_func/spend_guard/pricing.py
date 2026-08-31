# -*- coding: utf-8 -*-
"""Bedrock 模型单价表 + token→USD 估算(仅用于每日花销守卫的近实时估算)。

为什么需要它(根因)：
- Cost Explorer **无法按 IAM 用户/调用者身份拆成本**(GetDimensionValues 无 IAM
  principal 维度，实测确认)。要"按人 + 近实时"限额，唯一可行信号是 CloudWatch
  Logs invocation logging 里的 identity.arn + 四类 token count。
- 故这里用 token × 单价做**估算成本**(strict mode)，与 CE 的实付金额会有偏差，
  但足以驱动"某人今天烧太多就先禁掉"的守卫决策。真实对账仍以每日报告的 CE 金额为准。

单价来源(真实核对，2026-08，us-east-1，单位 USD / 1M tokens，standard 非 batch)：
- Opus 4.8 / Opus 5:  input 5.00 / output 25.00 / cacheRead 0.50 / cacheWrite 6.25
- Sonnet 4.5 / 4.6:   input 3.00 / output 15.00 / cacheRead 0.30 / cacheWrite 3.75
- Sonnet 5:           input 2.00 / output 10.00 / cacheRead 0.20 / cacheWrite 2.50
- Haiku 4.5:          input 1.00 / output  5.00 / cacheRead 0.10 / cacheWrite 1.25

依据: AWS Marketplace「Claude Opus 4.8 (Amazon Bedrock Edition)」定价页 +
platform.claude.com/pricing，多来源一致。若 AWS 调价需同步更新本表。

匹配方式：Logs 侧 modelId 形如 ``us.anthropic.claude-opus-4-8`` /
``global.anthropic.claude-sonnet-4-6``，按包含关系匹配家族关键字，未命中回退到
``DEFAULT``(取 Opus 单价，宁可高估触发也不放过)。
"""

from __future__ import annotations

from typing import TypedDict


class ModelPrice(TypedDict):
    """单位：USD / 1M tokens。"""

    input: float
    output: float
    cache_read: float
    cache_write: float


# 家族关键字 → 单价。匹配用"modelId 是否包含该 key"，故 key 要能唯一定位家族。
# 顺序有意义：先匹配更具体的(opus-5 早于 opus)，用 list 保序而非 dict 语义模糊。
_PRICE_TABLE: list[tuple[str, ModelPrice]] = [
    ("claude-opus", {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25}),
    ("claude-sonnet-5", {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50}),
    ("claude-sonnet", {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75}),
    ("claude-haiku", {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25}),
]

# 未命中回退：取 Opus 单价(最贵)，避免因未知新模型而漏算导致守卫失效。
_DEFAULT_PRICE: ModelPrice = {
    "input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25,
}

_PER_MILLION = 1_000_000.0


def price_for(model_id: str) -> ModelPrice:
    """按 modelId 返回单价；未命中回退到最贵档(不静默放过用量)。"""
    mid = (model_id or "").lower()
    for key, price in _PRICE_TABLE:
        if key in mid:
            return price
    return _DEFAULT_PRICE


def estimate_cost_usd(
    model_id: str,
    input_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
) -> float:
    """把一个(模型, 四类 token)组合估算成 USD。

    四类 token 各按其单价计价(缓存读远低于普通输入，缓存写略高于普通输入)。
    这是守卫用的**估算**成本，非 AWS 实付。
    """
    p = price_for(model_id)
    return (
        input_tokens * p["input"]
        + cache_read_tokens * p["cache_read"]
        + cache_write_tokens * p["cache_write"]
        + output_tokens * p["output"]
    ) / _PER_MILLION
