# -*- coding: utf-8 -*-
"""市场状态（牛熊）判定。

设计说明：
- A 股指数（如 sh000001）没有走 DataFetcherManager 的日线通道，
  这里使用宽基 ETF（510300 沪深300ETF、159915 创业板ETF）的日线
  作为指数趋势代理，天然复用现有多数据源 fallback。
- 趋势维度（ETF 均线结构）+ 广度维度（涨跌家数/涨跌停/成交额）
  合成一个 regime 评分，映射到仓位上限与是否允许开新仓。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 宽基 ETF 代理：code -> 展示名
REGIME_PROXIES = {
    "510300": "沪深300ETF",
    "159915": "创业板ETF",
}

# 广度阈值（经验值，可在 evaluate_market_regime 输入中覆盖统计口径）
BREADTH_UP_RATIO_BULL = 0.55
BREADTH_UP_RATIO_BEAR = 0.40
BREADTH_LIMIT_UP_ACTIVE = 40
BREADTH_AMOUNT_ACTIVE_YI = 15000.0  # 两市成交额（亿），牛市量能门槛

REGIME_POSITION_CEILING = {
    "strong_bull": 90.0,
    "bull": 70.0,
    "range": 40.0,
    "bear": 10.0,
}


@dataclass
class MarketRegimeResult:
    """市场状态判定结果。"""

    regime: str  # strong_bull / bull / range / bear
    score: float  # 归一化评分 0-100
    position_ceiling_pct: float  # 建议总仓位上限（%）
    allow_new_buys: bool
    details: List[str] = field(default_factory=list)
    proxy_summary: List[Dict[str, Any]] = field(default_factory=list)
    breadth: Optional[Dict[str, Any]] = None

    @property
    def regime_label(self) -> str:
        return {
            "strong_bull": "强势牛市",
            "bull": "牛市",
            "range": "震荡市",
            "bear": "弱势市",
        }.get(self.regime, self.regime)


def _proxy_trend_score(df: pd.DataFrame, name: str, details: List[str]) -> Optional[float]:
    """单个代理 ETF 的趋势得分（0-3），数据不足返回 None。"""
    if df is None or df.empty or len(df) < 30 or "close" not in df.columns:
        return None
    closes = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(closes) < 30:
        return None
    ma20 = closes.rolling(20).mean()
    ma60 = closes.rolling(60, min_periods=30).mean()
    last_close = float(closes.iloc[-1])
    last_ma20 = float(ma20.iloc[-1])
    last_ma60 = float(ma60.iloc[-1])
    ma20_5d_ago = float(ma20.iloc[-6]) if len(ma20.dropna()) >= 6 else last_ma20

    score = 0.0
    if last_close > last_ma20:
        score += 1.0
        details.append(f"{name}: 收盘价站上 MA20")
    else:
        details.append(f"{name}: 收盘价跌破 MA20")
    if last_ma20 > ma20_5d_ago:
        score += 1.0
        details.append(f"{name}: MA20 向上")
    if last_ma20 > last_ma60:
        score += 1.0
        details.append(f"{name}: MA20 > MA60（中期多头）")
    return score


def evaluate_market_regime(
    proxy_frames: Dict[str, pd.DataFrame],
    breadth: Optional[Dict[str, Any]],
) -> MarketRegimeResult:
    """纯函数：由代理 ETF 日线与市场广度统计得出市场状态。

    Args:
        proxy_frames: {ETF代码: 日线 DataFrame（含 close 列，升序）}
        breadth: get_market_stats() 结果，可为 None（缺失时只用趋势维度）
    """
    details: List[str] = []
    proxy_summary: List[Dict[str, Any]] = []

    trend_scores: List[float] = []
    for code, df in proxy_frames.items():
        name = REGIME_PROXIES.get(code, code)
        score = _proxy_trend_score(df, name, details)
        if score is None:
            details.append(f"{name}: 数据不足，趋势维度跳过")
            continue
        trend_scores.append(score)
        last = df.iloc[-1]
        proxy_summary.append({
            "code": code,
            "name": name,
            "close": float(pd.to_numeric(last.get("close"), errors="coerce")),
            "trend_score": score,
        })

    # 趋势维度：0-3 -> 0-60 分
    if trend_scores:
        trend_part = sum(trend_scores) / len(trend_scores) / 3.0 * 60.0
    else:
        trend_part = 30.0  # 无数据时中性
        details.append("趋势维度无有效数据，按中性计")

    # 广度维度：0-40 分
    breadth_part = 20.0  # 中性
    if breadth:
        up = float(breadth.get("up_count") or 0)
        down = float(breadth.get("down_count") or 0)
        limit_up = int(breadth.get("limit_up_count") or 0)
        limit_down = int(breadth.get("limit_down_count") or 0)
        amount = float(breadth.get("total_amount") or 0.0)
        total = up + down
        breadth_part = 0.0
        if total > 0:
            up_ratio = up / total
            if up_ratio >= BREADTH_UP_RATIO_BULL:
                breadth_part += 15.0
                details.append(f"广度: 上涨占比 {up_ratio:.0%}，市场做多氛围强")
            elif up_ratio >= BREADTH_UP_RATIO_BEAR:
                breadth_part += 8.0
                details.append(f"广度: 上涨占比 {up_ratio:.0%}，多空均衡")
            else:
                details.append(f"广度: 上涨占比 {up_ratio:.0%}，赚钱效应弱")
        else:
            breadth_part += 8.0
        if limit_up >= BREADTH_LIMIT_UP_ACTIVE and limit_up > limit_down * 2:
            breadth_part += 15.0
            details.append(f"广度: 涨停 {limit_up} 家 / 跌停 {limit_down} 家，情绪活跃")
        elif limit_up > limit_down:
            breadth_part += 8.0
            details.append(f"广度: 涨停 {limit_up} 家 / 跌停 {limit_down} 家")
        else:
            details.append(f"广度: 涨停 {limit_up} 家 / 跌停 {limit_down} 家，情绪偏弱")
        if amount >= BREADTH_AMOUNT_ACTIVE_YI:
            breadth_part += 10.0
            details.append(f"广度: 两市成交额约 {amount:.0f} 亿，量能充沛")
        elif amount > 0:
            details.append(f"广度: 两市成交额约 {amount:.0f} 亿")
    else:
        details.append("广度统计缺失，按中性计")

    score = max(0.0, min(100.0, trend_part + breadth_part))
    if score >= 75.0:
        regime = "strong_bull"
    elif score >= 55.0:
        regime = "bull"
    elif score >= 35.0:
        regime = "range"
    else:
        regime = "bear"

    return MarketRegimeResult(
        regime=regime,
        score=round(score, 1),
        position_ceiling_pct=REGIME_POSITION_CEILING[regime],
        allow_new_buys=regime != "bear",
        details=details,
        proxy_summary=proxy_summary,
        breadth=breadth,
    )


def assess_market_regime(data_manager: Any) -> MarketRegimeResult:
    """拉取数据并判定市场状态。单一数据源失败不阻断整体。"""
    proxy_frames: Dict[str, pd.DataFrame] = {}
    for code in REGIME_PROXIES:
        try:
            df, source = data_manager.get_daily_data(code, days=90)
            if df is not None and not df.empty:
                proxy_frames[code] = df
                logger.info("[BullSwing] regime proxy %s 数据获取成功 source=%s rows=%d", code, source, len(df))
        except Exception as exc:
            logger.warning("[BullSwing] regime proxy %s 数据获取失败: %s", code, exc)

    breadth: Optional[Dict[str, Any]] = None
    try:
        breadth = data_manager.get_market_stats(purpose="bull_swing_regime")
    except TypeError:
        # 兼容不带 purpose 参数的旧签名
        try:
            breadth = data_manager.get_market_stats()
        except Exception as exc:
            logger.warning("[BullSwing] 市场广度统计获取失败: %s", exc)
    except Exception as exc:
        logger.warning("[BullSwing] 市场广度统计获取失败: %s", exc)

    result = evaluate_market_regime(proxy_frames, breadth)
    logger.info(
        "[BullSwing] 市场状态判定: regime=%s score=%.1f position_ceiling=%.0f%% allow_new_buys=%s",
        result.regime, result.score, result.position_ceiling_pct, result.allow_new_buys,
    )
    return result
