# 投资大师选股系统 — CLAUDE.md

## 项目简介

美股全量打分流水线，模拟巴菲特、芒格、段永平、彼得·林奇的选股逻辑。
对全美上市股票进行五维评分 + 四大师加权排名，按季度归档结果。

---

## 目录结构

```
stock_scoring_agent/
├── config.py              # 所有常量/权重/阈值的唯一来源，改参数只动这里
├── run_scoring.py         # 主入口：全量打分流水线（支持断点续跑）
├── fetch_data.py          # 独立数据拉取脚本（纯拉取，不打分）
├── inspect_cache.py       # 数据库查询工具（查缓存/展示财报）
├── run_a_stocks.py        # 专项脚本：对 A 开头的美股打分，输出 Top10
├── src/
│   ├── fetcher.py         # 数据获取 + SQLite 缓存（唯一网络边界）
│   ├── normalizer.py      # 原始数据 → NormalizedFinancials dataclass
│   ├── scorer.py          # FilterFunnel / VetoEngine / DimensionScorer / MasterScorer
│   └── masters/           # 四位大师的权重应用和额外否决逻辑
│       ├── buffett.py
│       ├── munger.py
│       ├── duan.py
│       └── lynch.py
├── data/
│   └── stock_cache.db     # SQLite 缓存（财务数据90天，估值7天，股票列表30天）
├── scores/{quarter}/      # 输出目录，按季度归档
│   ├── {TICKER}.json      # 单股详细打分
│   ├── _summary.csv       # 全量汇总表
│   └── _run_log.txt       # 运行日志
├── dashboard.py           # 可视化看板 Flask 服务（python dashboard.py → http://localhost:5001）
├── templates/
│   └── index.html         # 看板前端（Bootstrap + Chart.js，支持明亮/夜间模式）
├── tests/
│   └── test_fetch.py      # 数据拉取测试（python tests/test_fetch.py AAPL MSFT）
└── .env                   # FMP_API_KEY（不提交到 git）
```

---

## 快速运行

```bash
# ── 数据拉取（fetch_data.py）────────────────────────────
# 单只 / 多只
python fetch_data.py --ticker AAPL
python fetch_data.py --tickers AAPL MSFT NVDA GOOGL

# 从文件（每行一个 ticker）
python fetch_data.py --file watchlist.txt

# 补全已缓存 ticker 的缺失/过期数据
python fetch_data.py --cached --mode core

# 全量拉取（无限套餐）
python fetch_data.py --all --mode full

# 预估调用量，不实际拉取
python fetch_data.py --cached --dry-run

# --mode core  = 打分必需 8 端点（默认）
# --mode full  = 全量 19 端点

# ── 打分（run_scoring.py）────────────────────────────────
# 全量打分（当季，支持断点续跑）
python run_scoring.py

# 只打指定股票（测试/验证）
python run_scoring.py --tickers AAPL MSFT GOOGL

# 打 A 开头全部美股并输出 Top10
python run_a_stocks.py

# ── 查询缓存（inspect_cache.py）─────────────────────────
# 总览
python inspect_cache.py

# 单只股票所有财报一把看
python inspect_cache.py --ticker AAPL --show detail

# 查看某张报表
python inspect_cache.py --ticker AAPL --show income_annual
python inspect_cache.py --ticker AAPL --show ratios_ttm

# 所有已缓存 ticker 列表
python inspect_cache.py --list

# ── 可视化看板（dashboard.py）────────────────────────────
python dashboard.py
# 浏览器自动打开 http://localhost:5001
# 功能：侧边栏股票列表 + 详情页（雷达图/营收/利润率/ROE/EPS 等 6 张图表）
# 支持明亮/夜间模式切换，偏好存入 localStorage
```

---

## 数据源

| 数据源 | 用途 | 备注 |
|--------|------|------|
| FMP Stable API | 财务报表、估值、指标 | 主源，无限套餐（已升级） |
| yfinance | fallback | 若 FMP 失败时兜底 |
| SEC EDGAR | 审计意见（计划中） | V3 功能 |

**API Base URL**: `https://financialmodelingprep.com/stable`（2025-08-31 后切换到 stable，旧 v3 已返回 403）

**关键可用端点**（经验证）：
- `GET /stable/profile?symbol=AAPL` — 公司基本信息 ✓
- `GET /stable/search-symbol?query=AA&limit=100` — 股票搜索 ✓
- `GET /stable/income-statement?symbol=AAPL&period=annual&limit=5` — 损益表 ✓
- `GET /stable/balance-sheet-statement` — 资产负债表 ✓
- `GET /stable/cash-flow-statement` — 现金流量表 ✓
- `GET /stable/key-metrics` — 关键指标历史 ✓
- `GET /stable/ratios` — 财务比率 ✓
- `GET /stable/ratios-ttm` — TTM 估值 ✓
- `GET /stable/key-metrics-ttm` — TTM 关键指标 ✓

**不可用端点**（已确认 404）：
- `/stable/stock-screener` — 不存在，不要使用
- `/stable/available-traded/list` — 不存在

**获取全量股票列表（`get_ticker_universe`）**：
1. 优先尝试 `/stable/stock-list`（单次请求，~15000 条）
2. 若返回空，自动降级到 `/stable/search-symbol` 全字母扫描（AA→ZZ，702次请求，~3分钟）
3. 结果缓存 30 天（cache_key: `universe:all`）

**获取 A 开头股票列表**（`run_a_stocks.py` 专用）：
- cache_key: `a_stocks:list`，TTL 30 天，已完成首次缓存

---

## 核心架构

### 三道过滤漏斗（FilterFunnel）

