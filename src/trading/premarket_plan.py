# -*- coding: utf-8 -*-
"""盘前交易计划编排与报告生成。

每个交易日盘前运行一次（``python main.py --premarket-plan``）：
1. 结算上一交易日的计划单（模拟撮合）并检查持仓止损
2. 判定市场状态（牛熊），得出总仓位上限与是否允许开新仓
3. 对持仓逐只给出 持有/减仓/清仓 建议，卖出建议挂为今日开盘卖出单
4. 筛选候选股并（可选）调用 TradingAgents-CN 深度复核，挂今日买入计划单
5. 生成盘前计划 Markdown 报告，保存到 reports/ 并按通知渠道推送
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.trading.candidate_screener import (
    Candidate,
    gather_candidate_codes,
    screen_candidates,
)
from src.trading.deep_review import resolve_deep_review_command, run_deep_review
from src.trading.market_regime import MarketRegimeResult, assess_market_regime
from src.trading.paper_broker import PaperBroker, PendingOrder, SettleReport, new_order_id
from src.trading.signal_engine import (
    LOT_SIZE,
    HoldingAdvice,
    advise_holding,
    calc_position_size,
)

logger = logging.getLogger(__name__)

DEEP_REVIEW_VETO_ACTIONS = {"卖出", "sell"}


@dataclass
class PremarketPlanResult:
    plan_date: date
    report: str
    report_path: Optional[str] = None
    notified: bool = False
    regime: Optional[MarketRegimeResult] = None
    candidates: List[Candidate] = field(default_factory=list)
    holding_advices: List[HoldingAdvice] = field(default_factory=list)
    settle_report: Optional[SettleReport] = None


def _resolve_state_path(config: Any) -> Path:
    database_path = getattr(config, "database_path", "./data/stock_analysis.db")
    return Path(database_path).parent / "bull_swing_state.json"


def run_premarket_plan(
    config: Any,
    send_notification: bool = True,
    plan_date: Optional[date] = None,
) -> PremarketPlanResult:
    """执行盘前计划全流程，返回结果（含报告文本）。"""
    from data_provider import DataFetcherManager
    from src.services.portfolio_service import PortfolioService

    plan_date = plan_date or date.today()
    logger.info("[BullSwing] ====== 盘前计划开始: %s ======", plan_date.isoformat())

    data_manager = DataFetcherManager()
    broker = PaperBroker(
        portfolio_service=PortfolioService(),
        account_name=config.bull_swing_account_name,
        state_path=str(_resolve_state_path(config)),
        initial_capital=config.bull_swing_initial_capital,
    )

    # 1. 结算历史计划单 + 止损检查
    settle_report = broker.settle(data_manager, today=plan_date)

    # 2. 市场状态
    regime = assess_market_regime(data_manager)

    # 3. 持仓管理
    snapshot = broker.snapshot()
    positions = list(snapshot.get("positions") or [])
    equity = float(snapshot.get("total_equity") or 0.0)
    cash = float(snapshot.get("total_cash") or 0.0)

    holding_advices: List[HoldingAdvice] = []
    sell_orders: List[PendingOrder] = []
    for position in positions:
        symbol = str(position.get("symbol") or "")
        quantity = float(position.get("quantity") or 0.0)
        if not symbol or quantity <= 0:
            continue
        stop_info = broker.state.get("stops", {}).get(symbol) or {}
        name = str(stop_info.get("name") or symbol)
        try:
            df, _src = data_manager.get_daily_data(symbol, days=40)
        except Exception as exc:
            logger.warning("[BullSwing] 持仓 %s 行情获取失败: %s", symbol, exc)
            df = None
        advice = advise_holding(
            symbol=symbol,
            df=df,
            avg_cost=float(position.get("avg_cost") or 0.0),
            stop_loss=stop_info.get("stop_loss"),
        )
        if advice.stop_updated and advice.stop_loss:
            broker.update_stop(symbol, advice.stop_loss)
        if advice.action == "sell":
            sell_orders.append(PendingOrder(
                order_id=new_order_id(),
                symbol=symbol,
                name=name,
                side="sell_open",
                plan_date=plan_date.isoformat(),
                quantity=int(quantity),
                reason=advice.reason,
            ))
        elif advice.action == "reduce":
            reduce_qty = int(quantity * advice.sell_ratio // LOT_SIZE) * LOT_SIZE
            if reduce_qty >= LOT_SIZE:
                sell_orders.append(PendingOrder(
                    order_id=new_order_id(),
                    symbol=symbol,
                    name=name,
                    side="sell_open",
                    plan_date=plan_date.isoformat(),
                    quantity=reduce_qty,
                    reason=advice.reason,
                ))
            else:
                advice.action = "hold"
                advice.reason += "（持仓不足两手，暂不减仓）"
        holding_advices.append(advice)

    # 4. 候选筛选与买入计划
    buy_orders: List[PendingOrder] = []
    candidates: List[Candidate] = []
    held_symbols = {str(p.get("symbol")) for p in positions}
    planned_sell_symbols = {o.symbol for o in sell_orders}
    remaining_slots = max(
        0,
        int(config.bull_swing_max_positions)
        - len(held_symbols - planned_sell_symbols),
    )

    if regime.allow_new_buys and remaining_slots > 0:
        codes = gather_candidate_codes(
            data_manager,
            watchlist=getattr(config, "stock_list", None),
            pool_size=int(config.bull_swing_screen_pool_size),
        )
        candidates = screen_candidates(
            data_manager,
            codes,
            max_candidates=int(config.bull_swing_max_candidates),
            max_scan=int(config.bull_swing_screen_pool_size),
            exclude_codes=list(held_symbols),
        )

        # TradingAgents-CN 多智能体深度复核（默认开启，缺引擎/失败自动降级）
        review_command = resolve_deep_review_command(config.bull_swing_deep_review_cmd)
        reviews = run_deep_review(
            candidates,
            plan_date,
            command=review_command,
            work_dir=_resolve_state_path(config).parent,
            timeout_seconds=int(config.bull_swing_deep_review_timeout),
        )
        for candidate in candidates:
            candidate.deep_review = reviews.get(candidate.code)

        available_cash = cash
        for candidate in candidates:
            if len(buy_orders) >= remaining_slots:
                break
            review = candidate.deep_review or {}
            review_action = str(review.get("action") or "").strip().lower()
            if review_action in DEEP_REVIEW_VETO_ACTIONS:
                logger.info("[BullSwing] %s 被深度复核否决（action=%s），跳过买入", candidate.code, review_action)
                continue
            entry_mid = (candidate.entry_low + candidate.entry_high) / 2.0
            sizing = calc_position_size(
                equity=equity,
                cash=available_cash,
                entry_price=entry_mid,
                stop_loss=candidate.stop_loss,
                risk_per_trade_pct=float(config.bull_swing_risk_per_trade_pct),
                position_ceiling_pct=regime.position_ceiling_pct,
                max_positions=int(config.bull_swing_max_positions),
            )
            if sizing.quantity <= 0:
                logger.info("[BullSwing] %s 仓位计算为 0（%s），跳过", candidate.code, sizing.note)
                continue
            available_cash -= sizing.est_cost
            buy_orders.append(PendingOrder(
                order_id=new_order_id(),
                symbol=candidate.code,
                name=candidate.name,
                side="buy",
                plan_date=plan_date.isoformat(),
                quantity=sizing.quantity,
                reason="；".join(candidate.reasons[:3]),
                entry_low=candidate.entry_low,
                entry_high=candidate.entry_high,
                stop_loss=candidate.stop_loss,
                target=candidate.target,
            ))
    elif not regime.allow_new_buys:
        logger.info("[BullSwing] 市场状态为 %s，今日不开新仓", regime.regime)

    # 5. 登记计划单并持久化
    broker.place_orders(sell_orders + buy_orders, plan_date)
    broker.save_state()

    # 6. 报告与通知
    performance = broker.performance_summary()
    report = render_plan_report(
        plan_date=plan_date,
        regime=regime,
        settle_report=settle_report,
        positions=positions,
        holding_advices=holding_advices,
        stops=broker.state.get("stops", {}),
        candidates=candidates,
        buy_orders=buy_orders,
        performance=performance,
        equity=equity,
        cash=cash,
    )

    result = PremarketPlanResult(
        plan_date=plan_date,
        report=report,
        regime=regime,
        candidates=candidates,
        holding_advices=holding_advices,
        settle_report=settle_report,
    )

    try:
        from src.notification import NotificationService

        notifier = NotificationService()
        result.report_path = notifier.save_report_to_file(
            report,
            filename=f"bull_swing_plan_{plan_date.strftime('%Y%m%d')}.md",
        )
        if send_notification:
            result.notified = notifier.send(report, route_type="report")
    except Exception as exc:
        logger.error("[BullSwing] 报告保存/推送失败（不影响计划生成）: %s", exc)

    logger.info(
        "[BullSwing] ====== 盘前计划完成: 持仓 %d 卖出计划 %d 买入计划 %d ======",
        len(positions), len(sell_orders), len(buy_orders),
    )
    return result


# ---------------------------------------------------------------------- report


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _action_label(action: str) -> str:
    return {
        "hold": "持有",
        "sell": "清仓",
        "reduce": "减仓",
    }.get(action, action)


def render_plan_report(
    *,
    plan_date: date,
    regime: MarketRegimeResult,
    settle_report: SettleReport,
    positions: List[Dict[str, Any]],
    holding_advices: List[HoldingAdvice],
    stops: Dict[str, Any],
    candidates: List[Candidate],
    buy_orders: List[PendingOrder],
    performance: Dict[str, Any],
    equity: float,
    cash: float,
) -> str:
    """渲染盘前计划 Markdown 报告（纯函数）。"""
    lines: List[str] = []
    lines.append(f"# 📈 牛市短线波段盘前计划 · {plan_date.isoformat()}")
    lines.append("")

    # 一、市场状态
    lines.append("## 一、市场状态")
    lines.append("")
    lines.append(
        f"- **状态判定**: {regime.regime_label}（评分 {regime.score}/100）"
    )
    lines.append(f"- **建议总仓位上限**: {regime.position_ceiling_pct:.0f}%")
    lines.append(f"- **是否开新仓**: {'是' if regime.allow_new_buys else '否（防守模式）'}")
    for detail in regime.details[:8]:
        lines.append(f"  - {detail}")
    lines.append("")

    # 二、上一交易日计划结算
    lines.append("## 二、上一交易日计划结算")
    lines.append("")
    has_settle_activity = any([
        settle_report.filled_buys,
        settle_report.filled_sells,
        settle_report.stop_triggered,
        settle_report.expired_orders,
        settle_report.errors,
    ])
    if not has_settle_activity:
        lines.append("无待结算计划单。")
    for item in settle_report.filled_buys:
        lines.append(
            f"- ✅ 买入成交 **{item['name']}({item['symbol']})** "
            f"{item['quantity']}股 @ {_fmt(item['price'])}，止损 {_fmt(item['stop_loss'])}"
        )
    for item in settle_report.filled_sells:
        lines.append(
            f"- ✅ 卖出成交 **{item['name']}({item['symbol']})** "
            f"{item['quantity']}股 @ {_fmt(item['price'])}（{item.get('reason') or '计划卖出'}）"
        )
    for item in settle_report.stop_triggered:
        lines.append(
            f"- ⛔ 止损触发 **{item['name']}({item['symbol']})** "
            f"{item['quantity']}股 @ {_fmt(item['price'])}（止损价 {_fmt(item['stop_loss'])}）"
        )
    for item in settle_report.expired_orders:
        lines.append(
            f"- ⏳ 计划单过期 {item.get('name') or ''}({item.get('symbol')})："
            f"{item.get('expire_reason') or '未成交'}"
        )
    for error in settle_report.errors:
        lines.append(f"- ⚠️ {error}")
    lines.append("")

    # 三、持仓管理
    lines.append("## 三、持仓管理")
    lines.append("")
    if not positions:
        lines.append("当前空仓。")
    else:
        lines.append("| 代码 | 名称 | 数量 | 成本 | 现价 | 盈亏% | 止损价 | 今日操作 | 说明 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        advice_map = {a.symbol: a for a in holding_advices}
        for position in positions:
            symbol = str(position.get("symbol") or "")
            advice = advice_map.get(symbol)
            stop_info = stops.get(symbol) or {}
            name = stop_info.get("name") or symbol
            action = _action_label(advice.action) if advice else "持有"
            note = advice.reason if advice else ""
            if advice and advice.stop_updated:
                note += "（止损已上移）"
            lines.append(
                f"| {symbol} | {name} | {_fmt(position.get('quantity'), 0)} "
                f"| {_fmt(position.get('avg_cost'))} | {_fmt(position.get('last_price'))} "
                f"| {_fmt(position.get('unrealized_pnl_pct'))} "
                f"| {_fmt((advice.stop_loss if advice else None) or stop_info.get('stop_loss'))} "
                f"| **{action}** | {note} |"
            )
    lines.append("")

    # 四、今日买入计划
    lines.append("## 四、今日买入计划")
    lines.append("")
    if not regime.allow_new_buys:
        lines.append("市场处于弱势状态，今日不开新仓，以防守为主。")
    elif not buy_orders:
        lines.append("今日无符合条件的买入计划（候选不足或仓位/资金受限）。")
    else:
        lines.append("| 代码 | 名称 | 来源 | 评分 | 买入区间 | 止损 | 目标 | 计划股数 | 预算(元) |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        candidate_map = {c.code: c for c in candidates}
        for order in buy_orders:
            candidate = candidate_map.get(order.symbol)
            source = candidate.source if candidate else "-"
            score = _fmt(candidate.score, 1) if candidate else "-"
            budget = order.quantity * (order.entry_low + order.entry_high) / 2.0
            lines.append(
                f"| {order.symbol} | {order.name} | {source} | {score} "
                f"| {_fmt(order.entry_low)} ~ {_fmt(order.entry_high)} "
                f"| {_fmt(order.stop_loss)} | {_fmt(order.target)} "
                f"| {order.quantity} | {_fmt(budget, 0)} |"
            )
        lines.append("")
        for order in buy_orders:
            candidate = candidate_map.get(order.symbol)
            if not candidate:
                continue
            lines.append(f"**{order.name}({order.symbol})** 入选理由：")
            for reason in candidate.reasons:
                lines.append(f"- {reason}")
            review = candidate.deep_review or {}
            if review.get("action") or review.get("reasoning"):
                lines.append(
                    f"- 🤖 多智能体复核: {review.get('action') or '-'}"
                    f"（信心 {_fmt(review.get('confidence'))} / 风险 {_fmt(review.get('risk_score'))}）"
                    f" {review.get('reasoning') or ''}"
                )
            lines.append("")

    # 落选但值得观察的候选
    ordered_symbols = {o.symbol for o in buy_orders}
    watch_only = [c for c in candidates if c.code not in ordered_symbols]
    if watch_only:
        lines.append("**观察池**（通过筛选但未排入计划）：")
        for candidate in watch_only:
            review = candidate.deep_review or {}
            review_note = f"，复核: {review.get('action')}" if review.get("action") else ""
            lines.append(
                f"- {candidate.name}({candidate.code}) 评分 {_fmt(candidate.score, 1)}，"
                f"来源 {candidate.source}{review_note}"
            )
        lines.append("")

    # 五、模拟盘绩效
    lines.append("## 五、模拟盘绩效")
    lines.append("")
    lines.append(f"- **总权益**: {_fmt(equity, 0)} 元（现金 {_fmt(cash, 0)} 元）")
    if performance.get("return_pct") is not None:
        lines.append(f"- **累计收益率**: {_fmt(performance['return_pct'])}%")
    lines.append(
        f"- **已平仓交易**: {performance.get('trades', 0)} 笔"
        + (
            f"，胜率 {_fmt(performance.get('win_rate_pct'), 1)}%"
            if performance.get("win_rate_pct") is not None else ""
        )
        + (
            f"，盈亏比 {_fmt(performance.get('profit_loss_ratio'))}"
            if performance.get("profit_loss_ratio") is not None else ""
        )
    )
    recent = performance.get("recent_closed") or []
    if recent:
        lines.append("- 最近平仓：")
        for trade in recent:
            lines.append(
                f"  - {trade.get('name')}({trade.get('symbol')}) "
                f"{trade.get('entry_date')} → {trade.get('exit_date')}，"
                f"盈亏 {_fmt(trade.get('pnl'), 0)} 元（{_fmt(trade.get('pnl_pct'))}%），{trade.get('reason') or ''}"
            )
    lines.append("")

    lines.append("---")
    lines.append(
        "> ⚠️ 本报告由规则引擎自动生成，仅为模拟盘研究用途，不构成任何投资建议。"
        "短线交易风险极高，请独立判断并自负盈亏。"
    )
    return "\n".join(lines)
