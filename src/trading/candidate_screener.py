# -*- coding: utf-8 -*-
"""候选池收集与波段买点筛选。

候选来源（免配置，全部走 DataFetcherManager 的降级链）：
1. 涨停池 / 连板梯队（强势股与题材龙头）
2. 市场人气榜（资金关注度）
3. 用户自选股（config.stock_list 中的 A 股）

筛选风格与仓库既有交易理念一致（AGENTS.md / 策略 YAML）：
- 趋势交易：MA5 > MA10 > MA20 多头排列
- 严进策略：乖离率（收盘价相对 MA5）不超过 5%
- 量价配合：量比温和放大，拒绝极端放量
- 排除 ST、北交所、上市不足 30 个交易日的标的
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from data_provider.base import is_bse_code, is_st_stock, normalize_stock_code

logger = logging.getLogger(__name__)

MAX_BIAS_MA5_PCT = 5.0        # 严进：收盘相对 MA5 乖离率上限（%）
MIN_HISTORY_ROWS = 30         # 至少 30 个交易日数据
MAX_VOLUME_RATIO = 6.0        # 极端放量视为一次性事件，排除
MIN_VOLUME_RATIO = 0.6        # 过度缩量说明关注度衰减
MAX_GAIN_20D_PCT = 45.0       # 20 日累计涨幅过大视为高位，不追
STOP_LOSS_MAX_PCT = 8.0       # 单笔最大止损空间（%）


@dataclass
class Candidate:
    """一个通过筛选的波段候选标的。"""

    code: str
    name: str
    source: str                   # 涨停池 / 人气榜 / 自选股
    close: float
    ma5: float
    ma10: float
    ma20: float
    volume_ratio: float
    bias_ma5_pct: float
    gain_20d_pct: float
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    # 交易计划要素
    entry_low: float = 0.0
    entry_high: float = 0.0
    stop_loss: float = 0.0
    target: float = 0.0
    # 深度复核结果（可选，由 deep_review 填充）
    deep_review: Optional[Dict[str, Any]] = None


def _is_cn_stock_code(code: str) -> bool:
    """仅保留沪深 A 股主板/创业板/科创板股票代码。"""
    if not code or not code.isdigit() or len(code) != 6:
        return False
    if is_bse_code(code):
        return False
    return code.startswith(("600", "601", "603", "605", "000", "001", "002", "003", "300", "301", "688"))


def gather_candidate_codes(
    data_manager: Any,
    watchlist: Optional[Sequence[str]] = None,
    pool_size: int = 40,
) -> List[Tuple[str, str, str]]:
    """收集去重后的候选 (code, name, source) 列表，按来源优先级排序。"""
    ordered: List[Tuple[str, str, str]] = []
    seen: set = set()

    def _add(code: Any, name: Any, source: str) -> None:
        norm = normalize_stock_code(str(code or "").strip())
        display_name = str(name or "").strip()
        if not _is_cn_stock_code(norm) or norm in seen:
            return
        if display_name and is_st_stock(display_name):
            return
        seen.add(norm)
        ordered.append((norm, display_name, source))

    try:
        for item in (data_manager.get_limit_up_pool(n=pool_size) or []):
            _add(item.get("code"), item.get("name"), "涨停池")
    except Exception as exc:
        logger.warning("[BullSwing] 获取涨停池失败: %s", exc)

    try:
        for item in (data_manager.get_hot_stocks(n=pool_size) or []):
            _add(item.get("code"), item.get("name"), "人气榜")
    except Exception as exc:
        logger.warning("[BullSwing] 获取人气榜失败: %s", exc)

    for code in (watchlist or []):
        _add(code, "", "自选股")

    logger.info("[BullSwing] 候选池收集完成: %d 只", len(ordered))
    return ordered[: pool_size * 2]


def score_candidate_frame(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """纯函数：对单只股票日线打分。不满足硬性条件返回 None。

    Returns:
        dict: close/ma5/ma10/ma20/volume_ratio/bias_ma5_pct/gain_20d_pct/score/reasons
    """
    if df is None or df.empty or len(df) < MIN_HISTORY_ROWS:
        return None
    required = {"close", "high", "low", "ma5", "ma10", "ma20", "volume_ratio"}
    if not required.issubset(df.columns):
        return None

    last = df.iloc[-1]
    close = float(last["close"])
    ma5 = float(last["ma5"])
    ma10 = float(last["ma10"])
    ma20 = float(last["ma20"])
    volume_ratio = float(last["volume_ratio"])
    if close <= 0 or ma5 <= 0 or ma10 <= 0 or ma20 <= 0:
        return None

    reasons: List[str] = []

    # 硬性条件 1：多头排列
    if not (ma5 > ma10 > ma20):
        return None
    reasons.append("MA5>MA10>MA20 多头排列")

    # 硬性条件 2：价格未破 5 日线且乖离可控（严进）
    bias_ma5_pct = (close - ma5) / ma5 * 100.0
    if close < ma5 * 0.97:
        return None
    if bias_ma5_pct > MAX_BIAS_MA5_PCT:
        return None
    reasons.append(f"MA5 乖离率 {bias_ma5_pct:.1f}%（≤{MAX_BIAS_MA5_PCT:.0f}%）")

    # 硬性条件 3：量能健康
    if not (MIN_VOLUME_RATIO <= volume_ratio <= MAX_VOLUME_RATIO):
        return None

    # 硬性条件 4：非高位（20 日累计涨幅约束）
    close_20d_ago = float(df["close"].iloc[-21]) if len(df) >= 21 else float(df["close"].iloc[0])
    gain_20d_pct = (close / close_20d_ago - 1.0) * 100.0 if close_20d_ago > 0 else 0.0
    if gain_20d_pct > MAX_GAIN_20D_PCT:
        return None

    # 评分（0-100）
    score = 50.0

    # MA20 斜率：中期趋势强度（近 5 日）
    ma20_series = pd.to_numeric(df["ma20"], errors="coerce").dropna()
    if len(ma20_series) >= 6:
        ma20_slope_pct = (float(ma20_series.iloc[-1]) / float(ma20_series.iloc[-6]) - 1.0) * 100.0
        if ma20_slope_pct > 0:
            score += min(10.0, ma20_slope_pct * 4.0)
            reasons.append("MA20 向上")

    # 量价配合：近 10 日上涨日均量 > 下跌日均量
    recent = df.tail(10)
    pct = recent["close"].pct_change()
    up_vol = recent.loc[pct > 0, "volume"].mean() if "volume" in recent.columns else None
    down_vol = recent.loc[pct < 0, "volume"].mean() if "volume" in recent.columns else None
    if up_vol and down_vol and up_vol > down_vol:
        score += 10.0
        reasons.append("近10日上涨放量、下跌缩量")

    # 近期强势：近 10 日出现过 ≥7% 大阳线
    if "pct_chg" in df.columns:
        recent_pct = pd.to_numeric(df["pct_chg"].tail(10), errors="coerce")
        if (recent_pct >= 7.0).any():
            score += 10.0
            reasons.append("近10日出现涨停/大阳线，资金关注度高")

    # 回踩买点：温和缩量且贴近 MA5（buy-the-dip 品质）
    if volume_ratio < 1.0 and abs(bias_ma5_pct) <= 2.0:
        score += 10.0
        reasons.append("缩量回踩 MA5 附近，符合波段买点")
    elif 1.0 <= volume_ratio <= 2.5:
        score += 5.0
        reasons.append(f"量比 {volume_ratio:.1f}，温和放量")

    # 突破位置：接近 20 日新高
    high_20d = float(df["high"].tail(20).max())
    if high_20d > 0 and close >= high_20d * 0.97:
        score += 5.0
        reasons.append("贴近 20 日新高，突破在即")

    return {
        "close": close,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "volume_ratio": volume_ratio,
        "bias_ma5_pct": round(bias_ma5_pct, 2),
        "gain_20d_pct": round(gain_20d_pct, 2),
        "score": round(min(score, 100.0), 1),
        "reasons": reasons,
    }


def build_trade_plan(metrics: Dict[str, Any]) -> Dict[str, float]:
    """由打分指标推导买入区间 / 止损 / 目标价（纯函数）。"""
    close = metrics["close"]
    ma5 = metrics["ma5"]
    ma10 = metrics["ma10"]

    entry_low = round(min(ma5, close) * 0.995, 2)
    entry_high = round(min(close * 1.01, ma5 * 1.03), 2)
    entry_mid = (entry_low + entry_high) / 2.0

    # 止损：MA10 下方一点，且不超过单笔最大止损空间
    stop_by_ma = ma10 * 0.99
    stop_by_cap = entry_mid * (1.0 - STOP_LOSS_MAX_PCT / 100.0)
    stop_loss = round(max(stop_by_ma, stop_by_cap), 2)
    if stop_loss >= entry_low:
        stop_loss = round(entry_low * (1.0 - STOP_LOSS_MAX_PCT / 100.0), 2)

    # 目标：至少 2 倍风险回报比
    risk = max(entry_mid - stop_loss, entry_mid * 0.02)
    target = round(entry_mid + 2.0 * risk, 2)

    return {
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": stop_loss,
        "target": target,
    }


def screen_candidates(
    data_manager: Any,
    codes: Sequence[Tuple[str, str, str]],
    max_candidates: int = 8,
    max_scan: int = 40,
    exclude_codes: Optional[Sequence[str]] = None,
) -> List[Candidate]:
    """对候选池逐只拉取日线并筛选，返回按评分排序的候选列表。"""
    excluded = {normalize_stock_code(c) for c in (exclude_codes or [])}
    results: List[Candidate] = []
    scanned = 0

    for code, name, source in codes:
        if scanned >= max_scan:
            break
        if code in excluded:
            continue
        scanned += 1
        try:
            df, _provider = data_manager.get_daily_data(code, days=60)
        except Exception as exc:
            logger.debug("[BullSwing] 候选 %s 日线获取失败，跳过: %s", code, exc)
            continue
        metrics = score_candidate_frame(df)
        if not metrics:
            continue
        plan = build_trade_plan(metrics)
        results.append(Candidate(
            code=code,
            name=name or code,
            source=source,
            close=metrics["close"],
            ma5=metrics["ma5"],
            ma10=metrics["ma10"],
            ma20=metrics["ma20"],
            volume_ratio=metrics["volume_ratio"],
            bias_ma5_pct=metrics["bias_ma5_pct"],
            gain_20d_pct=metrics["gain_20d_pct"],
            score=metrics["score"],
            reasons=metrics["reasons"],
            entry_low=plan["entry_low"],
            entry_high=plan["entry_high"],
            stop_loss=plan["stop_loss"],
            target=plan["target"],
        ))

    results.sort(key=lambda c: c.score, reverse=True)
    top = results[:max_candidates]
    logger.info(
        "[BullSwing] 筛选完成: 扫描 %d 只，通过 %d 只，输出 Top%d",
        scanned, len(results), len(top),
    )
    return top
