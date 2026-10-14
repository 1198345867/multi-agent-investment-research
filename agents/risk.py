"""风控 Agent：对分析结论提出可验证的具体质疑。

质疑是本系统最核心的协同机制。风控必须给出具体、可验证的理由，
而不是泛泛而谈的风险提示；同时也允许它在结论确实稳健时诚实标低风险，
避免退化成"为质疑而质疑"的噪音制造者。
"""

from __future__ import annotations

from pydantic import BaseModel

from llm import chat_json
from state import Challenge, ResearchState, Severity

SYSTEM = """你是投研团队的风控专员。
你的职责是复核分析师的结论，并对确实站不住脚的地方提出质疑。

要求：
1. 必须逐项复核分析师的数值比较。凡是结论或证据中出现"高于/低于/超过/
   回落/下滑/上升"这类比较判断的地方，都要用素材中的原始数字重新核对
   方向与幅度；
2. 复核结果分两处填写，两个字段互不重叠：
   verified：核对一致的项，一句话说明核对了什么、结论是什么；
   challenges：发现方向写反、幅度算错等真实问题的项；
   核对无误的内容绝不能出现在 challenges 里；
3. 禁止对数据本身的真实性、时效做无据猜测。素材已带 data_nature、
   source、fetched_at 字段，报告期的数据性质已在素材中明确声明，
   不得再凭当前日期推断某期数据是否已经披露；
4. 每条质疑必须指向结论中的某个具体要点（写入 target），
   并以素材中的具体数据作为理由（reason）；
5. 不允许提出"需关注宏观经济风险"这类无法验证的泛泛之谈；
6. challenges 不超过 4 条，按对投资决策的影响程度从高到低排列；
7. 确实找不到成立的问题时，challenges 允许为空，不要为了凑数而质疑。"""


class _ChallengeOut(BaseModel):
    """单条质疑的输出结构。round 由 Agent 打标，不交给模型填写。"""

    target: str
    reason: str
    severity: Severity


class _RiskOut(BaseModel):
    """风控 Agent 的输出包装。

    把"核对无误"与"确认有问题"拆成两个字段，用输出结构而不是提示词
    保证确认项不会混进质疑流；下游只取 challenges，verified 仅用于统计。
    """

    verified: list[str]
    challenges: list[_ChallengeOut]


def risk(state: ResearchState) -> None:
    """对当前分析结论提出质疑，追加写入 state.challenges。"""
    user = (
        f"当前研究标的：{state.request.symbol}\n"
        f"分析结论：{state.analysis.conclusion}\n"
        f"支撑证据：{state.analysis.evidences}\n"
        f"估值判断：{state.analysis.valuation_view}\n\n"
        f"可用素材（用于逐项核对该结论中的数值比较是否与原始数据一致）："
        f"\n{state.materials_text()}"
    )

    result = chat_json(SYSTEM, user, _RiskOut)
    for item in result.challenges:
        state.challenges.append(
            Challenge(
                round=state.debate_round,
                target=item.target,
                reason=item.reason,
                severity=item.severity,
            )
        )
    state.log(
        "risk",
        f"第 {state.debate_round} 轮复核 {len(result.verified)} 项，"
        f"提出质疑 {len(result.challenges)} 条",
    )