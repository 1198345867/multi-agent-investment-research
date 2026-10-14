"""分析 Agent：基于黑板素材形成结论，并逐条回应风控质疑。

同一个 Agent 承担两个阶段的职责，由 state.debate_round 区分：
    debate_round == 0   产出初版结论
    debate_round > 0    先逐条回应上一轮质疑，再据此修订结论

每次产出都追加到 state.analyses，初版与各轮修订版因此全部保留，
报告可以直接呈现结论的演进过程。
"""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel

from llm import chat_json
from state import Analysis, Rebuttal, ResearchState, Verdict

SYSTEM_FIRST = """你是投研团队的行业分析师。
基于提供的结构化数据形成分析结论。

要求：
1. evidences 中每一条都必须引用素材里的具体数字，不允许出现素材中没有的数据；
2. conclusion 要给出明确判断，不要模棱两可；
3. valuation_view 单独说明估值处于什么水平。"""

SYSTEM_REVISE = """你是投研团队的行业分析师。
风控对你的结论提出了质疑，请逐条回应。

判定 verdict 的标准：
1. 「反驳」：质疑的前提与素材数据不符，或质疑本身没有数据依据。
   只要质疑站不住脚就必须填「反驳」，不要因为对方是风控就让步；
2. 「采纳」：质疑成立，你的结论确实有误，必须修正；
3. 「部分采纳」：质疑方向有效，但不足以推翻原判断。

要求：
1. 判定依据是素材中的具体数据，而不是质疑的措辞是否严厉；
2. 填「采纳」或「部分采纳」时必须据此修正 conclusion；
3. 修正 conclusion 时不要添加没有数据支撑的限定词，
   例如把已披露的财报数据改写成"预计"；
4. 修订后的 conclusion 必须自洽，不能同时保留相互矛盾的表述。"""


class _RebuttalOut(BaseModel):
    """单条回应的输出结构。round 由 Agent 打标，不交给模型填写。"""

    target: str
    response: str
    verdict: Verdict


class _AnalysisOut(BaseModel):
    """结论产出结构。round 由 Agent 打标，不交给模型填写。"""

    conclusion: str
    evidences: list[str]
    valuation_view: str


class _RevisionOut(_AnalysisOut):
    """修订阶段的输出：在结论之外附带对每条质疑的回应。"""

    rebuttals: list[_RebuttalOut]


def analyst(state: ResearchState) -> None:
    """分析 Agent 入口，按辩论轮次分派到初版或修订。"""
    if state.debate_round == 0:
        out = chat_json(SYSTEM_FIRST, state.materials_text(), _AnalysisOut)
        state.analyses.append(
            Analysis(
                round=0,
                conclusion=out.conclusion,
                evidences=out.evidences,
                valuation_view=out.valuation_view,
            )
        )
        state.log("analyst", "初版结论完成")
    else:
        _revise(state)


def _revise(state: ResearchState) -> None:
    """逐条回应上一轮质疑，并据此修订结论。"""
    current = state.analysis
    challenges = [c for c in state.challenges if c.round == state.debate_round]
    challenge_text = "\n".join(
        f"[{c.severity}] {c.target}：{c.reason}" for c in challenges
    )
    user = (
        f"你此前的结论：{current.conclusion}\n"
        f"你此前的证据：{current.evidences}\n"
        f"可用素材：\n{state.materials_text()}\n\n"
        f"风控的质疑：\n{challenge_text}"
    )

    result = chat_json(SYSTEM_REVISE, user, _RevisionOut)

    for item in result.rebuttals:
        state.rebuttals.append(
            Rebuttal(
                round=state.debate_round,
                target=item.target,
                response=item.response,
                verdict=item.verdict,
            )
        )
    state.analyses.append(
        Analysis(
            round=state.debate_round,
            conclusion=result.conclusion,
            evidences=result.evidences,
            valuation_view=result.valuation_view,
        )
    )

    tally = Counter(item.verdict for item in result.rebuttals)
    state.log(
        "analyst",
        f"第 {state.debate_round} 轮回应完成：质疑 {len(result.rebuttals)} 条，"
        f"采纳 {tally['采纳']} 条，部分采纳 {tally['部分采纳']} 条，"
        f"反驳 {tally['反驳']} 条",
    )