# A 股牛市短线波段交易系统（Bull Swing Trading）

一套面向 **牛市环境、2-10 天持仓周期** 的 A 股短线波段交易系统：每个交易日盘前运行一次，产出当日交易计划报告，并用模拟盘账本跟踪策略绩效。**仅为模拟盘研究用途，不构成投资建议。**

## 快速开始

```bash
# 交易日盘前（建议 08:30 前后）运行
python main.py --premarket-plan

# 非交易日调试 / 跳过交易日检查
python main.py --premarket-plan --force-run

# 只生成报告，不推送通知
python main.py --premarket-plan --no-notify
```

零配置即可运行：数据走现有 `data_provider/` 多源降级链，报告保存到 `reports/bull_swing_plan_YYYYMMDD.md`，并按已配置的通知渠道（`route_type=report`）推送。

如需每日自动执行，可用系统 crontab / GitHub Actions 定时调用上述命令（工作日早晨触发即可，非交易日会自动跳过）。

## 每天做什么

每次运行按顺序完成 5 步：

1. **结算上一交易日计划单**：用实际 K 线模拟撮合（详见"撮合模型"），检查持仓止损是否触发，成交记入模拟盘账本。
2. **市场状态判定**：宽基 ETF（510300 沪深300ETF、159915 创业板ETF）均线结构 + 市场广度（涨跌家数、涨跌停、两市成交额）合成评分，映射为：

   | 状态 | 评分 | 总仓位上限 | 开新仓 |
   | --- | --- | --- | --- |
   | 强势牛市 | ≥75 | 90% | 是 |
   | 牛市 | 55-74 | 70% | 是 |
   | 震荡市 | 35-54 | 40% | 是 |
   | 弱势市 | <35 | 10% | 否 |

3. **持仓管理**：逐只给出 持有 / 减仓 / 清仓 建议：
   - 收盘跌破止损价或 MA10 → 次日开盘清仓
   - 相对 MA5 乖离率 > 12% → 减仓 1/2 锁定利润
   - 盈利超 10% → 止损上移至 MA10 下沿（移动止损）
4. **候选筛选与买入计划**：候选池 = 涨停池 + 人气榜 + 自选股（`STOCK_LIST`），逐只按波段标准筛选：
   - 硬性条件：MA5>MA10>MA20 多头排列、MA5 乖离率 ≤5%（严进）、量比 0.6~6、20 日涨幅 ≤45%、非 ST / 非北交所
   - 加分项：MA20 向上、上涨放量下跌缩量、近 10 日大阳线、缩量回踩 MA5、贴近 20 日新高
   - 按固定风险法计算仓位：单笔风险 = 权益 × `BULL_SWING_RISK_PER_TRADE_PCT`（默认 2%），股数按一手取整
5. **报告与推送**：生成盘前计划 Markdown（市场状态 / 昨日结算 / 持仓管理 / 今日买入计划 / 模拟盘绩效），保存并推送。

## 撮合模型（模拟盘）

T 日盘前挂计划单，T+1 日盘前用 T 日实际 K 线结算：

- **买入计划单**（当日有效）：开盘价落在买入区间内按开盘价成交；高开后盘中回落到区间上限按上限价成交；全天未触及区间不成交；大幅低开（>3%）视为破位放弃。
- **开盘卖出单**：按计划日开盘价成交。
- **止损监控**：持仓最近一根完整 K 线最低价触及止损价，按 min(开盘价, 止损价) 成交。
- **费用**：佣金 0.025%（最低 5 元，双边）+ 卖出印花税 0.05%。

模拟盘账户复用组合账本（`PortfolioService`），在 Web 组合页面可直接查看持仓与盈亏；计划单与净值历史保存在 `data/bull_swing_state.json`。

## TradingAgents-CN 深度复核（可选）

配置后，候选股会先经 [TradingAgents-CN](https://github.com/hsliuping/TradingAgents-CN) 多智能体（市场/基本面/新闻/多空辩论）复核，复核意见并入报告；复核结论为"卖出"的候选会被剔除出买入计划：

```env
BULL_SWING_DEEP_REVIEW_CMD=python /path/to/TradingAgents-CN/scripts/candidate_deep_review.py
```

契约：命令会被追加 `--input <候选JSON> --output <决策JSON>` 参数执行；输出格式为 `{"decisions": [{"symbol", "action", "confidence", "risk_score", "target_price", "reasoning"}]}`。命令缺失、失败或超时（`BULL_SWING_DEEP_REVIEW_TIMEOUT`，默认 1800 秒）只会跳过复核，不阻断计划生成。TradingAgents-CN 侧的运行环境与 LLM 配置见其仓库 `docs/`。

## 配置项

全部有默认值，见 `.env.example`：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BULL_SWING_ACCOUNT_NAME` | 牛市短线模拟盘 | 模拟盘账户名（组合账本中的账户） |
| `BULL_SWING_INITIAL_CAPITAL` | 1000000 | 模拟盘初始资金（元） |
| `BULL_SWING_MAX_POSITIONS` | 5 | 最大同时持仓数 |
| `BULL_SWING_RISK_PER_TRADE_PCT` | 2.0 | 单笔风险占权益比例（%） |
| `BULL_SWING_MAX_CANDIDATES` | 6 | 每日买入计划最多候选数 |
| `BULL_SWING_SCREEN_POOL_SIZE` | 40 | 候选池扫描规模 |
| `BULL_SWING_DEEP_REVIEW_CMD` | 空 | TradingAgents-CN 复核命令，留空跳过 |
| `BULL_SWING_DEEP_REVIEW_TIMEOUT` | 1800 | 复核超时（秒） |

## 代码结构

| 模块 | 职责 |
| --- | --- |
| `src/trading/market_regime.py` | 市场状态判定（ETF 趋势 + 广度） |
| `src/trading/candidate_screener.py` | 候选池收集与打分筛选 |
| `src/trading/signal_engine.py` | 仓位计算、持仓管理信号、撮合模拟（纯函数） |
| `src/trading/paper_broker.py` | 模拟盘账本与计划单撮合 |
| `src/trading/deep_review.py` | TradingAgents-CN 复核适配 |
| `src/trading/premarket_plan.py` | 全流程编排与报告渲染 |

离线单元测试：`python -m pytest tests/test_bull_swing_trading.py -q`

## 边界与已知限制

- 系统基于日线数据做盘前计划，不做盘中实时交易；涨停无法买入/跌停无法卖出的极端情形按可成交简化处理。
- 止损监控只检查最近一根完整 K 线：若某日未运行系统，中间跳过的 K 线不会补触发（模拟盘口径，实盘请自行盯止损）。
- 市场状态用宽基 ETF 代理指数趋势，与真实指数存在跟踪误差。
- 弱势市（bear）只做防守：不开新仓，仅执行持仓管理与止损。
