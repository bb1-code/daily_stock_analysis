# -*- coding: utf-8 -*-
"""模拟盘账本与计划单撮合。

- 成交、持仓、现金、盈亏全部复用 PortfolioService（P0 组合账本），
  模拟盘就是一个名字特殊的组合账户，Web/API 侧天然可见。
- 计划单（盘前挂的限价买入/次日开盘卖出/止损监控）与净值历史
  属于交易系统私有状态，存放在 data 目录下的 JSON 状态文件中。

撮合模型（T 日盘前生成计划，T+1 日盘前用 T 日实际 K 线结算）：
- 买入计划单：当日有效，按 signal_engine.simulate_buy_fill 模拟成交
- 开盘卖出单：按计划日开盘价成交
- 止损监控：每次结算检查持仓最近一根完整 K 线是否触及止损价

费用模拟：佣金 0.025%（最低 5 元，双边），卖出另收 0.05% 印花税。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from src.trading.signal_engine import simulate_buy_fill, simulate_stop_fill

logger = logging.getLogger(__name__)

STATE_VERSION = 1
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_TAX_RATE = 0.0005  # 仅卖出


def _commission(amount: float) -> float:
    return round(max(amount * COMMISSION_RATE, COMMISSION_MIN), 2)


def _stamp_tax(amount: float) -> float:
    return round(amount * STAMP_TAX_RATE, 2)


@dataclass
class PendingOrder:
    """一张盘前计划单。"""

    order_id: str
    symbol: str
    name: str
    side: str                     # buy / sell_open
    plan_date: str                # YYYY-MM-DD，计划执行日
    quantity: int
    reason: str = ""
    # buy 专用
    entry_low: float = 0.0
    entry_high: float = 0.0
    stop_loss: float = 0.0
    target: float = 0.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PendingOrder":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class SettleReport:
    """一次结算的结果汇总（供报告展示）。"""

    filled_buys: List[Dict[str, Any]] = field(default_factory=list)
    filled_sells: List[Dict[str, Any]] = field(default_factory=list)
    expired_orders: List[Dict[str, Any]] = field(default_factory=list)
    stop_triggered: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class PaperBroker:
    """牛市短线模拟盘。"""

    def __init__(
        self,
        portfolio_service: Any,
        account_name: str,
        state_path: str,
        initial_capital: float,
    ) -> None:
        self.service = portfolio_service
        self.account_name = account_name
        self.state_path = Path(state_path)
        self.initial_capital = float(initial_capital)
        self.state = self._load_state()
        self._positions_cache: Optional[List[Dict[str, Any]]] = None
        self.account_id = self._ensure_account()

    # ------------------------------------------------------------------ state

    def _load_state(self) -> Dict[str, Any]:
        if self.state_path.exists():
            try:
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    state = json.load(fh)
                if isinstance(state, dict):
                    state.setdefault("pending_orders", [])
                    state.setdefault("stops", {})
                    state.setdefault("closed_trades", [])
                    state.setdefault("equity_history", [])
                    return state
            except Exception as exc:
                logger.warning("[BullSwing] 状态文件损坏，重新初始化: %s", exc)
        return {
            "version": STATE_VERSION,
            "account_id": None,
            "last_plan_date": None,
            "pending_orders": [],
            "stops": {},
            "closed_trades": [],
            "equity_history": [],
        }

    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写，避免中断留下半个 JSON
        fd, tmp_path = tempfile.mkstemp(dir=str(self.state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.state_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ---------------------------------------------------------------- account

    def _ensure_account(self) -> int:
        cached_id = self.state.get("account_id")
        accounts = self.service.list_accounts()
        for account in accounts:
            if cached_id is not None and account.get("id") == cached_id:
                return int(cached_id)
        for account in accounts:
            if account.get("name") == self.account_name:
                self.state["account_id"] = int(account["id"])
                return int(account["id"])
        created = self.service.create_account(
            name=self.account_name,
            broker="paper",
            market="cn",
            base_currency="CNY",
        )
        account_id = int(created["id"])
        self.service.record_cash_ledger(
            account_id=account_id,
            event_date=date.today(),
            direction="in",
            amount=self.initial_capital,
            note="bull_swing 模拟盘初始资金",
        )
        self.state["account_id"] = account_id
        logger.info(
            "[BullSwing] 创建模拟盘账户: name=%s id=%d 初始资金=%.0f",
            self.account_name, account_id, self.initial_capital,
        )
        return account_id

    def snapshot(self) -> Dict[str, Any]:
        """返回模拟盘账户快照（现金/持仓/权益）。"""
        payload = self.service.get_portfolio_snapshot(account_id=self.account_id)
        accounts = payload.get("accounts") or []
        for account in accounts:
            if account.get("account_id") == self.account_id:
                return account
        # get_portfolio_snapshot(account_id=...) 只会返回该账户；兜底取第一个
        return accounts[0] if accounts else {}

    # ----------------------------------------------------------------- orders

    def place_orders(self, orders: List[PendingOrder], plan_date: date) -> None:
        """登记新的计划单（覆盖式：旧的未成交计划单已在结算时过期）。"""
        self.state["pending_orders"].extend(asdict(order) for order in orders)
        self.state["last_plan_date"] = plan_date.isoformat()

    def pending_orders(self) -> List[PendingOrder]:
        return [PendingOrder.from_dict(item) for item in self.state.get("pending_orders", [])]

    # ----------------------------------------------------------------- settle

    @staticmethod
    def _bar_for_date(df: pd.DataFrame, target: date) -> Optional[Dict[str, float]]:
        if df is None or df.empty or "date" not in df.columns:
            return None
        dates = pd.to_datetime(df["date"]).dt.date
        match = df.loc[dates == target]
        if match.empty:
            return None
        row = match.iloc[-1]
        return {
            "open": float(row.get("open") or 0.0),
            "high": float(row.get("high") or 0.0),
            "low": float(row.get("low") or 0.0),
            "close": float(row.get("close") or 0.0),
        }

    def settle(self, data_manager: Any, today: Optional[date] = None) -> SettleReport:
        """结算过往计划单并检查止损。逐单容错，单只失败不阻断。"""
        today = today or date.today()
        report = SettleReport()
        remaining: List[Dict[str, Any]] = []

        for raw in self.state.get("pending_orders", []):
            order = PendingOrder.from_dict(raw)
            try:
                plan_day = date.fromisoformat(order.plan_date)
            except ValueError:
                report.errors.append(f"{order.symbol}: 计划日期非法，丢弃")
                continue
            if plan_day >= today:
                remaining.append(raw)  # 尚未到结算时点（今日盘前挂的单）
                continue
            try:
                df, _src = data_manager.get_daily_data(order.symbol, days=15)
                bar = self._bar_for_date(df, plan_day)
            except Exception as exc:
                logger.warning("[BullSwing] 结算 %s 行情获取失败: %s", order.symbol, exc)
                bar = None
            if bar is None:
                # 计划日无 K 线（停牌/非交易日/数据缺失），过期处理
                report.expired_orders.append({**raw, "expire_reason": "计划日无有效行情"})
                continue

            if order.side == "buy":
                self._settle_buy(order, bar, plan_day, report)
            elif order.side == "sell_open":
                self._settle_sell_open(order, bar, plan_day, report)
            else:
                report.errors.append(f"{order.symbol}: 未知计划单类型 {order.side}")

        self.state["pending_orders"] = remaining
        self._check_stops(data_manager, today, report)
        self._record_equity_point(today)
        return report

    def _settle_buy(
        self,
        order: PendingOrder,
        bar: Dict[str, float],
        plan_day: date,
        report: SettleReport,
    ) -> None:
        fill = simulate_buy_fill(bar, order.entry_low, order.entry_high)
        if fill is None:
            report.expired_orders.append({**asdict(order), "expire_reason": "未触及买入区间或低开破位"})
            return
        amount = fill * order.quantity
        try:
            self.service.record_trade(
                account_id=self.account_id,
                symbol=order.symbol,
                trade_date=plan_day,
                side="buy",
                quantity=float(order.quantity),
                price=fill,
                fee=_commission(amount),
                trade_uid=f"bull_swing:{order.order_id}",
                note=f"bull_swing 买入 {order.name} 止损{order.stop_loss}",
            )
        except Exception as exc:
            report.errors.append(f"{order.symbol}: 买入记账失败 {exc}")
            return
        self._invalidate_positions_cache()
        self.state["stops"][order.symbol] = {
            "name": order.name,
            "stop_loss": order.stop_loss,
            "target": order.target,
            "entry_price": fill,
            "entry_date": plan_day.isoformat(),
        }
        report.filled_buys.append({
            "symbol": order.symbol,
            "name": order.name,
            "price": fill,
            "quantity": order.quantity,
            "stop_loss": order.stop_loss,
            "target": order.target,
        })

    def _position_quantity(self, symbol: str) -> float:
        # 快照重放 + 实时行情开销大，结算过程内做缓存，成交后失效
        if self._positions_cache is None:
            self._positions_cache = list(self.snapshot().get("positions") or [])
        for position in self._positions_cache:
            if str(position.get("symbol")) == symbol:
                return float(position.get("quantity") or 0.0)
        return 0.0

    def _invalidate_positions_cache(self) -> None:
        self._positions_cache = None

    def _sell(
        self,
        symbol: str,
        name: str,
        quantity: float,
        price: float,
        trade_date: date,
        reason: str,
        uid_suffix: str,
    ) -> bool:
        held = self._position_quantity(symbol)
        quantity = min(quantity, held)
        if quantity <= 0 or price <= 0:
            return False
        amount = price * quantity
        try:
            self.service.record_trade(
                account_id=self.account_id,
                symbol=symbol,
                trade_date=trade_date,
                side="sell",
                quantity=float(quantity),
                price=price,
                fee=_commission(amount),
                tax=_stamp_tax(amount),
                trade_uid=f"bull_swing:{uid_suffix}",
                note=f"bull_swing 卖出 {name} {reason}"[:250],
            )
        except Exception as exc:
            logger.warning("[BullSwing] %s 卖出记账失败: %s", symbol, exc)
            return False
        self._invalidate_positions_cache()
        stop_info = self.state["stops"].get(symbol) or {}
        entry_price = float(stop_info.get("entry_price") or 0.0)
        if entry_price > 0:
            self.state["closed_trades"].append({
                "symbol": symbol,
                "name": name,
                "entry_price": entry_price,
                "exit_price": price,
                "quantity": quantity,
                "pnl": round((price - entry_price) * quantity, 2),
                "pnl_pct": round((price / entry_price - 1.0) * 100.0, 2),
                "entry_date": stop_info.get("entry_date"),
                "exit_date": trade_date.isoformat(),
                "reason": reason,
            })
        if quantity >= held:
            self.state["stops"].pop(symbol, None)
        return True

    def _settle_sell_open(
        self,
        order: PendingOrder,
        bar: Dict[str, float],
        plan_day: date,
        report: SettleReport,
    ) -> None:
        open_price = round(float(bar.get("open") or 0.0), 2)
        if open_price <= 0:
            report.expired_orders.append({**asdict(order), "expire_reason": "开盘价缺失"})
            return
        if self._sell(
            order.symbol, order.name, float(order.quantity), open_price,
            plan_day, order.reason or "计划卖出", order.order_id,
        ):
            report.filled_sells.append({
                "symbol": order.symbol,
                "name": order.name,
                "price": open_price,
                "quantity": order.quantity,
                "reason": order.reason,
            })

    def _check_stops(self, data_manager: Any, today: date, report: SettleReport) -> None:
        """检查持仓最近一根完整 K 线是否触发止损（盘前视角=昨日 K 线）。"""
        for symbol, info in list(self.state.get("stops", {}).items()):
            stop_loss = float(info.get("stop_loss") or 0.0)
            if stop_loss <= 0:
                continue
            if self._position_quantity(symbol) <= 0:
                self.state["stops"].pop(symbol, None)
                continue
            try:
                df, _src = data_manager.get_daily_data(symbol, days=10)
            except Exception as exc:
                logger.warning("[BullSwing] 止损检查 %s 行情获取失败: %s", symbol, exc)
                continue
            if df is None or df.empty:
                continue
            last = df.iloc[-1]
            bar_date = pd.to_datetime(last["date"]).date() if "date" in df.columns else None
            if bar_date is None or bar_date >= today:
                continue  # 只用已完成的 K 线
            entry_date = info.get("entry_date")
            if entry_date and bar_date.isoformat() <= entry_date:
                continue  # 入场当日不触发止损（入场模拟已含破位放弃逻辑）
            bar = {
                "open": float(last.get("open") or 0.0),
                "low": float(last.get("low") or 0.0),
            }
            fill = simulate_stop_fill(bar, stop_loss)
            if fill is None:
                continue
            quantity = self._position_quantity(symbol)
            if self._sell(
                symbol, str(info.get("name") or symbol), quantity, fill,
                bar_date, f"触发止损 {stop_loss}", f"stop-{uuid.uuid4().hex[:12]}",
            ):
                report.stop_triggered.append({
                    "symbol": symbol,
                    "name": info.get("name") or symbol,
                    "price": fill,
                    "quantity": quantity,
                    "stop_loss": stop_loss,
                })

    def update_stop(self, symbol: str, stop_loss: float) -> None:
        info = self.state["stops"].setdefault(symbol, {})
        info["stop_loss"] = stop_loss

    # ------------------------------------------------------------ performance

    def _record_equity_point(self, today: date) -> None:
        try:
            snapshot = self.snapshot()
            equity = float(snapshot.get("total_equity") or 0.0)
        except Exception as exc:
            logger.warning("[BullSwing] 权益快照获取失败: %s", exc)
            return
        history: List[Dict[str, Any]] = self.state.setdefault("equity_history", [])
        today_iso = today.isoformat()
        history[:] = [item for item in history if item.get("date") != today_iso]
        history.append({"date": today_iso, "equity": round(equity, 2)})
        history[:] = history[-250:]

    def performance_summary(self) -> Dict[str, Any]:
        closed: List[Dict[str, Any]] = self.state.get("closed_trades", [])
        wins = [t for t in closed if float(t.get("pnl") or 0.0) > 0]
        losses = [t for t in closed if float(t.get("pnl") or 0.0) <= 0]
        total_pnl = sum(float(t.get("pnl") or 0.0) for t in closed)
        avg_win = sum(float(t["pnl"]) for t in wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(float(t["pnl"]) for t in losses) / len(losses)) if losses else 0.0
        history = self.state.get("equity_history", [])
        equity = history[-1]["equity"] if history else None
        return {
            "trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(closed) * 100.0, 1) if closed else None,
            "total_pnl": round(total_pnl, 2),
            "profit_loss_ratio": round(avg_win / avg_loss, 2) if avg_loss > 0 else None,
            "equity": equity,
            "initial_capital": self.initial_capital,
            "return_pct": (
                round((equity / self.initial_capital - 1.0) * 100.0, 2)
                if equity and self.initial_capital > 0 else None
            ),
            "recent_closed": closed[-5:],
        }


def new_order_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:10]}"
