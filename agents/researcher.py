"""采集 Agent：按规划清单调用数据工具，把素材原文搬进黑板。

本 Agent 不调用 LLM。采集是确定性动作，工具返回什么就存什么，
好处是素材天然可追溯（带 source 与 fetched_at），
也不会因为模型改写而丢失原始数据。
"""

from __future__ import annotations

from collections.abc import Callable

from state import DataKind, Material, ResearchState
from tools import calc, data

# 数据项类型到工具函数的映射，覆盖 DataKind 的全部取值
FETCHERS: dict[DataKind, Callable[[str], dict]] = {
    "price": data.get_price,
    "financials": data.get_financials,
    "profile": data.get_profile,
    "news": data.get_news,
    "technical": calc.calc_technical,
    "valuation": calc.calc_valuation,
}

# 各数据项实际包含的字段，注入规划 Agent 的提示词。
# 规划 Agent 只有知道数据能提供什么，才不会提出这些数据无法回答的问题，
# 从而避免"报告如实说明数据缺失、却被审校判为漏答"的死循环。
DATA_CAPABILITIES: dict[DataKind, str] = {
    "price": "日线行情：起止区间、交易日数、最新收盘价与涨跌幅、换手率、"
    "区间最高/最低价、区间收益率",
    "financials": "近 3 个年报期的财务摘要：营业总收入、归母净利润、扣非净利润、"
    "经营现金流量净额、净资产收益率(ROE)、毛利率、销售净利率、资产负债率、"
    "营业总收入与归母净利润增长率",
    "profile": "公司概况：公司全称与简称、所属行业与市场、上市日期、主营业务",
    "news": "近期个股新闻：标题、发布时间、来源媒体，不含正文",
    "technical": "技术指标：MA5/MA20/MA60、是否站上 MA20、区间收益率、"
    "年化波动率、最大回撤",
    "valuation": "估值水平：PE(TTM)、PE(静)、PB、PEG、总市值（亿元）、"
    "PE 近三年历史分位",
}


def researcher(state: ResearchState) -> None:
    """按 state.plan.data_tasks 逐项取数，写入 state.materials。"""
    symbol = state.request.symbol
    for task in state.plan.data_tasks:
        state.materials.append(Material(kind=task, payload=FETCHERS[task](symbol)))
    state.log("researcher", f"采集完成：{len(state.materials)} 条素材")