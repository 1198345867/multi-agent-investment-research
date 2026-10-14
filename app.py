"""Streamlit 界面：多 Agent 协同投研的可视化外壳。

界面只做触发与展示，不承担任何业务逻辑：
    填写标的、问题、用户后调用 engine.run 跑完整流程，
    再把黑板上的协同细节逐层铺开呈现，并可只读查看跨轮记忆与历史存档。

长任务处理：
    engine.run 是同步阻塞调用，单次耗时较长。若在主脚本里一路等它跑完，
    整个会话会被占住：运行期间按钮点了没反应、页面一刷新就丢结果。
    因此把流程放到后台线程跑，主脚本只做有节奏的轮询：
    每 POLL_SECONDS 秒渲染一次轨迹就 st.rerun() 让出一次，
    线程结束后收结果并整页刷新切到结果视图。
    这样运行态始终由主脚本体渲染，状态直观；两次刷新之间控件照常响应。

中止：
    运行期间可点"停止"，置位 job["cancel"]；引擎在每个环节开始前检查一次，
    命中即抛 RunCancelled 结束本轮，且不写记忆、不存档。

会话态（st.session_state）：
    job     运行中的作业 {"state", "thread", "error", "cancelled", "cancel"}，
            非空即代表"运行中"，线程结束后清空
    board   最近一次成功完成的黑板
    error   最近一次失败的原因
    notice  最近一次提示（中止、输入校验等）

启动方式：
    streamlit run app.py
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from engine import RunCancelled, run
from memory import reports, store
from state import ResearchRequest, ResearchState
from tools import data

# 运行期间轮询黑板轨迹的间隔（秒）
POLL_SECONDS = 2

st.set_page_config(page_title="多 Agent 协同投研", layout="wide")


def _bullets(items: list[str], empty: str = "无") -> None:
    """把字符串列表渲染成无序列表，空列表给出占位提示。"""
    if not items:
        st.caption(empty)
        return
    for item in items:
        st.markdown(f"- {item}")


def _render_report(board: ResearchState, key_prefix: str) -> None:
    """报告正文、评审状态与导出按钮。实时结果与历史存档共用。

    key_prefix 用于区分实时与历史两处下载按钮，避免控件 key 冲突。
    """
    request = board.request
    st.caption(
        f"标的 {request.symbol} ｜ 用户 {request.user_id} ｜ 问题：{request.question}"
    )
    if board.last_review is not None and board.last_review.passed:
        st.success("评审状态：已定稿")
    else:
        st.warning("评审状态：未经审校认可（best-effort 交付）")
    st.download_button(
        "下载报告 (Markdown)",
        data=reports.report_markdown(board),
        file_name=f"{request.symbol}_研究报告.md",
        mime="text/markdown",
        key=f"{key_prefix}_download",
    )
    st.markdown(board.report)


def _render_detail(board: ResearchState) -> None:
    """协同细节：规划、素材、结论演进、质疑、回应、评审全量铺开。"""
    st.subheader("流程概览")
    cols = st.columns(4)
    cols[0].metric("结论版本", len(board.analyses))
    cols[1].metric("质疑条数", len(board.challenges))
    cols[2].metric("回应条数", len(board.rebuttals))
    cols[3].metric("评审轮数", len(board.reviews))

    st.subheader("环节耗时与消耗")
    if not board.metrics:
        st.caption("无")
    else:
        st.dataframe(
            {
                "环节": [m.name for m in board.metrics],
                "耗时(秒)": [m.seconds for m in board.metrics],
                "模型调用": [m.calls for m in board.metrics],
                "token": [m.total_tokens for m in board.metrics],
            }
        )

    st.subheader("规划")
    plan = board.plan
    if plan is None:
        st.caption("无")
    else:
        st.markdown(f"- 公司：{plan.company}")
        st.markdown(f"- 关注点：{'、'.join(plan.focus_points) or '无'}")
        st.markdown(f"- 数据任务：{'、'.join(plan.data_tasks) or '无'}")

    st.subheader("素材")
    if not board.materials:
        st.caption("无")
    for material in board.materials:
        with st.expander(f"【{material.kind}】{material.note}"):
            st.json(material.payload)

    st.subheader("结论演进")
    for analysis in board.analyses:
        label = "初版" if analysis.round == 0 else f"第 {analysis.round} 轮修订"
        with st.expander(label, expanded=analysis.round == 0):
            st.markdown(analysis.conclusion)
            st.markdown(f"**估值观点**：{analysis.valuation_view or '无'}")
            st.markdown("**证据**：")
            _bullets(analysis.evidences)

    st.subheader("风控质疑")
    if not board.challenges:
        st.caption("无")
    for challenge in board.challenges:
        st.markdown(
            f"- 第 {challenge.round} 轮 ｜ 严重度 {challenge.severity} ｜ "
            f"针对「{challenge.target}」：{challenge.reason}"
        )

    st.subheader("分析回应")
    if not board.rebuttals:
        st.caption("无")
    for rebuttal in board.rebuttals:
        st.markdown(
            f"- 第 {rebuttal.round} 轮 ｜ 处置 {rebuttal.verdict} ｜ "
            f"针对「{rebuttal.target}」：{rebuttal.response}"
        )

    st.subheader("评审收敛度")
    if not board.reviews:
        st.caption("无")
    else:
        convergence = pd.DataFrame(
            {
                "阻断项": [len(r.blocking) for r in board.reviews],
                "建议项": [len(r.advisory) for r in board.reviews],
            },
            index=[f"第{r.round}轮" for r in board.reviews],
        )
        st.bar_chart(convergence)

    st.subheader("评审记录")
    if not board.reviews:
        st.caption("无")
    for review in board.reviews:
        verdict = "通过" if review.passed else "未通过"
        with st.expander(f"第 {review.round} 轮评审：{verdict}"):
            st.markdown("**阻断项**：")
            _bullets(review.blocking)
            st.markdown("**建议项**：")
            _bullets(review.advisory)


def _render_trace(board: ResearchState) -> None:
    """流转轨迹原始日志。"""
    st.code("\n".join(board.trace), language="text")


def _archive_label(path: Path) -> str:
    """存档文件名转可读标签：20261011_111405_000002 -> 2026-10-11 11:14:05 ｜ 000002。"""
    date_str, time_str, symbol = path.stem.split("_")
    return (
        f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]} "
        f"{time_str[:2]}:{time_str[2:4]}:{time_str[4:]} ｜ {symbol}"
    )


def _start_job(symbol: str, question: str, user_id: str) -> None:
    """把一轮研究放到后台线程执行后立即返回，不阻塞界面。"""
    final_question = question or f"分析 {symbol} 当前是否具备投资价值"
    state = ResearchState(
        request=ResearchRequest(
            question=final_question, symbol=symbol, user_id=user_id
        )
    )
    job: dict = {
        "state": state,
        "error": None,
        "cancelled": False,
        "cancel": threading.Event(),
    }

    def _worker() -> None:
        try:
            run(state, should_stop=job["cancel"].is_set)
        except RunCancelled:
            job["cancelled"] = True
        except Exception as exc:  # 取数或模型调用失败时把原因原样留给界面
            job["error"] = str(exc)

    job["thread"] = threading.Thread(target=_worker, daemon=True)
    job["thread"].start()

    # 起新一轮前清掉上一轮结果，避免与运行中的轨迹混看
    st.session_state["board"] = None
    st.session_state["error"] = None
    st.session_state["notice"] = None
    st.session_state["job"] = job


def _apply_pick() -> None:
    """按名称查询的候选被选中后，把对应代码回填到「标的代码」输入框。"""
    matches = st.session_state.get("symbol_matches") or []
    labels = {f"{m['code']} {m['name']}": m["code"] for m in matches}
    st.session_state["symbol_value"] = labels[st.session_state["symbol_pick"]]


# ---------- 侧边栏：研究输入 ----------
# 标的代码用 session_state 承载：查询区回填时直接改它，也避免用 value= 触发冲突警告
if "symbol_value" not in st.session_state:
    st.session_state["symbol_value"] = "600519"

with st.sidebar:
    st.header("研究输入")
    with st.expander("不知道代码？按名称查询"):
        # 与下方研究表单同理：关键词与查询按钮必须放进同一个 form 打包提交。
        # 否则文本框的新值与按钮点击分两次事件到达，查询会拿到旧值（空串）。
        with st.form("symbol_search_form"):
            keyword = st.text_input("公司名称或关键词", key="symbol_keyword")
            searched = st.form_submit_button("查询")
        if searched:
            found = data.search_symbols(keyword)
            st.session_state["symbol_matches"] = found
            # 唯一命中直接回填，省去多余一步
            if len(found) == 1:
                st.session_state["symbol_value"] = found[0]["code"]
        matches = st.session_state.get("symbol_matches")
        if matches and len(matches) > 1:
            labels = {f"{m['code']} {m['name']}": m["code"] for m in matches}
            st.selectbox(
                "选择标的", list(labels), key="symbol_pick", on_change=_apply_pick
            )
            st.caption("选中后自动填入下方「标的代码」")
        elif matches:
            st.caption(f"已填入 {matches[0]['code']} {matches[0]['name']}")
        elif matches is not None:
            st.caption("未找到匹配的股票，请检查名称或直接输入 6 位代码。")

    # 用 form 把三个输入与提交按钮打包：提交时一次性取到当前输入值。
    # 否则文本框的新值与按钮点击可能分两次事件到达，流程会按旧值跑。
    with st.form("research_form"):
        symbol = st.text_input("标的代码", key="symbol_value")
        question = st.text_input("研究问题", value="", placeholder="留空则按标的自动生成")
        user_id = st.text_input("用户标识", value="default", help="用于跨轮记忆隔离")
        running = st.session_state.get("job") is not None
        submitted = st.form_submit_button(
            "开始研究", type="primary", disabled=running
        )
    if submitted:
        # 标的代码是整条链路的输入，为空则直接提示并挡住，不让流程跑到采集阶段才报错
        if not symbol.strip():
            st.session_state["notice"] = "请先填写标的代码"
        else:
            _start_job(symbol.strip(), question, user_id)
        # 立即整页刷新：让按钮进入禁用态、运行视图就位，点击有明确反馈
        st.rerun()
    if st.session_state.get("job") is not None:
        if st.button("停止"):
            st.session_state["job"]["cancel"].set()
        if st.session_state["job"]["cancel"].is_set():
            st.caption("已请求中止，将在当前环节结束后停止")
    st.caption("单次流程耗时较长，运行期间轨迹会持续刷新。")


# ---------- 主区域 ----------
job = st.session_state.get("job")
if job is not None:
    if job["thread"].is_alive():
        st.info(f"协同流程运行中，轨迹每 {POLL_SECONDS} 秒刷新。")
        st.code("\n".join(job["state"].trace) or "流程启动中...", language="text")
        # 睡一小段再整页刷新：既持续更新轨迹，又让出脚本线程处理点击等事件
        time.sleep(POLL_SECONDS)
        st.rerun()
    else:
        # 线程已结束：把结果收进会话态，再整页刷新切到结果视图
        if job["cancelled"]:
            st.session_state["notice"] = "上一轮研究已中止，未产出报告也未写入记忆"
        elif job["error"]:
            st.session_state["error"] = job["error"]
        else:
            st.session_state["board"] = job["state"]
        st.session_state["job"] = None
        st.rerun()

if st.session_state.get("notice"):
    st.warning(st.session_state["notice"])
if st.session_state.get("error"):
    st.error(f"上一轮运行失败：{st.session_state['error']}")

board = st.session_state.get("board")

tab_report, tab_detail, tab_trace, tab_memory, tab_history = st.tabs(
    ["研究报告", "协同细节", "流转轨迹", "跨轮记忆", "历史会话"]
)


# ---------- 研究报告 ----------
with tab_report:
    if board is None:
        st.info("请在左侧填写标的与问题，然后点击“开始研究”。")
    else:
        _render_report(board, "live")


# ---------- 协同细节 ----------
with tab_detail:
    if board is None:
        st.info("尚未运行研究流程。")
    else:
        _render_detail(board)


# ---------- 流转轨迹 ----------
with tab_trace:
    if board is None:
        st.info("尚未运行研究流程。")
    else:
        _render_trace(board)


# ---------- 跨轮记忆（只读） ----------
with tab_memory:
    st.caption(f"用户「{user_id}」的持久记忆，仅展示，不参与界面逻辑。")
    raw = store.snapshot(user_id)

    st.subheader("会话脉络")
    if not raw["sessions"]:
        st.caption("暂无记录")
    for session in raw["sessions"]:
        st.markdown(
            f"- {session['time']} ｜ [{session['symbol']}] {session['question']}\n"
            f"  结论：{session['conclusion']}"
        )

    st.subheader("标的档案")
    if not raw["symbols"]:
        st.caption("暂无记录")
    for code, archive in raw["symbols"].items():
        st.markdown(
            f"- {code} ｜ 更新于 {archive['updated_at']} ｜ "
            f"上次问题：{archive['last_question']}"
        )
        st.caption(f"上次结论：{archive['last_conclusion']}")


# ---------- 历史会话 ----------
with tab_history:
    archives = reports.list_all(user_id)
    if not archives:
        st.info("暂无历史存档。每轮研究正常结束后会自动存档。")
    else:
        labels = {_archive_label(path): path for path in archives}
        chosen = st.selectbox("选择一次历史研究", list(labels))
        past = reports.load(labels[chosen])
        _render_report(past, "history")
        st.divider()
        _render_detail(past)
        st.divider()
        _render_trace(past)