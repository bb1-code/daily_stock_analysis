# -*- coding: utf-8 -*-
"""TradingAgents-CN 深度复核适配器（默认开启，自动降级）。

契约：
- 输入：候选 JSON 文件 {"plan_date": "...", "candidates": [{"symbol", "name", ...}]}
- 命令：复核命令追加 ``--input <path> --output <path>`` 参数执行
  （TradingAgents-CN 仓库的 ``scripts/candidate_deep_review.py`` 实现了该契约）
- 输出：决策 JSON {"decisions": [{"symbol", "action", "confidence",
  "risk_score", "target_price", "reasoning"}]}

命令解析（``BULL_SWING_DEEP_REVIEW_CMD``）：
- 留空（默认）：自动探测 monorepo 布局下的仓内引擎脚本
  ``<repo_root>/scripts/candidate_deep_review.py``（daily_stock_analysis
  作为 TradingAgents-CN 主仓子目录时命中）；探测不到则跳过复核，
  与独立部署时的旧行为一致
- ``off`` / ``none`` / ``disabled`` / ``false`` / ``0``：显式关闭复核
- 其他值：作为外部命令执行

引擎依赖缺失（如未装 langgraph）、LLM 未配置、命令失败或超时都只会
跳过复核意见，不阻断盘前计划主流程。
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

DEEP_REVIEW_DISABLE_VALUES = frozenset({"off", "none", "disabled", "false", "0"})


def default_engine_script_path() -> Path:
    """monorepo 布局下仓内复核引擎脚本的预期位置。

    deep_review.py 位于 <dsa_root>/src/trading/，当 daily_stock_analysis
    作为 TradingAgents-CN 主仓的子目录时，引擎脚本在
    <dsa_root>/../scripts/candidate_deep_review.py。
    """
    dsa_root = Path(__file__).resolve().parents[2]
    return dsa_root.parent / "scripts" / "candidate_deep_review.py"


def resolve_deep_review_command(
    configured: str,
    engine_script: Optional[Path] = None,
) -> Optional[List[str]]:
    """解析深度复核命令。返回 argv 列表；返回 None 表示跳过复核。

    Args:
        configured: BULL_SWING_DEEP_REVIEW_CMD 配置值
        engine_script: 仓内引擎脚本路径（默认自动探测，测试可注入）
    """
    text = (configured or "").strip()
    if text.lower() in DEEP_REVIEW_DISABLE_VALUES:
        logger.info("[BullSwing] 深度复核已显式关闭（%s）", text)
        return None
    if text:
        return shlex.split(text)
    script = engine_script if engine_script is not None else default_engine_script_path()
    if script.exists():
        logger.info("[BullSwing] 使用仓内深度复核引擎: %s", script)
        return [sys.executable, str(script)]
    logger.info("[BullSwing] 未找到仓内深度复核引擎（独立部署），跳过复核")
    return None


def run_deep_review(
    candidates: Sequence[Any],
    plan_date: date,
    command: Optional[Sequence[str]],
    work_dir: Path,
    timeout_seconds: int = 1800,
) -> Dict[str, Dict[str, Any]]:
    """调用多智能体命令复核候选股，返回 {symbol: decision}。

    Args:
        command: resolve_deep_review_command() 解析出的 argv；None 表示跳过

    任何失败都只记录 warning 并返回空 dict（复核是增强项，不阻断主流程）。
    """
    if not command or not candidates:
        return {}

    work_dir.mkdir(parents=True, exist_ok=True)
    date_tag = plan_date.strftime("%Y%m%d")
    input_path = work_dir / f"bull_swing_candidates_{date_tag}.json"
    output_path = work_dir / f"bull_swing_deep_review_{date_tag}.json"

    payload = {
        "plan_date": plan_date.isoformat(),
        "candidates": [
            {
                "symbol": c.code,
                "name": c.name,
                "close": c.close,
                "entry_low": c.entry_low,
                "entry_high": c.entry_high,
                "stop_loss": c.stop_loss,
                "target": c.target,
                "score": c.score,
                "reasons": list(c.reasons),
            }
            for c in candidates
        ],
    }
    try:
        with open(input_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("[BullSwing] 深度复核输入文件写入失败: %s", exc)
        return {}

    argv = list(command) + ["--input", str(input_path), "--output", str(output_path)]
    logger.info("[BullSwing] 启动深度复核: %s", " ".join(argv))
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=max(60, timeout_seconds),
        )
    except subprocess.TimeoutExpired:
        logger.warning("[BullSwing] 深度复核超时（%ds），跳过复核意见", timeout_seconds)
        return {}
    except Exception as exc:
        logger.warning("[BullSwing] 深度复核命令执行失败: %s", exc)
        return {}

    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "")[-500:]
        logger.warning(
            "[BullSwing] 深度复核命令退出码 %d，跳过复核意见。stderr: %s",
            completed.returncode, stderr_tail,
        )
        return {}

    return parse_review_output(output_path)


def parse_review_output(output_path: Path) -> Dict[str, Dict[str, Any]]:
    """解析复核输出 JSON（纯函数，便于测试）。"""
    try:
        with open(output_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.warning("[BullSwing] 深度复核输出解析失败: %s", exc)
        return {}

    decisions: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        decisions = data.get("decisions") or []
    elif isinstance(data, list):
        decisions = data

    result: Dict[str, Dict[str, Any]] = {}
    for item in decisions:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip()
        if not symbol:
            continue
        result[symbol] = {
            "action": str(item.get("action") or "").strip() or None,
            "confidence": _safe_float(item.get("confidence")),
            "risk_score": _safe_float(item.get("risk_score")),
            "target_price": _safe_float(item.get("target_price")),
            "reasoning": str(item.get("reasoning") or "").strip() or None,
            "error": str(item.get("error") or "").strip() or None,
        }
    logger.info("[BullSwing] 深度复核完成: %d 条决策", len(result))
    return result


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
