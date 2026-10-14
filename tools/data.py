"""行情与财报数据工具，数据源为 akshare。

设计约定：
1. 只做确定性取数，不做任何分析判断；
2. 对外只返回结构化摘要，原始 DataFrame 不越过工具层；
3. 每个返回值都带 source 与 fetched_at，供审校 Agent 追溯核验。

接口说明（已实测 akshare 1.19.1）：
- stock_zh_a_daily           日线行情（新浪），东财 stock_zh_a_hist 端点持续断连已弃用
- stock_financial_abstract   财务摘要，宽表结构：指标为行、报告期为列
- stock_value_em             估值历史序列
- stock_profile_cninfo       公司概况
- stock_news_em              个股新闻
- stock_info_a_code_name     全部 A 股代码与名称对照，用于按名称反查代码
- stock_individual_info_em   已失效，不再使用
"""

from __future__ import annotations

from datetime import datetime, timedelta
import unicodedata

import akshare as ak
import pandas as pd

from tools.cache import cached

SOURCE = "akshare"

# 财报摘要需要提取的指标。
# 用 (选项, 指标) 作唯一键，因为不同选项分组下存在同名指标。
KEY_INDICATORS: tuple[tuple[str, str], ...] = (
    ("常用指标", "营业总收入"),
    ("常用指标", "归母净利润"),
    ("常用指标", "扣非净利润"),
    ("常用指标", "经营现金流量净额"),
    ("盈利能力", "净资产收益率(ROE)"),
    ("盈利能力", "毛利率"),
    ("盈利能力", "销售净利率"),
    ("财务风险", "资产负债率"),
    ("成长能力", "营业总收入增长率"),
    ("成长能力", "归属母公司净利润增长率"),
)


