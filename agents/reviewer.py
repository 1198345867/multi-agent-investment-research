"""审校 Agent：判定报告能否交付，不能则给出可执行的修改意见。

输出是结构化的发现清单，每条含位置、原句、问题、依据、是否阻断，
passed 由阻断项是否存在推导，不交给模型自行判定，避免"认为有问题却判通过"。

两条设计约束用来把返工闭环收敛住：
一、要求逐字引用报告原句，逼审校先核对原文再下结论，
    "报告漏写了某某"这类凭印象的断言一旦要求引用原句就会自我否定；
二、要求阻断项必须填写具体依据（素材字段名与数值、分析师结论原句、
    风控质疑原文），填不出依据就落不进 blocking。审校天然倾向于
    "再找点问题"，而"报告没披露修订史""报告没替分析师下结论"这类
    越界要求根本引不出素材依据，加了举证门槛后会被结构性地挡在阻断之外。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from llm import chat_json
from state import ResearchState, Review

SYSTEM = """你是投研团队的审校。逐条核对报告，对每条发现给出最终判定。

审校对象只是报告本身：核对报告是否忠实呈现素材数据与分析师的最终结论。
分析师结论自身的措辞、定义、贴切程度不在审校范围内，不得据此判报告有问题。

每条发现必须包含以下字段：
- location：问题在报告中的位置，如"二、支撑依据 第2段"；
- quote：报告中出问题的原句，逐字复制，不得改写或概括；
  若问题是"漏答"这类没有对应原句的情形，留空；
- problem：问题所在，一句话说清，不复述与素材逐条对比的推理过程；
- basis：判为阻断时必须填写具体依据——素材字段名与数值、
  分析师最终结论原句、或风控质疑原文；填不出具体依据的不得判为阻断；
- blocking：该问题是否必须修正才能交付。

阻断（blocking=true）只限以下三类：
1. 数值冲突：报告中的数值与素材数值实际冲突，
   或报告出现既不在素材、也不在分析师结论中的数值。
   素材中存在的数值、分析师结论中出现的数值（包括其中的派生值），
   报告如实引用或呈现，均不构成编造；
2. 核心问题漏答：对照"必须回答的核心问题"逐条检查，
   某条明确要求的指标在素材中有现成数据，报告却没有呈现该数据；
   不得自行扩展核心问题没有要求的指标维度；
3. 风险与分歧未呈现：风控提出的质疑、或分析师的判定，
   没有在报告的风险与分歧部分如实呈现。
   报告如实呈现了质疑与判定即算合规，
   不要求报告额外声明"该问题曾出错/现已修正"。

以下一律 blocking=false，只作建议记录：
4. 措辞差异：报告改写、概括、语气不同等，只要没有改变投资建议方向，
   一律列为建议；核心结论章节应原样引用分析师结论，但出现措辞差异时
   也只在建议中提示，不作为阻断；
5. 表述严谨、计算精度、结构排版；
6. 单位标注：素材已声明 unit_note（如"比率类单位为百分数"），
   报告按该口径呈现即为一致，不得以"未标注单位""口径可能不同"这类
   推测性理由判为数值冲突；
7. 报告基于素材数值做的一般性解读与常识性判断，
   只要不改变投资建议方向、不引入素材之外的数值；
8. 素材中没有的派生值（如未提供的分位、同比率、行业对比数值），
   报告没有计算或呈现，不算漏答；
   素材无法回答的部分，报告如实说明数据缺失或无法判断，不算漏答。

审校边界：
1. 只审当前报告内容本身，不涉及报告的形成过程或修订历史；
2. 报告按核心问题逐条作答，回答"是/否/数据不足"都是正常作答；
   报告无须自行下结论，只须如实呈现素材数据并引用分析师结论。

硬性要求：
1. 只写入确实存在、可以改进的问题；已核对无误的内容不要写入 issues；
2. 先引用原句再判定。若你无法在报告中逐字引用到出问题的原句
   （漏答类除外），说明该问题不成立，不要写入 issues；
3. 只写最终判定，禁止写推理过程、改主意的过程或自我推翻的话；
4. 一条发现只讲一个问题；
5. 不要因为"还能写得更好"就把报告判为不可交付。"""


class _Issue(BaseModel):
    """一条审校发现。blocking 决定它是否阻断交付，basis 是阻断的举证。"""

    location: str
    quote: str = ""
    problem: str
    basis: str = ""
    blocking: bool


class _ReviewOut(BaseModel):
    """审校输出。passed 由阻断项推导，不作为字段交给模型填写。"""

    issues: list[_Issue] = Field(default_factory=list)

    @property
    def blocking(self) -> list[_Issue]:
        return [i for i in self.issues if i.blocking]

    @property
    def advisory(self) -> list[_Issue]:
        return [i for i in self.issues if not i.blocking]

    @property
    def passed(self) -> bool:
        return not self.blocking


def _describe(issue: _Issue) -> str:
    """把结构化发现压成一行可读文本，供写作 Agent 与日志使用。"""
    parts = [issue.location, issue.problem]
    if issue.quote:
        parts.append(f"原句：{issue.quote}")
    if issue.basis:
        parts.append(f"依据：{issue.basis}")
    return "｜".join(parts)


def reviewer(state: ResearchState) -> None:
    """评审当前草稿，追加写入 state.reviews。"""
    focus = "\n".join(f"{i}. {q}" for i, q in enumerate(state.plan.focus_points, 1))
    user = (
        f"研究标的：{state.request.symbol}\n"
        f"待审报告：\n{state.draft}\n\n"
        f"必须回答的核心问题：\n{focus}\n\n"
        f"素材数据（唯一可信的数据来源）：\n{state.materials_text()}\n\n"
        f"分析师最终结论：{state.analysis.conclusion}\n"
        f"结论支撑证据：{state.analysis.evidences}"
    )

    out = chat_json(SYSTEM, user, _ReviewOut)
    state.reviews.append(
        Review(
            round=state.review_round,
            passed=out.passed,
            blocking=[_describe(i) for i in out.blocking],
            advisory=[_describe(i) for i in out.advisory],
        )
    )
    if out.passed:
        state.log(
            "reviewer",
            f"第 {state.review_round} 轮评审通过（建议 {len(out.advisory)} 条）",
        )
    else:
        state.log(
            "reviewer",
            f"第 {state.review_round} 轮评审未通过："
            f"阻断 {len(out.blocking)} 条，建议 {len(out.advisory)} 条",
        )