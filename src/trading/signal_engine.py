# -*- coding: utf-8 -*-
"""仓位计算与持仓管理信号（纯计算，不做 IO）。

仓位模型：固定风险法（risk-based sizing）
- 单笔风险 = 账户权益 * 单笔风险比例（默认 2%）
- 股数 = 单笔风险 / (买入价 - 止损价)，按 A 股一手 100 股取整
- 单票市值不超过 权益 * 总仓位上限 / 最大持仓数，且不超过可用现金

持仓管理规则（2-10 天波段）：
- 跌破止损价：无条件离场（盘中触价，按止损价成交模拟）
- 收盘跌破 MA10：趋势破位，次日开盘离场
- 相对 MA5 乖离率 > 12%：短线过热，减仓一半锁定利润
- 盈利超过 10% 后：止损上移到 MA10（移动止损）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

LOT_SIZE = 100                 # A 股一手
OVERHEAT_BIAS_MA5_PCT = 12.0   # 短线过热乖离阈值
TRAILING_PROFIT_PCT = 10.0     # 盈利超过该值后启用移动止损


@dataclass
class PositionSizing:
    quantity: int
    est_cost: float
    risk_amount: float
    note: str = ""


def calc_position_size(
    equity: float,
    cash: float,
    entry_price: float,
    stop_loss: float,
    risk_per_trade_pct: float = 2.0,
    position_ceiling_pct: float = 70.0,
    max_positions: int = 5,
) -> PositionSizing:
    """固定风险法计算买入股数（A 股按 100 股整数倍）。"""
    if entry_price <= 0 or stop_loss <= 0 or entry_price <= stop_loss:
        return PositionSizing(0, 0.0, 0.0, "价格或止损无效")
    if equity <= 0 or cash <= 0:
        return PositionSizing(0, 0.0, 0.0, "无可用资金")

    risk_amount = equity * risk_per_trade_pct / 100.0
    per_share_risk = entry_price - stop_loss
    qty_by_risk = risk_amount / per_share_risk

    max_position_value = equity * position_ceiling_pct / 100.0 / max(max_positions, 1)
    qty_by_value = max_position_value / entry_price
    qty_by_cash = cash / entry_price

    quantity = int(min(qty_by_risk, qty_by_value, qty_by_cash) // LOT_SIZE) * LOT_SIZE
    if quantity <= 0:
        return PositionSizing(0, 0.0, risk_amount, "资金不足一手或风险预算过小")
    return PositionSizing(
        quantity=quantity,
        est_cost=round(quantity * entry_price, 2),
        risk_amount=round(quantity * per_share_risk, 2),
    )


@dataclass
class HoldingAdvice:
    """单只持仓的管理建议。"""

    symbol: str
    action: str  # hold / sell / reduce / stop_pending
    reason: str
    close: Optional[float] = None
    stop_loss: Optional[float] = None       # 最新（可能上移后）止损价
    stop_updated: bool = False
    sell_ratio: float = 1.0                 # reduce 时的减仓比例
    unrealized_pnl_pct: Optional[float] = None
    details: List[str] = field(default_factory=list)


def advise_holding(
    symbol: str,
    df: pd.DataFrame,
    avg_cost: float,
    stop_loss: Optional[float],
) -> HoldingAdvice:
    """纯函数：根据最新日线对单只持仓给出持有/减仓/卖出建议。"""
    if df is None or df.empty or "close" not in df.columns:
        return HoldingAdvice(symbol=symbol, action="hold", reason="行情数据缺失，维持持有并人工关注", stop_loss=stop_loss)

    last = df.iloc[-1]
    close = float(last["close"])
    ma5 = float(last.get("ma5") or 0.0)
    ma10 = float(last.get("ma10") or 0.0)
    pnl_pct = (close / avg_cost - 1.0) * 100.0 if avg_cost > 0 else None

    advice = HoldingAdvice(
        symbol=symbol,
        action="hold",
        reason="趋势未破坏，继续持有",
        close=close,
        stop_loss=stop_loss,
        unrealized_pnl_pct=round(pnl_pct, 2) if pnl_pct is not None else None,
    )

    # 移动止损：盈利超过阈值后，把止损上移到 MA10 下沿
    if pnl_pct is not None and pnl_pct >= TRAILING_PROFIT_PCT and ma10 > 0:
        trailed = round(ma10 * 0.99, 2)
        if stop_loss is None or trailed > stop_loss:
            advice.stop_loss = trailed
            advice.stop_updated = True
            advice.details.append(f"盈利 {pnl_pct:.1f}%，止损上移至 MA10 下沿 {trailed}")

    effective_stop = advice.stop_loss

    # 1) 收盘已跌破止损：次日开盘离场
    if effective_stop and close < effective_stop:
        advice.action = "sell"
        advice.reason = f"收盘 {close} 跌破止损 {effective_stop}，次日开盘离场"
        return advice

    # 2) 收盘跌破 MA10：趋势破位
    if ma10 > 0 and close < ma10:
        advice.action = "sell"
        advice.reason = f"收盘跌破 MA10({ma10:.2f})，波段趋势破位，次日开盘离场"
        return advice

    # 3) 短线过热：减仓一半
    if ma5 > 0:
        bias = (close - ma5) / ma5 * 100.0
        if bias > OVERHEAT_BIAS_MA5_PCT:
            advice.action = "reduce"
            advice.sell_ratio = 0.5
            advice.reason = f"MA5 乖离率 {bias:.1f}% 过热，减仓 1/2 锁定利润"
            return advice

    return advice


def simulate_buy_fill(
    day_bar: Dict[str, float],
    entry_low: float,
    entry_high: float,
) -> Optional[float]:
    """纯函数：用计划日实际 K 线模拟限价区间买入。

    规则：
    - 开盘价落在买入区间上限内（含 1% 容忍）：按开盘价成交
    - 开盘高开超出区间但盘中回落到 entry_high：按 entry_high 成交
    - 全天最低价高于 entry_high：不成交（不追高）
    - 低开跌破 entry_low 太深（>3%）：视为破位，放弃买入
    """
    open_price = float(day_bar.get("open") or 0.0)
    low = float(day_bar.get("low") or 0.0)
    if open_price <= 0 or low <= 0:
        return None
    if open_price < entry_low * 0.97:
        return None  # 大幅低开破位，放弃
    if open_price <= entry_high * 1.01:
        return round(open_price, 2)
    if low <= entry_high:
        return round(entry_high, 2)
    return None


def simulate_stop_fill(day_bar: Dict[str, float], stop_loss: float) -> Optional[float]:
    """纯函数：模拟止损触发。盘中触及止损价按 min(开盘, 止损价) 成交。"""
    open_price = float(day_bar.get("open") or 0.0)
    low = float(day_bar.get("low") or 0.0)
    if low <= 0 or stop_loss <= 0:
        return None
    if low <= stop_loss:
        fill = min(open_price, stop_loss) if open_price > 0 else stop_loss
        return round(fill, 2)
    return None
