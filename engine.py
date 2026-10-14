"""流程引擎：把各 Agent 按固定顺序串成一次研究流程。

编排逻辑显式写死，不用通用图引擎：本流程是固定主链加两条回边，
用代码直述比用图描述更易读、也更容易改。

执行顺序：
    planner -> researcher -> analyst(初版)
    -> [risk -> analyst(修订)] x DEBATE_ROUNDS
    -> writer(初稿)
    -> [reviewer -> writer(修订)] 直到评审通过或达到 MAX_REVIEW_ROUNDS

关于"可复现"：
    编排顺序完全确定，每次运行的流转一致；
    黑板 trace 逐步记录，可逐轮核对协同过程。
    LLM 输出本身带随机性，本引擎保证的是流程可复现，
    而不是结论逐字可复现。

关于"中止"：
    run 接受一个可选的中止判据 should_stop，每进入一个环节前询问一次。
    判据为真即抛 RunCancelled 停止本轮，且不写记忆、不存档——
    中止的轮次当作没发生过，不留半成品污染跨轮记忆与历史存档。

关于"可观测"：
    每个环节的耗时与模型消耗记入 state.metrics，
    供界面定位慢在哪、贵在哪，不参与流程判断。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from agents.analyst import analyst
from agents.planner import planner
from agents.researcher import researcher
from agents.reviewer import reviewer
from agents.risk import risk
from agents.writer import writer
from llm import usage_snapshot
from memory import reports, store
from state import ResearchState, StepMetric

# 对抗辩论轮数：每轮由风控提出质疑、分析师逐条回应并修订结论
DEBATE_ROUNDS = 2

# 评审打回的最大轮数，超出则不再无限返工，转为人工介入
MAX_REVIEW_ROUNDS = 3


class RunCancelled(Exception):
    """用户主动中止本轮流程。中止不留产出，也不回写记忆与存档。"""


def run(
    state: ResearchState, should_stop: Callable[[], bool] | None = None
) -> ResearchState:
    """执行一次完整研究流程，返回写满结果的黑板。

    should_stop 为可选的无参判据，返回真表示请求中止。
    """
    state.log("engine", "流程启动")
    # 跨轮记忆在流程入口读取一次并注入黑板，后续 Agent 不再回查记忆库
    state.memory_ctx = store.load(state.request.user_id, state.request.symbol)

    def step(agent: Callable[[ResearchState], None], name: str) -> None:
        """执行一个环节：先确认是否中止，再计时并统计该环节的模型消耗。"""
        if should_stop is not None and should_stop():
            state.log("engine", f"收到中止请求，「{name}」未执行")
            raise RunCancelled
        before = usage_snapshot()
        started = time.perf_counter()
        agent(state)
        after = usage_snapshot()
        state.metrics.append(
            StepMetric(
                name=name,
                seconds=round(time.perf_counter() - started, 1),
                calls=after["calls"] - before["calls"],
                total_tokens=after["total_tokens"] - before["total_tokens"],
            )
        )

    step(planner, "规划")
    step(researcher, "采集")
    step(analyst, "初版分析")  # debate_round 为 0，产出初版结论

    for round_no in range(1, DEBATE_ROUNDS + 1):
        state.debate_round = round_no
        step(risk, "风控质疑")
        step(analyst, "分析修订")  # 逐条回应本轮质疑并修订结论

    _write_and_review(state, step)

    # 只有正常跑完的轮次才回写：记忆存摘要供下轮规划，存档存整块黑板供回看
    store.save(state)
    reports.save(state)

    state.log(
        "engine",
        f"流程结束：结论 {len(state.analyses)} 版，"
        f"质疑 {len(state.challenges)} 条，回应 {len(state.rebuttals)} 条，"
        f"评审 {len(state.reviews)} 轮",
    )
    return state


def _write_and_review(
    state: ResearchState, step: Callable[[Callable, str], None]
) -> None:
    """写作与评审闭环：优先以通过评审的方式定稿。

    评审始终不通过时交付最后一版修订稿作为 best-effort 输出。
    消费者可用 state.last_review.passed 区分报告是定稿还是未经认可的稿子。
    """
    step(writer, "写作")

    for round_no in range(1, MAX_REVIEW_ROUNDS + 1):
        state.review_round = round_no
        step(reviewer, "评审")
        if state.last_review.passed:
            state.report = state.draft
            state.log("engine", f"第 {round_no} 轮评审通过，报告定稿")
            return
        step(writer, "修订")  # 带本轮阻断项修订

    state.report = state.draft
    state.log(
        "engine",
        f"连续 {MAX_REVIEW_ROUNDS} 轮评审未通过，"
        f"按 best-effort 交付最后一版修订稿，未经审校认可",
    )