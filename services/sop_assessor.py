# -*- coding: utf-8 -*-
"""SOP 实训评估核心：工具定义、系统指令渲染、服务端状态机、NDJSON 落盘。

设计要点
--------
1. 一次浏览器 WS 会话对应一个 ``SopAssessor`` 实例。
2. Live 会话里只暴露三个工具：
   - ``log_step_event``      —— 三段式评分事件 STEP_START/PROCESSING/STEP_COMPLETE
   - ``detect_spoken_keyword`` —— 学员口述命中 SOP keywords_required
   - ``flag_forbidden_action`` —— 学员做出 forbidden_action 中列出的违规
3. 服务端在拦截 ``tool_call`` 时调用 ``handle_tool_call``，做：
   - 数学一致性校验（有扣分理由但满分）→ 生成 correction 文本回灌给模型；
   - 跳步处理（中间步骤标记为 skipped）；
   - 状态表 / 累计得分维护；
   - 评估事件 append 到 ``logs/live/sessions/{session_id}.ndjson``。
4. ``snapshot()`` 给前端做实时进度面板的渲染数据源。

本模块对外只暴露纯函数 + 一个 ``SopAssessor`` 类，便于单测。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


# =====================================================================
# SOP 数据模型 & 解析
# =====================================================================


@dataclass(slots=True)
class SopStep:
    id: str
    name: str
    description: str = ""
    scoring_criteria: str = ""
    deduction_rule: str = ""
    keywords_required: List[str] = field(default_factory=list)
    ai_recognition_clues: str = ""
    ai_reasoning: str = ""
    forbidden_action: List[str] = field(default_factory=list)
    weight: float = 1.0


@dataclass(slots=True)
class SopDocument:
    name: str
    total_scoring_points: float
    steps: List[SopStep]
    raw: Dict[str, Any]


def parse_sop(payload: Union[str, Dict[str, Any], None]) -> SopDocument:
    """把用户提供的 SOP 规范化为 :class:`SopDocument`。

    允许的输入形态（按优先级）：
        1. ``dict`` —— 直接按 JSON 结构解析；
        2. ``str`` 且能 ``json.loads`` 成 dict —— 同上；
        3. 其它字符串 —— 视为 Markdown，按标题/编号切成步骤。
    """
    if payload is None or (isinstance(payload, str) and not payload.strip()):
        raise ValueError("SOP payload is empty")

    if isinstance(payload, dict):
        return _from_json(payload)

    text = payload.strip() if isinstance(payload, str) else ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return _from_json(data)
    except json.JSONDecodeError:
        pass
    return _from_markdown(text)


def _from_json(data: Dict[str, Any]) -> SopDocument:
    steps_raw = data.get("steps") or []
    if not isinstance(steps_raw, list):
        steps_raw = []
    steps: List[SopStep] = []
    auto_total = 0.0
    for idx, item in enumerate(steps_raw, start=1):
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("id") or item.get("step_id") or idx)
        weight = _to_float(item.get("weight"), default=1.0) or 1.0
        auto_total += weight
        steps.append(
            SopStep(
                id=step_id,
                name=str(item.get("step_name") or item.get("name") or f"步骤 {step_id}"),
                description=str(item.get("description") or ""),
                scoring_criteria=str(item.get("scoring_criteria") or ""),
                deduction_rule=str(item.get("deduction_rule") or ""),
                keywords_required=[
                    str(k) for k in (item.get("keywords_required") or []) if str(k).strip()
                ],
                ai_recognition_clues=str(item.get("ai_recognition_clues") or ""),
                ai_reasoning=str(item.get("aiReasoning") or item.get("ai_reasoning") or ""),
                forbidden_action=[
                    str(k) for k in (item.get("forbidden_action") or []) if str(k).strip()
                ],
                weight=weight,
            )
        )
    if not steps:
        raise ValueError("SOP JSON 缺少 steps 数组或 steps 为空")
    declared_total = _to_float(data.get("total_scoring_points"), default=0.0) or 0.0
    return SopDocument(
        name=str(data.get("sop_name") or data.get("name") or "未命名 SOP"),
        total_scoring_points=declared_total if declared_total > 0 else auto_total,
        steps=steps,
        raw=data,
    )


_MD_HEAD_RE = re.compile(
    r"^(?:#{1,6}\s+|[-*]\s+|\d+\s*[\.、)）]\s*|[一二三四五六七八九十]+\s*[、.]\s*)(.+)$"
)


def _from_markdown(text: str) -> SopDocument:
    chunks: List[List[str]] = []
    titles: List[str] = []
    current_title = ""
    current_body: List[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _MD_HEAD_RE.match(line.strip())
        if m and m.group(1).strip():
            if current_title or current_body:
                titles.append(current_title)
                chunks.append(current_body)
            current_title = m.group(1).strip()
            current_body = []
        else:
            current_body.append(line)
    if current_title or current_body:
        titles.append(current_title)
        chunks.append(current_body)

    steps: List[SopStep] = []
    for idx, (title, body) in enumerate(zip(titles, chunks), start=1):
        description = "\n".join(body).strip()
        if not title and not description:
            continue
        steps.append(
            SopStep(
                id=str(idx),
                name=title or f"步骤 {idx}",
                description=description,
                weight=1.0,
            )
        )
    if not steps:
        steps = [SopStep(id="1", name="整体 SOP", description=text, weight=1.0)]
    total = float(sum(s.weight for s in steps)) or 1.0
    return SopDocument(
        name="Markdown SOP",
        total_scoring_points=total,
        steps=steps,
        raw={"markdown": text},
    )


def sop_to_dict(sop: SopDocument) -> Dict[str, Any]:
    """把 ``SopDocument`` 反向序列化为标准 JSON（用于注入 system_instruction）。"""
    return {
        "sop_name": sop.name,
        "total_scoring_points": sop.total_scoring_points,
        "steps": [
            {
                "id": s.id,
                "step_name": s.name,
                "description": s.description,
                "scoring_criteria": s.scoring_criteria,
                "deduction_rule": s.deduction_rule,
                "keywords_required": s.keywords_required,
                "ai_recognition_clues": s.ai_recognition_clues,
                "aiReasoning": s.ai_reasoning,
                "forbidden_action": s.forbidden_action,
                "weight": s.weight,
            }
            for s in sop.steps
        ],
    }


# =====================================================================
# Function Declarations & 系统指令
# =====================================================================


def build_sop_tools() -> List[Dict[str, Any]]:
    """构造 Live API 期望的 tools 列表（单个 group 包含 3 个声明）。"""
    return [
        {
            "function_declarations": [
                {
                    "name": "log_step_event",
                    "description": (
                        "Emit ONE assessment event for the current SOP step. "
                        "Per-step lifecycle: exactly one STEP_START → one or more "
                        "PROCESSING (one call per KOP, NEVER merge multiple KOPs into "
                        "one PROCESSING) → exactly one STEP_COMPLETE. Never emit free "
                        "narration outside this tool."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "step_id": {
                                "type": "string",
                                "description": "Must match an id in the SOP.steps array.",
                            },
                            "step_name": {"type": "string"},
                            "event_type": {
                                "type": "string",
                                "enum": ["STEP_START", "PROCESSING", "STEP_COMPLETE"],
                            },
                            "status": {
                                "type": "string",
                                "enum": ["info", "succeed", "warning", "error"],
                            },
                            "description": {
                                "type": "string",
                                "description": "一句简体中文，描述当前瞬间观察到的事实。",
                            },
                            "kop_name": {
                                "type": "string",
                                "description": "PROCESSING 必填：本次评估的关键观察点名称。",
                            },
                            "weight_total": {
                                "type": "number",
                                "description": "该 KOP/步骤满分。",
                            },
                            "score_earned": {
                                "type": "number",
                                "description": "本 KOP 实际得分（有扣分理由时必须小于 weight_total）。",
                            },
                            "deduction_reasons": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "扣分原因列表，逐条简体中文。",
                            },
                            "ai_reasoning": {
                                "type": "string",
                                "description": "简短的评分逻辑链。",
                            },
                            "evidence_clue": {
                                "type": "string",
                                "description": "具体的视觉/听觉证据，例如'检测到双手戴绝缘手套'。",
                            },
                            "error_category": {
                                "type": "string",
                                "enum": ["safety", "sequence", "omission", "quality", "timeout"],
                            },
                            "error_severity": {
                                "type": "string",
                                "enum": ["low", "medium", "high", "critical"],
                            },
                        },
                        "required": ["step_id", "event_type", "status", "description"],
                    },
                },
                {
                    "name": "detect_spoken_keyword",
                    "description": (
                        "Call when the learner has audibly uttered a keyword that the "
                        "current SOP step lists in `keywords_required`. One call per hit."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "step_id": {"type": "string"},
                            "keyword": {"type": "string"},
                            "matched_phrase": {
                                "type": "string",
                                "description": "学员原句的简短摘录。",
                            },
                        },
                        "required": ["step_id", "keyword"],
                    },
                },
                {
                    "name": "flag_forbidden_action",
                    "description": (
                        "Call IMMEDIATELY when the learner performs an action listed in "
                        "the current SOP step's `forbidden_action`. Safety-critical."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "step_id": {"type": "string"},
                            "violation": {
                                "type": "string",
                                "description": "违规动作描述（简体中文）。",
                            },
                            "evidence_clue": {"type": "string"},
                            "severity": {
                                "type": "string",
                                "enum": ["low", "medium", "high", "critical"],
                            },
                        },
                        "required": ["step_id", "violation"],
                    },
                },
            ]
        }
    ]


_SOP_PROMPT_TEMPLATE = """# Role: 职业教育 AI 实训资深考官（实时通道）

你正在通过摄像头 + 麦克风**实时**观察一位学员执行下面这份 SOP。
你的唯一职责是：对照 SOP，**仅通过工具调用**上报每一个关键观察点的评估事件。

## 工作机制
- 严禁输出长段文本或自由语音；所有判定必须经由 `log_step_event` 等工具发送。
- 每一个 SOP 步骤必须经历完整生命周期：`STEP_START` → 多次 `PROCESSING`（每个 KOP 一条，禁止合并）→ `STEP_COMPLETE`。
- 学员口述命中 SOP 当前步骤 `keywords_required` 任意关键词时，立即调用 `detect_spoken_keyword`。
- 学员做出当前步骤 `forbidden_action` 列出的违规动作时，立即调用 `flag_forbidden_action`。
{voice_clause}

## 当前 SOP（JSON）
```json
{sop_json}
```

## 关键原则（违反则被系统纠正并重发）
1. **全局覆盖**：SOP 中共 {step_count} 个步骤，每一个都必须产生 `STEP_START` + `STEP_COMPLETE`，禁止"沉默跳过"。
2. **KOP 颗粒度**：步骤内的每一个观察点单独一条 `PROCESSING`，绝不合并。
3. **数学一致**：若 `deduction_reasons` 非空，则 `score_earned` 必须 **严格小于** `weight_total`。
4. **乱序处理**：
   - 学员跳跃执行（目标步骤 > 当前指针）→ 先为中间被跳过的步骤补 `warning` 的 START + COMPLETE，再处理目标步骤；
   - 学员回头补做（目标步骤 < 当前指针）→ 指针不回退；只发一条带 `[乱序补做 -1分]` 的 PROCESSING。
