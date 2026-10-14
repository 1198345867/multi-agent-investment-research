"""共享黑板：多 Agent 协同的唯一状态载体。

所有 Agent 通过读写同一个 ResearchState 交换信息，不做两两直接通信，
因此不存在消息风暴和状态不一致的问题。

分区归属（每个分区只有一个写入者，其余 Agent 只读）：
    plan                        规划 Agent
    materials                   采集 Agent
    analyses                    分析 Agent，每次产出追加一条，全部版本保留
    challenges                  风控 Agent
    rebuttals                   分析 Agent
    draft                       写作 Agent
    reviews                     审校 Agent
    report                      引擎在评审通过后落定
    metrics                     引擎记录每个环节的耗时与 token
    debate_round / review_round  引擎维护轮次，Agent 只读

生命周期：
    黑板只在单次 engine.run() 内存在，运行结束即销毁。
    跨轮持久化只保留摘要，见 memory/store.py。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# 采集 Agent 支持的数据项类型，同时约束规划 Agent 的输出取值范围
DataKind = Literal[
    "price", "financials", "profile", "news", "technical", "valuation"
]

# 质疑的严重程度，约束风控 Agent 的输出取值
Severity = Literal["高", "中", "低"]

# 分析 Agent 对单条质疑的处理结果
Verdict = Literal["采纳", "部分采纳", "反驳"]


class MemoryContext(BaseModel):
    """规划 Agent 单点注入的跨轮记忆上下文。

    对应持久记忆的三层结构（会话脉络、用户画像、标的档案）。
    只在流程入口读取一次，后续 Agent 一律从黑板获取，不再回查记忆库。
    """

    session_summary: str = ""
    user_profile: dict = Field(default_factory=dict)
    symbol_archive: dict = Field(default_factory=dict)


class ResearchRequest(BaseModel):
    """本轮研究请求。"""

    question: str
    symbol: str
    user_id: str = "default"


class Plan(BaseModel):
    """规划 Agent 产出：本轮要回答什么、需要哪些数据。"""

    symbol: str
    company: str
    focus_points: list[str] = Field(default_factory=list)
    data_tasks: list[DataKind] = Field(default_factory=list)


class Material(BaseModel):
    """采集 Agent 产出的一条素材。

    payload 直接存放工具返回值原文，不改写，保证可追溯。
    """

    kind: DataKind
    payload: dict
    note: str = ""


class Analysis(BaseModel):
    """分析 Agent 的一次结论产出。

    round 为 0 表示初版，大于 0 表示第 round 轮辩论后的修订版。
    每次产出都追加到黑板的 analyses，因此全部版本都保留，可直接对比演进。
    """

    round: int
    conclusion: str
    evidences: list[str] = Field(default_factory=list)
    valuation_view: str = ""


class Challenge(BaseModel):
    """风控 Agent 产出的一条质疑。"""

    round: int
    target: str
    reason: str
    severity: Severity


class Rebuttal(BaseModel):
    """分析 Agent 对质疑的逐条回应。"""

    round: int
    target: str
    response: str
    verdict: Verdict


class Review(BaseModel):
    """审校 Agent 的一轮评审结果。

    阻断项与建议项分开存放：阻断项决定能否交付，也是写作 Agent 修订的唯一依据；
    建议项只作记录，不进入返工闭环，避免为改措辞而牵动事实、把闭环拖长。
    """

    round: int
    passed: bool
    blocking: list[str] = Field(default_factory=list)
    advisory: list[str] = Field(default_factory=list)


class StepMetric(BaseModel):
    """一个执行环节的耗时与模型消耗，用于定位慢在哪、贵在哪。"""

    name: str
    seconds: float
    calls: int
    total_tokens: int


class ResearchState(BaseModel):
    """共享黑板本体。"""

    request: ResearchRequest
    memory_ctx: MemoryContext = Field(default_factory=MemoryContext)

    plan: Plan | None = None
    materials: list[Material] = Field(default_factory=list)
    analyses: list[Analysis] = Field(default_factory=list)
    challenges: list[Challenge] = Field(default_factory=list)
    rebuttals: list[Rebuttal] = Field(default_factory=list)
    draft: str = ""
    reviews: list[Review] = Field(default_factory=list)
    report: str = ""

    debate_round: int = 0
    review_round: int = 0
    trace: list[str] = Field(default_factory=list)
    metrics: list[StepMetric] = Field(default_factory=list)

    def log(self, actor: str, message: str) -> None:
        """记录一步流程轨迹，用于演示协同过程与排查问题。"""
        stamp = datetime.now().strftime("%H:%M:%S")
        self.trace.append(f"[{stamp}] {actor} | {message}")

    def material(self, kind: str) -> dict | None:
        """按类型取回素材，供分析与风控 Agent 引用具体数据。"""
        for item in self.materials:
            if item.kind == kind:
                return item.payload
        return None

    def materials_text(self) -> str:
        """把素材拼成紧凑文本，供需要阅读数据的 Agent 使用。"""
        return "\n".join(
            f"【{item.kind}】{json.dumps(item.payload, ensure_ascii=False)}"
            for item in self.materials
        )

    @property
    def analysis(self) -> Analysis | None:
        """当前最新结论，供下游 Agent 直接读取。全部版本见 analyses。"""
        return self.analyses[-1] if self.analyses else None

    @property
    def last_review(self) -> Review | None:
        """最近一轮评审结果，供引擎判断是否还需返工。"""
        return self.reviews[-1] if self.reviews else None