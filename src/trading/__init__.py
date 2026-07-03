# -*- coding: utf-8 -*-
"""牛市短线波段交易系统（Bull Swing Trading）。

模块职责：
- market_regime: 基于宽基 ETF 趋势 + 市场广度的市场状态判定
- candidate_screener: 候选池收集与波段买点筛选
- signal_engine: 仓位计算与持仓管理信号
- paper_broker: 模拟盘账本（复用 PortfolioService）与计划单撮合
- deep_review: TradingAgents-CN 深度复核适配（可选外部命令）
- premarket_plan: 盘前交易计划编排与报告生成

入口：``python main.py --premarket-plan``，详见 docs/bull-swing-trading.md。
"""

from src.trading.premarket_plan import run_premarket_plan

__all__ = ["run_premarket_plan"]
