"""每轮研究结果的落盘存档，供界面回看历史会话。

与 store.py 的分工：
    store.py   只存摘要，供下一轮规划参考（会被读进提示词，必须小）
    reports.py 存完整黑板，供人回看（只写进文件系统，不参与流程）

存的是整块黑板 JSON 而不是只存报告正文：回看时能一并展开协同细节与流转轨迹，
不必为存档再维护一套有损格式。只有正常跑完的轮次才存档（见 engine.run）。

一个用户一个目录：memory/data/reports/<user_id>/<时间>_<标的>.json
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from state import ResearchState

# 存档根目录
ARCHIVE_DIR = Path(__file__).resolve().parent / "data" / "reports"


def _user_dir(user_id: str) -> Path:
    return ARCHIVE_DIR / user_id


def save(state: ResearchState) -> Path:
    """把本轮黑板存档为 JSON，文件名带时间与标的，便于排序与检索。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = _user_dir(state.request.user_id) / f"{stamp}_{state.request.symbol}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    return path


def list_all(user_id: str) -> list[Path]:
    """按时间倒序列出该用户的全部存档。"""
    directory = _user_dir(user_id)
    if not directory.exists():
        return []
    return sorted(directory.glob("*.json"), reverse=True)


def load(path: Path | str) -> ResearchState:
    """把存档还原成黑板，供界面用与实时结果完全相同的方式渲染。"""
    return ResearchState.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )


def report_markdown(state: ResearchState) -> str:
    """把黑板组装成一份可独立阅读的 Markdown，供界面导出下载。

    只取请求信息、评审状态与报告正文：这是对外交付的部分。
    协同细节与流转轨迹保留在存档 JSON 中，不混进导出稿。
    """
    request = state.request
    if state.last_review is not None and state.last_review.passed:
        status = "已定稿"
    else:
        status = "未经审校认可（best-effort 交付）"
    return (
        f"# {request.symbol} 研究报告\n\n"
        f"- 标的：{request.symbol}\n"
        f"- 用户：{request.user_id}\n"
        f"- 问题：{request.question}\n"
        f"- 评审状态：{status}\n\n"
        f"---\n\n"
        f"{state.report}\n"
    )