5. **证据可溯**：`evidence_clue` 必须填具体的视觉/听觉证据，禁止"看起来还行"这种空话。
6. **响应在步骤切换瞬间**：画面/语音明显切到下一步骤时，立刻 COMPLETE 上一个、START 下一个，不要等画面静止。

## 系统反馈
- 当你触发数学矛盾或步骤指针错乱时，会发回一条 `[CORRECTION]` 文本，请按要求重发事件。
- 收到 `[STATUS]` 状态简报时，请据此自检覆盖率并补齐遗漏。

现在开始监听，按规则输出工具调用。
"""

_VOICE_CLAUSE_SILENT = (
    "- **禁止任何语音输出**。即便学员主动提问，也只能继续按工具调用上报评估事件。"
)
_VOICE_CLAUSE_COACH = (
    "- **开启语音教练时请主动短评**：当出现 `warning/error`、触发 `flag_forbidden_action`、"
    "或每个 `STEP_COMPLETE` 结算时，都可以补一句不超过 18 字的简体中文口头反馈；"
    "优先指出“哪里不规范 + 立即怎么改”。"
)


def build_system_instruction(sop: SopDocument, *, voice_coach: bool = False) -> str:
    sop_json = json.dumps(sop_to_dict(sop), ensure_ascii=False, indent=2)
    return _SOP_PROMPT_TEMPLATE.format(
        sop_json=sop_json,
        step_count=len(sop.steps),
        voice_clause=_VOICE_CLAUSE_COACH if voice_coach else _VOICE_CLAUSE_SILENT,
    )


# =====================================================================
# 状态机
# =====================================================================


class StepStatus:
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(slots=True)
class StepState:
    step_id: str
    name: str
    weight: float
    status: str = StepStatus.PENDING
    score_earned: float = 0.0
    kops_logged: List[str] = field(default_factory=list)
    deductions: List[str] = field(default_factory=list)
    keyword_hits: List[str] = field(default_factory=list)


class SopAssessor:
    """单会话内的状态机 + 日志落盘 + 反向校验。"""

    def __init__(
        self,
        sop: SopDocument,
        *,
        session_id: str,
        log_dir: Path,
        voice_coach: bool = False,
    ) -> None:
        self.sop = sop
        self.session_id = session_id
        self.voice_coach = voice_coach
        self.started_at = time.time()

        self._order: List[str] = [s.id for s in sop.steps]
        self._step_index: Dict[str, int] = {sid: i for i, sid in enumerate(self._order)}
        self.states: Dict[str, StepState] = {
            s.id: StepState(step_id=s.id, name=s.name, weight=s.weight) for s in sop.steps
        }
        self._pointer: int = 0  # 指向 self._order 的下一个待执行 step 的索引

        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = log_dir / f"{session_id}.ndjson"
        # 写入会话头
        self._append_log(
            {
                "kind": "session_start",
                "sessionId": session_id,
                "sopName": sop.name,
                "totalMax": self.total_max,
                "stepCount": len(sop.steps),
                "voiceCoach": voice_coach,
                "timestamp": _now_iso(),
            }
        )

    # ----- 查询 -----
    @property
    def total_score(self) -> float:
        return round(sum(s.score_earned for s in self.states.values()), 2)

    @property
    def total_max(self) -> float:
        declared = self.sop.total_scoring_points
        return declared if declared > 0 else float(sum(s.weight for s in self.states.values()))

    @property
    def current_step_id(self) -> Optional[str]:
        if 0 <= self._pointer < len(self._order):
            return self._order[self._pointer]
        return None

    @property
    def log_path(self) -> Path:
        return self._log_path

    def snapshot(self) -> Dict[str, Any]:
        return {
            "sopName": self.sop.name,
            "currentStepId": self.current_step_id,
            "totalScore": self.total_score,
            "totalMax": self.total_max,
            "steps": [
                {
                    "stepId": s.step_id,
                    "name": s.name,
                    "weight": s.weight,
                    "status": s.status,
                    "scoreEarned": round(s.score_earned, 2),
                    "kopsLogged": list(s.kops_logged),
                    "deductions": list(s.deductions),
                    "keywordHits": list(s.keyword_hits),
                }
                for s in self.states.values()
            ],
        }

    # ----- 工具调用入口 -----
    def handle_tool_call(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """处理一次 tool_call，返回 {assessment, tool_response, correction}。"""
        args = args or {}
        if name == "log_step_event":
            return self._handle_step_event(args)
        if name == "detect_spoken_keyword":
            return self._handle_keyword(args)
        if name == "flag_forbidden_action":
            return self._handle_forbidden(args)
        return {
            "assessment": {"kind": "unknown_tool", "name": name, "args": args},
            "tool_response": {"result": "ignored", "reason": f"unknown tool: {name}"},
            "correction": None,
        }

    def status_report(self) -> str:
        """供调用方周期性把状态摘要回灌给模型（client_content 文本）。"""
        snap = self.snapshot()
        cur = snap["currentStepId"] or "已结束"
        pending = [s["stepId"] for s in snap["steps"] if s["status"] == StepStatus.PENDING]
        skipped = [s["stepId"] for s in snap["steps"] if s["status"] == StepStatus.SKIPPED]
        return (
            f"[STATUS] currentStepId={cur} totalScore={snap['totalScore']}/{snap['totalMax']} "
            f"pendingSteps={pending} skippedSteps={skipped}. "
            f"请自检覆盖率，对遗漏的 KOP 立即补 PROCESSING。"
        )

    def finalize(self) -> Dict[str, Any]:
        """会话结束时落最终汇总日志，返回给前端展示。"""
        # 尚未结束的步骤标记为 skipped
        for state in self.states.values():
            if state.status == StepStatus.PENDING:
                state.status = StepStatus.SKIPPED
            elif state.status == StepStatus.IN_PROGRESS:
                state.status = StepStatus.SKIPPED
        snap = self.snapshot()
        final = {
            "kind": "final_summary",
            "sessionId": self.session_id,
            "elapsedSec": round(time.time() - self.started_at, 2),
            "logPath": str(self._log_path),
            "timestamp": _now_iso(),
            **snap,
        }
        self._append_log(final)
        return final

    # ----- 内部：三类事件 -----
    def _handle_step_event(self, args: Dict[str, Any]) -> Dict[str, Any]:
        step_id = str(args.get("step_id") or "")
        event_type = str(args.get("event_type") or "")
        status = str(args.get("status") or "info")
        description = str(args.get("description") or "")
        kop_name = str(args.get("kop_name") or "").strip()
        weight_total = _to_float(args.get("weight_total"))
        score_earned = _to_float(args.get("score_earned"))
        deduction_reasons = [str(r) for r in (args.get("deduction_reasons") or []) if str(r).strip()]

        correction: Optional[str] = None
        state = self.states.get(step_id)
        if state is None:
            correction = (
                f"[CORRECTION] step_id={step_id!r} 不在当前 SOP 步骤集合内。"
                f"合法 step_id 列表：{self._order}。请按合法 id 重发事件。"
            )

        # 推进指针：仅在 STEP_START 时
        if state is not None and event_type == "STEP_START":
            target_idx = self._step_index[step_id]
            if target_idx > self._pointer:
                for i in range(self._pointer, target_idx):
                    sk = self.states[self._order[i]]
                    if sk.status == StepStatus.PENDING:
                        sk.status = StepStatus.SKIPPED
                self._pointer = target_idx
            state.status = StepStatus.IN_PROGRESS

        # PROCESSING：累计得分 + 数学校验 + KOP 列表
        if event_type == "PROCESSING" and state is not None:
            if (
                deduction_reasons
                and weight_total is not None
                and score_earned is not None
                and score_earned >= weight_total
            ):
                correction = (
                    f"[CORRECTION] step_id={step_id!r} kop={kop_name!r} 给出了扣分理由 "
                    f"{deduction_reasons}，但 score_earned={score_earned} >= "
                    f"weight_total={weight_total}。请重发该 PROCESSING 并调小 score_earned。"
                )
            if kop_name and kop_name not in state.kops_logged:
                state.kops_logged.append(kop_name)
            if score_earned is not None:
                state.score_earned += float(score_earned)
            if deduction_reasons:
                state.deductions.extend(deduction_reasons)

        # STEP_COMPLETE：定状态 + 推指针
        if event_type == "STEP_COMPLETE" and state is not None:
            if not state.kops_logged:
                state.status = StepStatus.SKIPPED
            elif status == "error":
                state.status = StepStatus.FAILED
            else:
                state.status = StepStatus.COMPLETED
            idx = self._step_index[step_id]
            if idx >= self._pointer:
                self._pointer = idx + 1

        assessment = {
            "kind": "step_event",
            "stepId": step_id,
            "stepName": str(args.get("step_name") or (state.name if state else "")),
            "eventType": event_type,
            "status": status,
            "description": description,
            "kopName": kop_name or None,
            "weightTotal": weight_total,
            "scoreEarned": score_earned,
            "deductionReasons": deduction_reasons,
            "aiReasoning": str(args.get("ai_reasoning") or "") or None,
            "evidenceClue": str(args.get("evidence_clue") or "") or None,
            "errorCategory": args.get("error_category"),
            "errorSeverity": args.get("error_severity"),
            "timestamp": _now_iso(),
            "snapshot": self.snapshot(),
        }
        self._append_log(assessment)

        return {
            "assessment": assessment,
            "tool_response": {
                "result": "ok" if correction is None else "needs_correction",
                "currentStepId": self.current_step_id,
                "totalScore": self.total_score,
                "totalMax": self.total_max,
            },
            "correction": correction,
        }

    def _handle_keyword(self, args: Dict[str, Any]) -> Dict[str, Any]:
        step_id = str(args.get("step_id") or "")
        keyword = str(args.get("keyword") or "").strip()
        matched = str(args.get("matched_phrase") or "")
        state = self.states.get(step_id)
        if state is not None and keyword and keyword not in state.keyword_hits:
            state.keyword_hits.append(keyword)
        assessment = {
            "kind": "keyword_hit",
            "stepId": step_id,
            "keyword": keyword,
            "matchedPhrase": matched,
            "timestamp": _now_iso(),
            "snapshot": self.snapshot(),
        }
        self._append_log(assessment)
        return {
            "assessment": assessment,
            "tool_response": {"result": "ok"},
            "correction": None,
        }

    def _handle_forbidden(self, args: Dict[str, Any]) -> Dict[str, Any]:
        step_id = str(args.get("step_id") or "")
        violation = str(args.get("violation") or "").strip()
        evidence = str(args.get("evidence_clue") or "")
        severity = str(args.get("severity") or "high")
        state = self.states.get(step_id)
        if state is not None and violation:
            state.deductions.append(f"[禁止行为] {violation}")
        assessment = {
            "kind": "forbidden_action",
            "stepId": step_id,
            "violation": violation,
            "evidenceClue": evidence or None,
            "severity": severity,
            "timestamp": _now_iso(),
            "snapshot": self.snapshot(),
        }
        self._append_log(assessment)
        return {
            "assessment": assessment,
            "tool_response": {"result": "noted", "severity": severity},
            "correction": None,
        }

    # ----- 持久化 -----
    def _append_log(self, event: Dict[str, Any]) -> None:
        try:
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                f.write("\n")
        except Exception:
            # 日志失败不能拖垮主流程
            pass


# =====================================================================
# 小工具
# =====================================================================


def _to_float(value: Any, *, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return time.strftime("%H:%M:%S", time.localtime(time.time()))
