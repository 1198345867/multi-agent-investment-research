"""写作 Agent：把黑板上的研究结果整合成一份投研报告。

由 state.review_round 区分两个阶段：
    review_round == 0   产出初稿
    review_round > 0    按审校意见产出修订稿

输出是 Markdown 正文而非结构化数据，因此走 chat 而不是 chat_json。
报告要求如实呈现风控质疑与分析师判定，未消除的分歧不得粉饰。
"""

from __future__ import annotations

from llm import chat
from state import ResearchState

SYSTEM_DRAFT = """你是投研团队的报告撰写人。
你的职责是把团队的研究结果原样整理成报告，不是做二次分析。

报告必须包含以下四个部分，使用 Markdown 二级标题：

## 一、核心结论
原样引用分析师的最终结论全文，不得改写、概括、增删判断，
禁止在这里提出分析师没有给出过的观点或投资建议。

## 二、支撑依据
按核心问题逐条组织，每个核心问题一个子段落：
先用一句话直接回答（回答取自素材数据或分析师结论），
再列出支撑该回答的素材数值，数值原样引用，不改算、不补算。
素材中确实没有对应数据的，如实说明数据缺失并给中性声明，
不要用其他数据替代，也不要自行推算。

## 三、风险与分歧
如实呈现风控提出的质疑及分析师的判定：
判定为「采纳」「部分采纳」的，写明结论已做的调整；
判定为「反驳」的，写明反驳依据。不得隐瞒仍然存在的分歧。

## 四、数据来源
列出用到的数据来源与抓取时间。

通用要求：
1. 全文数值只能来自素材，或来自分析师结论；
   不得自行推算素材未给出的派生值（如同比变动率、分位推断、行业对比数值）；
2. 不得添加素材与分析师结论之外的定性判断，
   如"行业顶级水平""呈空头排列""波动剧烈""增长预期逆转"这类评价性、趋势性结论；
   技术指标只呈现数值本身，由分析师结论去下判断；
   回答核心问题时直接罗列素材数值即可，不必额外给出判断性结论；
3. 素材中已有的数据必须如实呈现，不得以"口径存疑"等理由删除或改写，
   数据口径确有问题时应在第三部分说明，而不是在正文中隐去；
4. 不得使用"预计""或将"这类模糊限定词去描述素材中的已披露数据。"""

SYSTEM_REVISE = """你是投研团队的报告撰写人。
审校对上一版报告提出了必须修正的阻断项，请逐条修正。

要求：
1. 逐条修正下列阻断项，不要遗漏；只改被点到的位置；
2. 没有被点到的部分保持原样，不要整篇重写以致引入新错误；
3. 修改后仍须满足初稿的全部要求：
   核心结论原样引用分析师结论、每个核心问题有显式回答、
   数据与素材一致、不提出分析师结论之外的投资判断；
4. 修正过程中不得新增素材中没有的数值，也不得新增定性判断；
5. 直接输出修改后的完整报告，不要输出修改说明。"""


def writer(state: ResearchState) -> None:
    """产出初稿或修订稿，写入 state.draft。"""
    if state.review_round == 0:
        state.draft = chat(SYSTEM_DRAFT, _draft_input(state))
        state.log("writer", f"初稿完成（{len(state.draft)} 字）")
    else:
        state.draft = chat(SYSTEM_REVISE, _revise_input(state))
        state.log(
            "writer", f"第 {state.review_round} 轮修订稿完成（{len(state.draft)} 字）"
        )


def _draft_input(state: ResearchState) -> str:
    """整理写作素材：核心问题、数据、最终结论、辩论过程。"""
    focus = "\n".join(f"{i}. {q}" for i, q in enumerate(state.plan.focus_points, 1))
    challenges = "\n".join(
        f"- [风控·{c.severity}] {c.target}：{c.reason}" for c in state.challenges
    )
    rebuttals = "\n".join(
        f"- [{r.verdict}] {r.target}：{r.response}" for r in state.rebuttals
    )
    return (
        f"研究标的：{state.request.symbol}\n"
        f"用户提问：{state.request.question}\n\n"
        f"必须回答的核心问题：\n{focus}\n\n"
        f"素材数据：\n{state.materials_text()}\n\n"
        f"分析师最终结论（核心结论章节需原样引用以下全文）：\n"
        f"{state.analysis.conclusion}\n"
        f"支撑证据：{state.analysis.evidences}\n"
        f"估值判断：{state.analysis.valuation_view}\n\n"
        f"风控质疑：\n{challenges}\n\n"
        f"分析师回应：\n{rebuttals}"
    )


def _revise_input(state: ResearchState) -> str:
    """整理修订素材：上一版报告、阻断项、可供核对的数据。

    只喂阻断项。建议项不进入修订输入，避免为改措辞而牵动事实、
    把返工闭环拖长，这也是评审把问题分成两组的目的。
    """
    issues = "\n".join(
        f"{i}. {x}" for i, x in enumerate(state.last_review.blocking, 1)
    )
    return (
        f"上一版报告：\n{state.draft}\n\n"
        f"必须修正的阻断项：\n{issues}\n\n"
        f"可引用的素材数据：\n{state.materials_text()}\n\n"
        f"分析师最终结论：{state.analysis.conclusion}"
    )