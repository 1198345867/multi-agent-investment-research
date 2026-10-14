"""基于行情与估值数据的确定性计算工具。

计算类工具内部自行取数，只把结论返回给上游，
避免原始序列进入模型上下文造成体积膨胀。
"""

from __future__ import annotations

import numpy as np

from tools.cache import cached
from tools.data import (
    SOURCE,
    _num,
    _stamp,
    fetch_price_frame,
    fetch_valuation_frame,
)

# A 股年均交易日数量，用于波动率年化
TRADING_DAYS_PER_YEAR = 252


@cached("technical")
def calc_technical(code: str, days: int = 120) -> dict:
    """技术面指标：均线位置、区间表现、年化波动率与最大回撤。"""
    df = fetch_price_frame(code, days)
    close = df["close"].astype(float)

    def moving_average(window: int) -> float | None:
        if len(close) < window:
            return None
        return float(close.tail(window).mean())

    ma5, ma20, ma60 = moving_average(5), moving_average(20), moving_average(60)
    latest = float(close.iloc[-1])

    returns = close.pct_change().dropna()
    annual_volatility = float(returns.std() * np.sqrt(TRADING_DAYS_PER_YEAR) * 100)

    drawdown = (close - close.cummax()) / close.cummax()

    return {
        "symbol": code,
        "window_days": int(len(close)),
        "close": _num(latest),
        "ma5": _num(ma5),
        "ma20": _num(ma20),
        "ma60": _num(ma60),
        # 价格站上 20 日均线是常用的中期趋势判断依据
        "above_ma20": None if ma20 is None else latest > ma20,
        "period_return_pct": _num((latest / float(close.iloc[0]) - 1) * 100),
        "annual_volatility_pct": _num(annual_volatility),
        "max_drawdown_pct": _num(float(drawdown.min()) * 100),
        "source": SOURCE,
        "fetched_at": _stamp(),
    }


@cached("valuation")
def calc_valuation(code: str, lookback_days: int = 750) -> dict:
    """估值水平：最新 PE/PB/PEG 及 PE 的历史分位。

    分位数基于最近 lookback_days 个交易日（约三年）的 PE(TTM) 序列，
    分位越低表示当前估值相对自身历史越便宜。
    """
    df = fetch_valuation_frame(code).tail(lookback_days)
    latest = df.iloc[-1]

    pe_series = df["PE(TTM)"].astype(float).dropna()
    pe_now = float(latest["PE(TTM)"])
    pe_percentile = float((pe_series <= pe_now).mean() * 100)

    return {
        "symbol": code,
        "date": str(latest["数据日期"]),
        "pe_ttm": _num(pe_now),
        "pe_static": _num(latest["PE(静)"]),
        "pb": _num(latest["市净率"]),
        "peg": _num(latest["PEG值"]),
        # 总市值换算为亿元，便于阅读
        "market_cap_yi": _num(float(latest["总市值"]) / 1e8),
        "pe_percentile_pct": _num(pe_percentile, digits=1),
        "lookback_days": int(len(df)),
        "source": SOURCE,
        "fetched_at": _stamp(),
    }