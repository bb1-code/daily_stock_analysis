# -*- coding: utf-8 -*-
"""牛市短线波段交易系统单元测试（离线，unit marker）。"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest

from src.trading.candidate_screener import (
    Candidate,
    build_trade_plan,
    gather_candidate_codes,
    score_candidate_frame,
)
from src.trading.deep_review import parse_review_output
from src.trading.market_regime import evaluate_market_regime
from src.trading.paper_broker import PaperBroker, PendingOrder, new_order_id
from src.trading.premarket_plan import render_plan_report
from src.trading.signal_engine import (
    advise_holding,
    calc_position_size,
    simulate_buy_fill,
    simulate_stop_fill,
)

pytestmark = pytest.mark.unit


def make_daily_frame(closes: List[float], start: Optional[date] = None) -> pd.DataFrame:
    """按 data_provider 标准列构造日线 DataFrame（升序）。"""
    start = start or (date.today() - timedelta(days=len(closes) + 5))
    rows = []
    prev_close = closes[0]
    current = start
    for close in closes:
        while current.weekday() >= 5:
            current += timedelta(days=1)
        rows.append({
            "date": pd.Timestamp(current),
            "open": round(prev_close * 1.001, 2),
            "high": round(max(prev_close, close) * 1.015, 2),
            "low": round(min(prev_close, close) * 0.985, 2),
            "close": close,
            "volume": 1_000_000,
            "amount": close * 1_000_000,
            "pct_chg": round((close / prev_close - 1.0) * 100.0, 2),
        })
        prev_close = close
        current += timedelta(days=1)
    df = pd.DataFrame(rows)
    df["ma5"] = df["close"].rolling(5, min_periods=1).mean().round(2)
    df["ma10"] = df["close"].rolling(10, min_periods=1).mean().round(2)
    df["ma20"] = df["close"].rolling(20, min_periods=1).mean().round(2)
    avg5 = df["volume"].rolling(5, min_periods=1).mean()
    df["volume_ratio"] = (df["volume"] / avg5.shift(1)).fillna(1.0).round(2)
    return df


def uptrend_closes(n: int = 60, start_price: float = 10.0, daily_gain: float = 0.008) -> List[float]:
    closes = []
    price = start_price
    for _ in range(n):
        price *= 1.0 + daily_gain
        closes.append(round(price, 2))
    return closes


def downtrend_closes(n: int = 60, start_price: float = 20.0) -> List[float]:
    closes = []
    price = start_price
    for _ in range(n):
        price *= 0.992
        closes.append(round(price, 2))
    return closes


# ------------------------------------------------------------- market regime


class TestMarketRegime:
    def test_bull_market_detected(self):
        frames = {
            "510300": make_daily_frame(uptrend_closes(70)),
            "159915": make_daily_frame(uptrend_closes(70)),
        }
        breadth = {
            "up_count": 3500, "down_count": 1500, "flat_count": 100,
            "limit_up_count": 80, "limit_down_count": 5, "total_amount": 20000.0,
        }
        result = evaluate_market_regime(frames, breadth)
        assert result.regime in ("bull", "strong_bull")
        assert result.allow_new_buys
        assert result.position_ceiling_pct >= 70.0

    def test_bear_market_blocks_new_buys(self):
        frames = {
            "510300": make_daily_frame(downtrend_closes(70)),
            "159915": make_daily_frame(downtrend_closes(70)),
        }
        breadth = {
            "up_count": 800, "down_count": 4200, "flat_count": 100,
            "limit_up_count": 5, "limit_down_count": 30, "total_amount": 6000.0,
        }
        result = evaluate_market_regime(frames, breadth)
        assert result.regime == "bear"
        assert not result.allow_new_buys
        assert result.position_ceiling_pct <= 10.0

    def test_missing_data_falls_back_to_neutral(self):
        result = evaluate_market_regime({}, None)
        assert result.regime in ("range", "bull")
        assert 0.0 <= result.score <= 100.0


# --------------------------------------------------------------- screener


class TestCandidateScreener:
    def test_uptrend_stock_passes(self):
        df = make_daily_frame(uptrend_closes(60))
        metrics = score_candidate_frame(df)
        assert metrics is not None
        assert metrics["score"] >= 50.0
        assert metrics["ma5"] > metrics["ma10"] > metrics["ma20"]

    def test_downtrend_stock_rejected(self):
        df = make_daily_frame(downtrend_closes(60))
        assert score_candidate_frame(df) is None

    def test_overextended_stock_rejected(self):
        # 尾部两天暴涨拉开乖离
        closes = uptrend_closes(58) + [uptrend_closes(58)[-1] * 1.12, uptrend_closes(58)[-1] * 1.2]
        df = make_daily_frame([round(c, 2) for c in closes])
        assert score_candidate_frame(df) is None

    def test_insufficient_history_rejected(self):
        df = make_daily_frame(uptrend_closes(10))
        assert score_candidate_frame(df) is None

    def test_trade_plan_risk_reward(self):
        df = make_daily_frame(uptrend_closes(60))
        metrics = score_candidate_frame(df)
        plan = build_trade_plan(metrics)
        entry_mid = (plan["entry_low"] + plan["entry_high"]) / 2.0
        assert plan["stop_loss"] < plan["entry_low"]
        assert plan["target"] > plan["entry_high"]
        risk = entry_mid - plan["stop_loss"]
        reward = plan["target"] - entry_mid
        assert reward >= risk * 1.9  # 约 2 倍风险回报比
        # 止损空间不超过 8%
        assert (entry_mid - plan["stop_loss"]) / entry_mid <= 0.085

    def test_gather_candidates_filters_and_dedupes(self):
        class FakeManager:
            def get_limit_up_pool(self, n=20):
                return [
                    {"code": "600111", "name": "北方稀土"},
                    {"code": "300750", "name": "宁德时代"},
                    {"code": "600111", "name": "北方稀土"},   # 重复
                    {"code": "920001", "name": "北交所股"},   # 北交所
                    {"code": "600222", "name": "ST某某"},     # ST
                ]

            def get_hot_stocks(self, n=10):
                return [
                    {"code": "SZ000001", "name": "平安银行"},  # 带前缀
                    {"code": "HK00700", "name": "腾讯控股"},   # 非 A 股
                ]

        codes = gather_candidate_codes(FakeManager(), watchlist=["600519", "AAPL"])
        symbols = [c[0] for c in codes]
        assert symbols == ["600111", "300750", "000001", "600519"]
        assert codes[0][2] == "涨停池"
        assert codes[-1][2] == "自选股"


# ------------------------------------------------------------ signal engine


class TestSignalEngine:
    def test_position_size_lot_and_risk(self):
        sizing = calc_position_size(
            equity=1_000_000, cash=1_000_000,
            entry_price=20.0, stop_loss=18.6,
            risk_per_trade_pct=2.0, position_ceiling_pct=70.0, max_positions=5,
        )
        assert sizing.quantity > 0
        assert sizing.quantity % 100 == 0
        # 风险不超过预算（2% = 20000）
        assert sizing.quantity * (20.0 - 18.6) <= 20_000
        # 单票市值不超过 70% / 5 = 14%
        assert sizing.est_cost <= 1_000_000 * 0.14 + 2000

    def test_position_size_insufficient_cash(self):
        sizing = calc_position_size(
            equity=1_000_000, cash=500.0,
            entry_price=20.0, stop_loss=18.6,
        )
        assert sizing.quantity == 0

    def test_position_size_invalid_stop(self):
        assert calc_position_size(1_000_000, 1_000_000, 10.0, 12.0).quantity == 0

    def test_advise_sell_on_stop_break(self):
        df = make_daily_frame(uptrend_closes(40) + [8.0])  # 尾部闪崩
        advice = advise_holding("600000", df, avg_cost=10.0, stop_loss=9.5)
        assert advice.action == "sell"

    def test_advise_sell_on_ma10_break(self):
        closes = uptrend_closes(40)
        closes.append(round(closes[-1] * 0.9, 2))  # 跌破 MA10 但高于止损
        df = make_daily_frame(closes)
        # 成本与现价接近（无浮盈），避免触发移动止损分支
        advice = advise_holding("600000", df, avg_cost=closes[-1], stop_loss=closes[-1] * 0.5)
        assert advice.action == "sell"
        assert "MA10" in advice.reason

    def test_advise_hold_and_trailing_stop(self):
        df = make_daily_frame(uptrend_closes(60))
        last_close = float(df["close"].iloc[-1])
        ma10 = float(df["ma10"].iloc[-1])
        advice = advise_holding("600000", df, avg_cost=last_close * 0.8, stop_loss=last_close * 0.7)
        assert advice.action in ("hold", "reduce")
        assert advice.stop_updated
        assert advice.stop_loss == pytest.approx(ma10 * 0.99, rel=1e-3)

    def test_advise_missing_data(self):
        advice = advise_holding("600000", None, avg_cost=10.0, stop_loss=9.0)
        assert advice.action == "hold"

    def test_buy_fill_at_open_within_zone(self):
        bar = {"open": 10.0, "low": 9.8}
        assert simulate_buy_fill(bar, entry_low=9.9, entry_high=10.2) == 10.0

    def test_buy_fill_pullback_to_limit(self):
        bar = {"open": 10.6, "low": 10.1}
        assert simulate_buy_fill(bar, entry_low=9.9, entry_high=10.2) == 10.2

    def test_buy_no_fill_when_runaway(self):
        bar = {"open": 10.8, "low": 10.5}
        assert simulate_buy_fill(bar, entry_low=9.9, entry_high=10.2) is None

    def test_buy_abandoned_on_gap_down(self):
        bar = {"open": 9.3, "low": 9.0}
        assert simulate_buy_fill(bar, entry_low=9.9, entry_high=10.2) is None

    def test_stop_fill(self):
        assert simulate_stop_fill({"open": 9.8, "low": 9.2}, stop_loss=9.5) == 9.5
        assert simulate_stop_fill({"open": 9.0, "low": 8.8}, stop_loss=9.5) == 9.0  # 低开直接按开盘
        assert simulate_stop_fill({"open": 10.0, "low": 9.6}, stop_loss=9.5) is None


# ------------------------------------------------------------- paper broker


class FakePortfolioService:
    """内存版组合服务，覆盖 PaperBroker 用到的接口子集。"""

    def __init__(self):
        self.accounts: List[Dict[str, Any]] = []
        self.trades: List[Dict[str, Any]] = []
        self.cash = 0.0
        self._next_id = 1

    def list_accounts(self, include_inactive: bool = False):
        return list(self.accounts)

    def create_account(self, *, name, broker, market, base_currency, owner_id=None):
        account = {"id": self._next_id, "name": name, "broker": broker,
                   "market": market, "base_currency": base_currency}
        self._next_id += 1
        self.accounts.append(account)
        return account

    def record_cash_ledger(self, *, account_id, event_date, direction, amount, currency=None, note=None):
        self.cash += amount if direction == "in" else -amount
        return {"id": 1}

    def record_trade(self, *, account_id, symbol, trade_date, side, quantity, price,
                     fee=0.0, tax=0.0, market=None, currency=None,
                     trade_uid=None, dedup_hash=None, note=None):
        cost = quantity * price + fee + tax
        if side == "buy":
            self.cash -= cost
        else:
            self.cash += quantity * price - fee - tax
        self.trades.append({
            "symbol": symbol, "side": side, "quantity": quantity,
            "price": price, "trade_date": trade_date, "note": note,
        })
        return {"id": len(self.trades)}

    def get_portfolio_snapshot(self, *, account_id=None, as_of=None, cost_method="fifo"):
        positions: Dict[str, Dict[str, Any]] = {}
        for trade in self.trades:
            entry = positions.setdefault(trade["symbol"], {
                "symbol": trade["symbol"], "quantity": 0.0, "avg_cost": 0.0,
                "last_price": trade["price"], "unrealized_pnl_pct": 0.0,
            })
            if trade["side"] == "buy":
                total_qty = entry["quantity"] + trade["quantity"]
                entry["avg_cost"] = (
                    (entry["avg_cost"] * entry["quantity"] + trade["price"] * trade["quantity"]) / total_qty
                )
                entry["quantity"] = total_qty
            else:
                entry["quantity"] -= trade["quantity"]
            entry["last_price"] = trade["price"]
        open_positions = [p for p in positions.values() if p["quantity"] > 0]
        market_value = sum(p["quantity"] * p["last_price"] for p in open_positions)
        return {
            "accounts": [{
                "account_id": account_id or 1,
                "account_name": "test",
                "total_cash": self.cash,
                "total_market_value": market_value,
                "total_equity": self.cash + market_value,
                "positions": open_positions,
            }],
        }


class FakeDataManager:
    def __init__(self, frames: Dict[str, pd.DataFrame]):
        self.frames = frames

    def get_daily_data(self, code, start_date=None, end_date=None, days=30):
        if code not in self.frames:
            raise RuntimeError(f"no data for {code}")
        return self.frames[code], "fake"


class TestPaperBroker:
    def _make_broker(self, tmp_path, service=None):
        return PaperBroker(
            portfolio_service=service or FakePortfolioService(),
            account_name="测试模拟盘",
            state_path=str(tmp_path / "state.json"),
            initial_capital=1_000_000.0,
        )

    def test_account_created_and_seeded(self, tmp_path):
        service = FakePortfolioService()
        broker = self._make_broker(tmp_path, service)
        assert broker.account_id == 1
        assert service.cash == 1_000_000.0
        # 二次初始化复用同一账户
        broker2 = self._make_broker(tmp_path, service)
        assert broker2.account_id == 1
        assert service.cash == 1_000_000.0

    def test_buy_order_fill_and_stop_trigger(self, tmp_path):
        service = FakePortfolioService()
        broker = self._make_broker(tmp_path, service)

        plan_day = date.today() - timedelta(days=2)
        while plan_day.weekday() >= 5:
            plan_day -= timedelta(days=1)

        closes = uptrend_closes(40, start_price=9.0)
        df = make_daily_frame(closes, start=plan_day - timedelta(days=70))
        # 让最后一根 K 线正好落在 plan_day，开盘价在买入区间内
        df = df[pd.to_datetime(df["date"]).dt.date <= plan_day].copy()
        last_idx = df.index[-1]
        df.loc[last_idx, "date"] = pd.Timestamp(plan_day)
        entry_price = float(df.loc[last_idx, "open"])

        broker.place_orders([PendingOrder(
            order_id=new_order_id(),
            symbol="600100",
            name="测试股",
            side="buy",
            plan_date=plan_day.isoformat(),
            quantity=1000,
            entry_low=round(entry_price * 0.99, 2),
            entry_high=round(entry_price * 1.02, 2),
            stop_loss=round(entry_price * 0.93, 2),
            target=round(entry_price * 1.15, 2),
        )], plan_day)
        broker.save_state()

        report = broker.settle(FakeDataManager({"600100": df}), today=date.today())
        assert len(report.filled_buys) == 1
        assert broker.state["stops"]["600100"]["entry_price"] == pytest.approx(entry_price, rel=0.02)
        assert service.trades[-1]["side"] == "buy"

        # 构造次日暴跌触发止损
        crash_close = round(entry_price * 0.85, 2)
        df_crash = make_daily_frame(closes + [crash_close], start=plan_day - timedelta(days=70))
        yesterday = date.today() - timedelta(days=1)
        df_crash.loc[df_crash.index[-1], "date"] = pd.Timestamp(yesterday)
        report2 = broker.settle(FakeDataManager({"600100": df_crash}), today=date.today())
        assert len(report2.stop_triggered) == 1
        assert service.trades[-1]["side"] == "sell"
        assert "600100" not in broker.state["stops"]
        performance = broker.performance_summary()
        assert performance["trades"] == 1
        assert performance["losses"] == 1

    def test_buy_order_expires_without_fill(self, tmp_path):
        broker = self._make_broker(tmp_path)
        plan_day = date.today() - timedelta(days=2)
        while plan_day.weekday() >= 5:
            plan_day -= timedelta(days=1)
        closes = uptrend_closes(40, start_price=9.0)
        df = make_daily_frame(closes, start=plan_day - timedelta(days=70))
        df = df[pd.to_datetime(df["date"]).dt.date <= plan_day].copy()
        df.loc[df.index[-1], "date"] = pd.Timestamp(plan_day)
        open_price = float(df["open"].iloc[-1])

        broker.place_orders([PendingOrder(
            order_id=new_order_id(),
            symbol="600100",
            name="测试股",
            side="buy",
            plan_date=plan_day.isoformat(),
            quantity=1000,
            entry_low=round(open_price * 0.7, 2),   # 远低于市场价，不可能成交
            entry_high=round(open_price * 0.72, 2),
            stop_loss=round(open_price * 0.65, 2),
            target=round(open_price * 0.9, 2),
        )], plan_day)
        report = broker.settle(FakeDataManager({"600100": df}), today=date.today())
        assert len(report.expired_orders) == 1
        assert not broker.state["pending_orders"]

    def test_future_order_kept_pending(self, tmp_path):
        broker = self._make_broker(tmp_path)
        broker.place_orders([PendingOrder(
            order_id=new_order_id(),
            symbol="600100",
            name="测试股",
            side="buy",
            plan_date=date.today().isoformat(),
            quantity=1000,
            entry_low=9.9, entry_high=10.1, stop_loss=9.3, target=11.0,
        )], date.today())
        report = broker.settle(FakeDataManager({}), today=date.today())
        assert not report.filled_buys and not report.expired_orders
        assert len(broker.state["pending_orders"]) == 1

    def test_state_file_roundtrip(self, tmp_path):
        broker = self._make_broker(tmp_path)
        broker.state["closed_trades"].append({"symbol": "600100", "pnl": 100.0})
        broker.save_state()
        with open(tmp_path / "state.json", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["closed_trades"][0]["symbol"] == "600100"


# -------------------------------------------------------------- deep review


class TestDeepReview:
    def test_parse_valid_output(self, tmp_path):
        path = tmp_path / "out.json"
        path.write_text(json.dumps({
            "decisions": [
                {"symbol": "600519", "action": "买入", "confidence": 0.8,
                 "risk_score": 0.3, "target_price": 2000.0, "reasoning": "基本面强"},
                {"symbol": "", "action": "买入"},
                "not-a-dict",
            ],
        }, ensure_ascii=False), encoding="utf-8")
        result = parse_review_output(path)
        assert set(result) == {"600519"}
        assert result["600519"]["action"] == "买入"
        assert result["600519"]["confidence"] == pytest.approx(0.8)

    def test_parse_missing_file(self, tmp_path):
        assert parse_review_output(tmp_path / "missing.json") == {}


# ------------------------------------------------------------------- report


class TestPlanReport:
    def test_render_report_sections(self):
        from src.trading.market_regime import evaluate_market_regime
        from src.trading.paper_broker import SettleReport

        frames = {"510300": make_daily_frame(uptrend_closes(70))}
        regime = evaluate_market_regime(frames, {
            "up_count": 3000, "down_count": 2000, "flat_count": 100,
            "limit_up_count": 50, "limit_down_count": 3, "total_amount": 18000.0,
        })
        candidate = Candidate(
            code="600100", name="测试股", source="涨停池",
            close=10.0, ma5=9.9, ma10=9.7, ma20=9.4,
            volume_ratio=1.5, bias_ma5_pct=1.0, gain_20d_pct=12.0,
            score=75.0, reasons=["MA5>MA10>MA20 多头排列"],
            entry_low=9.85, entry_high=10.05, stop_loss=9.4, target=11.0,
            deep_review={"action": "买入", "confidence": 0.7, "risk_score": 0.4,
                         "target_price": 11.5, "reasoning": "多智能体一致看多", "error": None},
        )
        order = PendingOrder(
            order_id="x", symbol="600100", name="测试股", side="buy",
            plan_date=date.today().isoformat(), quantity=1000,
            entry_low=9.85, entry_high=10.05, stop_loss=9.4, target=11.0,
        )
        report = render_plan_report(
            plan_date=date.today(),
            regime=regime,
            settle_report=SettleReport(),
            positions=[{"symbol": "600200", "quantity": 500, "avg_cost": 8.0,
                        "last_price": 8.8, "unrealized_pnl_pct": 10.0}],
            holding_advices=[],
            stops={"600200": {"name": "持仓股", "stop_loss": 7.8}},
            candidates=[candidate],
            buy_orders=[order],
            performance={"trades": 2, "wins": 1, "losses": 1, "win_rate_pct": 50.0,
                         "total_pnl": 500.0, "profit_loss_ratio": 1.5,
                         "equity": 1_005_000.0, "initial_capital": 1_000_000.0,
                         "return_pct": 0.5, "recent_closed": []},
            equity=1_005_000.0,
            cash=900_000.0,
        )
        assert "市场状态" in report
        assert "持仓管理" in report
        assert "今日买入计划" in report
        assert "600100" in report and "测试股" in report
        assert "模拟盘绩效" in report
        assert "多智能体复核" in report
        assert "不构成任何投资建议" in report
