"""规划 Agent：把用户提问拆成本轮要回答的核心问题与数据采集清单。

同时承担跨轮记忆的注入点：持久记忆只在这里读取一次并写入黑板，
后续 Agent 一律从黑板获取，不再回查记忆库。
"""

from __future__ import annotations

from agents.researcher import DATA_CAPABILITIES
from llm import chat_json
from state import Plan, ResearchState

SYSTEM = """你是投研团队的规划分析师。
根据用户提问与历史记忆，拆解出本轮研究需要回答的核心问题，
并列出为回答这些问题需要采集的数据项。

要求：
1. 核心问题控制在 3 到 5 个，聚焦对投资决策有实际影响的问题；
2. 核心问题必须能用下面列出的数据回答。
   数据覆盖不到的方面（例如股东户数、渠道库存、产品吨价、经销商政策）
   一律不要作为核心问题提出，否则报告只能回答"数据缺失"；
3. 数据项只从 schema 允许的取值中选择，且必须覆盖回答问题所需的全部数据；
4. 如果历史记忆中已有该标的的研究结论，核心问题应侧重"与上次相比发生了什么变化"。"""


def planner(state: ResearchState) -> None:
    """产出研究计划，写入 state.plan。"""
    memory = state.memory_ctx
    capabilities = "\n".join(
        f"- {kind}：{desc}" for kind, desc in DATA_CAPABILITIES.items()
    )
    user = (
        f"用户提问：{state.request.question}\n"
        f"标的代码：{state.request.symbol}\n\n"
        f"可采集的数据项及其包含的字段：\n{capabilities}\n\n"
        f"历史会话脉络：{memory.session_summary or '无'}\n"
        f"用户偏好：{memory.user_profile or '无'}\n"
        f"该标的历史研究结论：{memory.symbol_archive or '无'}"
    )

    plan = chat_json(SYSTEM, user, Plan)
    # 标的代码以用户请求为准，不允许模型改写
    plan.symbol = state.request.symbol
    state.plan = plan
    state.log(
        "planner",
        f"规划完成：{len(plan.focus_points)} 个核心问题，"
        f"采集 {'/'.join(plan.data_tasks)}",
    )