```
全量股票（~6000）
  ↓ Stage1: 市值>$3亿 + 日均成交>$50万 + 上市>3年 + 交易所[NYSE/NASDAQ/AMEX]
~2500只
  ↓ Stage2: 过去3年至少2年盈利 + 总负债/总资产<85%
~1500只
  ↓ Stage3: 五维打分 + 一票否决
最终排名
```

### 五大评分维度

| 维度 | 默认权重 | 核心指标 |
|------|---------|---------|
| 生意质量 | 30% | ROE、毛利率、FCF/净利润、Capex占比 |
| 财务健康 | 20% | 流动比率、负债/权益、利息覆盖、OCF/负债 |
| 成长性 | 20% | 营收CAGR、净利CAGR、FCF CAGR、EPS CAGR |
| 管理层 | 15% | ROIC、股权稀释率、回购率、FCF转化率 |
| 估值 | 15% | PEG、PE、EV/EBITDA、PB |

### 四大师权重矩阵

| 大师 | 生意质量 | 财务健康 | 成长性 | 管理层 | 估值 |
|------|---------|---------|-------|-------|-----|
| 巴菲特 | 40% | 20% | 10% | 20% | 10% |
| 芒格 | 35% | 20% | 10% | 30% | 5% |
| 段永平 | 35% | 15% | 25% | 25% | 0% |
| 彼得·林奇 | 20% | 15% | 40% | 10% | 15% |

### 一票否决项（VetoEngine）

触发后标红，不参与排名（但仍输出到 CSV）：
- 过去3年 FCF 持续为负
- 净利润率<0 且连续2年
- 长期负债/FCF > 10
- 近1年内部人减持 > 30%
- 审计非标准意见

---

## 数据缓存规则

| 数据类型 | TTL | 备注 |
|---------|-----|------|
| 财务报表（损益/资产/现金流） | 90天 | 季报才更新 |
| 估值数据（TTM PE/PB等） | 7天 | 价格每天变 |
| 股票列表 | 30天 | 月度刷新 |
| 公司 profile | 30天 | 含市值/上市日期 |

缓存存储在 `data/stock_cache.db`（SQLite），key 格式为 `{endpoint}:{ticker}:{period}`。

---

## 输出格式

### 单股 JSON（`scores/2026-Q1/AAPL.json`）

```json
{
  "ticker": "AAPL",
  "quarter": "2026-Q1",
  "composite_score": 7.45,
  "veto_triggered": false,
  "veto_reasons": [],
  "dimensions": {
    "business_quality": 8.8,
    "financial_health": 7.0,
    "growth": 6.0,
    "management": 9.0,
    "valuation": 3.0
  },
  "master_scores": {
    "buffett": 8.1,
    "munger": 8.5,
    "duan": 7.8,
    "lynch": 6.2
  },
  "company_info": { ... },
  "key_metrics": { ... }
}
```

### 汇总 CSV（`_summary.csv` / `_a_stocks_summary.csv`）

每只股票一行，包含所有维度分、大师分、关键财务指标。
`veto_triggered=True` 的股票排在末尾，不参与正式排名。

---

## 可视化看板（dashboard.py）

### API 端点

| 端点 | 说明 |
|------|------|
| `GET /api/quarters` | 返回 `scores/` 下所有季度目录列表 |
| `GET /api/stocks?quarter=2026-Q1` | 返回该季度所有股票摘要（从 JSON 文件读取） |
| `GET /api/stock/{ticker}?quarter=2026-Q1` | 返回单股完整数据，附带从 SQLite 提取的历史财务时序 |
| `GET /api/data?quarter=2026-Q1` | 兼容接口，返回 CSV 平铺数据 |

### 历史财务数据（`financials` 字段）

`/api/stock/{ticker}` 会从 `data/stock_cache.db` 中拼装以下时序（最近5年，倒序）：
- `years` / `revenue` / `net_income` / `gross_margin` / `net_margin`（来自 `income_annual`）
- `free_cash_flow`（来自 `cashflow_annual`）
- `roe` / `roic`（来自 `key_metrics_annual`）
- `eps`（来自 `income_annual.epsDiluted`）

---

## 开发路线图

- **V1（当前）**: 跑通打分流水线 ✓
- **V1.5（当前）**: 可视化看板 ✓（Flask + Chart.js，明亮/夜间模式）
- **V2**: 离线回测（IC值、分组分析、Point-in-Time）
- **V3**: LLM 读 10-K 做定性分析
- **V4**: 权重自动优化

---

## 注意事项

1. **API Key** 在 `.env` 中，不提交 git。`.env.example` 中有示例。
2. **断点续跑**：`run_checkpoints` 表记录已打分的 `(quarter, ticker)`，重启不会重复打分。用 `--fresh` 参数强制重跑。
3. **ETF/基金过滤**：通过 Stage1 市值+流动性过滤，大多数 ETF 会被筛掉。
4. **missing_valuation_ttm**：TTM 估值数据缺失时打分时估值维度得 0 分，JSON 中标记 `data_quality_flags`。
5. **`stable/search-symbol`** 是当前唯一可用的股票搜索端点，已缓存 A 股列表（key: `a_stocks:list`）。
6. **拉取与打分分离**：推荐先用 `fetch_data.py` 把数据拉进缓存，再跑 `run_scoring.py`。打分时若数据已在缓存则不会发出任何网络请求。
7. **cache_key 格式**：`{type}:{TICKER}`，如 `income_annual:AAPL`、`ratios_ttm:AAPL`。每个端点对应固定 key，见 `fetch_data.py` 中的 `_CACHE_KEY_PREFIX`。
