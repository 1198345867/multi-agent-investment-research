"""跨轮持久记忆：把每轮研究的结论沉淀下来，供下一轮规划参考。

三层结构，与 state.MemoryContext 一一对应：
    session_summary  近期会话脉络：最近若干次研究的标的、提问与结论摘要
    user_profile     用户画像：常关注的标的与研究次数
    symbol_archive   标的档案：该标的上一次的研究结论与时间

读写时机：
    读取只在流程入口发生一次（engine.run 开头），注入 planner；
    回写在流程结束后进行，只存摘要不存全文，避免记忆库无限膨胀。

持久化用 JSON 文件，一个用户一个文件，内容可直接人工查看与修改。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from state import MemoryContext, ResearchState

# 记忆库目录：memory/data/<user_id>.json
DATA_DIR = Path(__file__).resolve().parent / "data"

# 会话脉络保留的最近条数，更早的会话不再注入
RECENT_SESSIONS = 5

# 单条结论摘要的截断长度
CONCLUSION_LIMIT = 120


def _file(user_id: str) -> Path:
    return DATA_DIR / f"{user_id}.json"


def _read(user_id: str) -> dict:
    """读取用户的记忆文件；首次使用时返回空结构。"""
    path = _file(user_id)
    if not path.exists():
        return {"sessions": [], "symbols": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot(user_id: str) -> dict:
    """只读返回该用户的原始记忆内容，供界面展示，不参与流程逻辑。"""
    return _read(user_id)


def load(user_id: str, symbol: str) -> MemoryContext:
    """构建本轮的记忆上下文，供 engine 注入 planner。"""
    raw = _read(user_id)

    sessions = raw["sessions"][-RECENT_SESSIONS:]
    session_summary = "\n".join(
        f"{s['time']} [{s['symbol']}] {s['question']} → 结论：{s['conclusion']}"
        for s in sessions
    )

    # 用户画像：按出现频次统计常关注标的
    counts: dict[str, int] = {}
    for s in raw["sessions"]:
        counts[s["symbol"]] = counts.get(s["symbol"], 0) + 1
    user_profile = {
        "常关注标的": sorted(counts, key=lambda k: counts[k], reverse=True),
        "研究次数": len(raw["sessions"]),
    }

    return MemoryContext(
        session_summary=session_summary,
        user_profile=user_profile,
        symbol_archive=raw["symbols"].get(symbol, {}),
    )


def save(state: ResearchState) -> None:
    """把本轮研究回写成一条摘要，更新会话脉络与标的档案。"""
    raw = _read(state.request.user_id)
    conclusion = state.analysis.conclusion[:CONCLUSION_LIMIT]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    raw["sessions"].append(
        {
            "time": now,
            "symbol": state.request.symbol,
            "question": state.request.question,
            "conclusion": conclusion,
        }
    )
    raw["symbols"][state.request.symbol] = {
        "updated_at": now,
        "last_question": state.request.question,
        "last_conclusion": conclusion,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _file(state.request.user_id).write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )