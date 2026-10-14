"""程序入口：解析命令行参数，跑一次完整研究流程并打印过程与报告。

用法：
    python main.py 600519
    python main.py 600519 "分析茅台当前是否具备投资价值"
    python main.py 600519 --user alice
"""

from __future__ import annotations

import argparse

from engine import run
from state import ResearchRequest, ResearchState


def main() -> None:
    parser = argparse.ArgumentParser(description="多 Agent 协同投研报告")
    parser.add_argument("symbol", nargs="?", default="600519", help="标的代码")
    parser.add_argument(
        "question", nargs="?", default=None, help="研究问题，缺省按标的生成"
    )
    parser.add_argument("--user", default="default", help="用户标识，用于跨轮记忆隔离")
    args = parser.parse_args()

    question = args.question or f"分析 {args.symbol} 当前是否具备投资价值"
    state = ResearchState(
        request=ResearchRequest(
            question=question, symbol=args.symbol, user_id=args.user
        )
    )
    board = run(state)

    # 先打印流转轨迹，便于观察协同过程；再打印报告正文
    print("\n".join(board.trace))
    print("\n" + "=" * 60)
    if board.last_review.passed:
        print("评审状态：已定稿")
    else:
        print("评审状态：未经审校认可（best-effort 交付）")
    print("=" * 60 + "\n")
    print(board.report)


if __name__ == "__main__":
    main()