def _stamp() -> str:
    """统一的数据获取时间戳。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _num(value, digits: int = 2) -> float | None:
    """数值清洗：空值与 NaN 统一转成 None，保证结果可被 JSON 序列化。"""
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def _clean_name(value) -> str:
    """名称归一化：去空白并把全角字母数字转半角，便于按名称搜索。

    akshare 的代码名称表里，较短的名字会用全角空格补齐（如"五 粮 液"），
    A/B 股后缀又常用全角字母（如"万科Ａ"）；不归一化会导致按名称搜不到。
    """
    return unicodedata.normalize("NFKC", str(value)).replace(" ", "")


def _sina_symbol(code: str) -> str:
    """把 6 位股票代码转成新浪接口要求的市场前缀格式。"""
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def fetch_price_frame(code: str, days: int) -> pd.DataFrame:
    """取前复权日线数据，只保留最近 days 个交易日。

    新浪接口返回的列名为英文：date/open/high/low/close/volume/amount/turnover。
    注意 turnover 为小数比例（0.0044 表示 0.44%），对外输出前需乘 100。
    供本包的计算类工具复用，不建议直接暴露给 Agent。
    """
    end = datetime.now()
    start = end - timedelta(days=int(days * 2))
    df = ak.stock_zh_a_daily(
        symbol=_sina_symbol(code),
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        adjust="qfq",
    )
    return df.tail(days).reset_index(drop=True)


def fetch_valuation_frame(code: str) -> pd.DataFrame:
    """取估值历史序列，供本包的计算类工具复用。"""
    return ak.stock_value_em(symbol=code)


@cached("price")
def get_price(code: str, days: int = 120) -> dict:
    """个股日线行情摘要。

    只返回区间高低点与最新一日表现，不返回完整价格序列。
    """
    df = fetch_price_frame(code, days)
    close = df["close"].astype(float)
    last = df.iloc[-1]
    return {
        "symbol": code,
        "window": f"{df.iloc[0]['date']} ~ {last['date']}",
        "trading_days": int(len(df)),
        "latest": {
            "date": str(last["date"]),
            "close": _num(last["close"]),
            # 新浪接口本身不含涨跌幅，由相邻两日收盘价计算
            "pct_change": _num(
                (float(close.iloc[-1]) / float(close.iloc[-2]) - 1) * 100
            ),
            "turnover_rate": _num(float(last["turnover"]) * 100),
        },
        "period_high": _num(df["high"].max()),
        "period_low": _num(df["low"].min()),
        "period_return_pct": _num(
            (float(close.iloc[-1]) / float(close.iloc[0]) - 1) * 100
        ),
        "source": SOURCE,
        "fetched_at": _stamp(),
    }


@cached("financials")
def get_financials(code: str, years: int = 3) -> dict:
    """最近若干年报期的核心财务指标摘要。

    只取年报期（报告期以 1231 结尾），保证各期同比口径一致。
    """
    df = ak.stock_financial_abstract(symbol=code)
    periods = sorted(
        [c for c in df.columns if str(c).endswith("1231")], reverse=True
    )[:years]

    indicators: dict[str, dict[str, float | None]] = {}
    for period in periods:
        values: dict[str, float | None] = {}
        for option, indicator in KEY_INDICATORS:
            row = df[(df["选项"] == option) & (df["指标"] == indicator)]
            if row.empty:
                continue
            values[indicator] = _num(row.iloc[0][period], digits=4)
        indicators[str(period)] = values

    return {
        "symbol": code,
        "periods": [str(p) for p in periods],
        "indicators": indicators,
        "unit_note": "金额类单位为元，比率类单位为百分数",
        # 显式声明数据性质：stock_financial_abstract 只返回已披露的定期报告，
        # 下游 Agent 无需再推测某期数据是否已经公布，从数据层消除时序猜测
        "data_nature": "以上报告期均为已披露的定期报告实际数据，非预测值",
        "source": SOURCE,
        "fetched_at": _stamp(),
    }


@cached("profile")
def get_profile(code: str) -> dict:
    """公司基本概况。"""
    row = ak.stock_profile_cninfo(symbol=code).iloc[0]
    return {
        "symbol": code,
        "name": row["公司名称"],
        "short_name": row["A股简称"],
        "industry": row["所属行业"],
        "market": row["所属市场"],
        "listed_date": str(row["上市日期"]),
        "main_business": str(row["主营业务"])[:400],
        "source": SOURCE,
        "fetched_at": _stamp(),
    }


@cached("news")
def get_news(code: str, limit: int = 10) -> dict:
    """个股近期新闻摘要。

    只保留标题、发布时间与来源，正文不返回以控制体积。
    """
    df = ak.stock_news_em(symbol=code).head(limit)
    return {
        "symbol": code,
        "items": [
            {
                "title": r["新闻标题"],
                "published_at": str(r["发布时间"]),
                "media": r["文章来源"],
            }
            for _, r in df.iterrows()
        ],
        "source": SOURCE,
        "fetched_at": _stamp(),
    }


@cached("symbols")
def get_symbol_table(code: str = "all") -> dict:
    """全部 A 股的代码与名称对照表。

    整表一次下载、按天缓存；首参仅作缓存键（固定用默认值），
    避免每个查询关键词都重复全量下载。供按名称反查代码使用。
    """
    df = ak.stock_info_a_code_name()
    return {
        "items": [
            {"code": str(r["code"]), "name": _clean_name(r["name"])}
            for _, r in df.iterrows()
        ],
        "source": SOURCE,
        "fetched_at": _stamp(),
    }


def search_symbols(keyword: str, limit: int = 20) -> list[dict]:
    """按公司名称或代码关键词反查标的，返回候选列表。

    纯本地过滤，不额外取数：名称包含关键词、或代码以关键词开头即命中。
    关键词与表内名称都先归一化，避免空格与全角字符造成匹配失败。
    """
    keyword = _clean_name(keyword.strip())
    if not keyword:
        return []
    return [
        item
        for item in get_symbol_table()["items"]
        if keyword in item["name"] or item["code"].startswith(keyword)
    ][:limit]