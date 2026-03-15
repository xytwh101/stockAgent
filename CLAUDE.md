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
├── backtest.py            # 打分回测验证：预测排名 vs 实际涨幅排名
├── inspect_cache.py       # 数据库查询工具（查缓存/展示财报）
├── run_a_stocks.py        # 专项脚本：对 A 开头的美股打分，输出 Top10
├── dashboard.py           # 可视化看板 Flask 服务（→ http://localhost:5001）
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
├── scores/backtest/       # 回测输出目录（backtest.py 生成）
│   ├── backtest_{score_quarters}_vs_{eval_quarters}_{score_key}.csv
│   └── backtest_{score_quarters}_vs_{eval_quarters}_{score_key}_report.txt
├── templates/
│   └── index.html         # 看板前端（Bootstrap + Chart.js，单季度/区间模式，明亮/夜间主题）
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

# 历史季度打分（自动启用 PIT 过滤，仅用季度末前已发布的数据）
python run_scoring.py --quarter 2023-Q1 --offline-db
# 输出：scores/2023-Q1/*.json，JSON 中含 "pit_mode": true, "as_of_date": "2023-03-31"

# 历史年度全量打分（逐季度执行，配合回测使用）
python run_scoring.py --quarter 2023-Q1 --offline-db
python run_scoring.py --quarter 2023-Q2 --offline-db
python run_scoring.py --quarter 2023-Q3 --offline-db
python run_scoring.py --quarter 2023-Q4 --offline-db

# 离线模式：只用缓存数据（不发任何 API 请求）
python run_scoring.py --offline-db                   # 所有已缓存 ticker，当前季度
python run_scoring.py --offline --tickers AAPL MSFT  # 指定 ticker，离线

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
# 导航栏两种模式：
#   单季度 — 下拉选单个季度
#   区间   — 选 from~to 季度范围，聚合查看多季度平均打分

# ── 打分回测验证（backtest.py）───────────────────────────
# 列出所有可回测的季度
python backtest.py --list

# 基本用法：指定打分时段 + 评估时段
python backtest.py --score-period 2023-Q1 --eval-period 2024-Q1

# 年度打分 vs 年度评估（自动展开为4个季度）
python backtest.py --score-period 2023 --eval-period 2024

# 多年打分范围（逗号分隔 = 空格分隔）
python backtest.py --score-period 2023,2024 --eval-period 2025-Q2

# 验证某一大师维度
python backtest.py --score-period 2023-Q1 --eval-period 2024-Q1 --score-key buffett

# 对比所有打分维度 IC
python backtest.py --score-period 2023-Q1 --eval-period 2024-Q1 --all-keys

# 包含否决股验证否决规则有效性
python backtest.py --score-period 2023-Q1 --eval-period 2024-Q1 --include-veto

# 多季度聚合方式：等权平均(默认) / 取最新
python backtest.py --score-period 2023 --eval-period 2024 --agg-mode latest

# 不自动打分（仅用已有数据）
python backtest.py --score-period 2023-Q1 --eval-period 2024-Q1 --no-auto-score

# 强制重新打分
python backtest.py --score-period 2023-Q1 --eval-period 2024-Q1 --fresh-score
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
  ↓ Stage1: 退市过滤（三重判断）+ 市值>$3亿 + 日均成交>$50万 + 上市>3年 + 交易所[NYSE/NASDAQ/AMEX]
~2500只
  ↓ Stage2: 过去3年至少2年盈利 + 总负债/总资产<85%
~1500只
  ↓ Stage3: 五维打分 + 一票否决
最终排名
```

### 退市 / 实质停止交易过滤（三重判断）

三项检查在打分和拉取流程中同时生效，profile 已缓存时零额外 API 调用：

| 检查项 | 判断条件 | 生效位置 |
|--------|---------|---------|
| FMP 官方标记 | `isActivelyTrading = False` | Stage1、score_ticker、fetch_ticker |
| 无报价 | `profile.price = 0` | Stage1、score_ticker、fetch_ticker |
| 年报过期 | 最新年报距今 > 18 个月 | score_ticker（拿到完整 raw 后检查） |

**注意**：不能用 `not profile.get("isActivelyTrading", True)` 判断——当 FMP 返回 `null` 时，Python 得到 `None`，`not None = True` 会误杀正常股票。须显式判断：
```python
is_active = profile.get("isActivelyTrading")
if is_active is not None and not bool(is_active):
    ...  # 确认退市
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

### Point-in-Time (PIT) 历史打分

**问题**：用当前数据给历史季度打分会产生数据穿越（用了未来才有的财报）。

**解法**：`run_scoring.py` 在打历史季度时自动启用 PIT 过滤：
- 自动检测：`quarter_end_date < today` → 启用 PIT 模式
- `apply_pit_filter(raw, as_of_date)` 按季度末日期截断所有含 `date` 字段的列表型数据
- TTM 估值用最近可用年度数据近似替代（`ratios_annual[0]` / `key_metrics_annual[0]`）
- 年报过期检查基准日改为 `as_of_date`（不用 today）
- 退市检查仅在当前打分时执行（历史时该股票可能是活跃的）

**TTM 字段映射（历史近似）**：

| TTM 字段 | 来源（ratios_annual[0]） |
|---------|------------------------|
| `priceToEarningsRatioTTM` | `priceEarningsRatio` |
| `priceToBookRatioTTM` | `priceToBookRatio` |
| `priceToSalesRatioTTM` | `priceToSalesRatio` |
| `priceToEarningsGrowthRatioTTM` | `priceEarningsToGrowthRatio` |
| `evToEBITDATTM` | `evToEbitda`（key_metrics_annual[0]） |
| `freeCashFlowYieldTTM` | `freeCashFlowYield`（key_metrics_annual[0]） |

打分 JSON 中会写入 `"pit_mode": true` 和 `"as_of_date": "2023-03-31"` 供回测引用。

---

## 数据缓存规则

| 数据类型 | TTL | 备注 |
|---------|-----|------|
| 财务报表（损益/资产/现金流） | 90天 | 季报才更新 |
| 估值数据（TTM PE/PB等） | 7天 | 价格每天变 |
| 股票列表 | 30天 | 月度刷新 |
| 公司 profile | 30天 | 含市值/上市日期/退市标记 |
| 日线价格 | 7天 | 回测用 |

缓存存储在 `data/stock_cache.db`（SQLite），key 格式为 `{endpoint}:{ticker}`。

### fetch_data.py 增量续跑机制

`fetch_data.py` 天然支持断点续跑，**无需任何额外参数**：
- 每只股票拉取前调用 `missing_endpoints()` 检查缓存状态
- 已缓存且未过期的端点直接跳过，不发网络请求
- 中途中断后重新运行 `--all`，已拉到的股票全部自动跳过

```bash
# 昨天拉到 KSEA 中断了，今天直接重跑即可，已有数据自动跳过
python fetch_data.py --all --mode core
```

唯一额外开销：启动时需过一遍全量列表做缓存检查（几秒钟）。

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

### 前端功能

**导航栏时间选择器**（两种模式）：
- **单季度模式**：下拉选择单个季度（按年分组），直接查看该季度打分
- **区间模式**：选择起始季度（from）和结束季度（to），点"查看"聚合范围内所有季度

**区间聚合逻辑**：
- 多个季度的各维度分、大师分、综合分取**等权平均**
- 详情页顶部显示蓝色 badge 标注包含的季度及各季得分
- 多季度时自动展示**季度趋势图**（综合分 + 五维分跨季度折线图）

**详情页内容**：
- 公司基本信息 + 关键指标 pills
- 否决状态（通过/触发）
- 四大师得分卡片
- 五维雷达图 + 大师得分柱状图
- 季度趋势图（仅多季度模式）
- 营收/净利润、毛利率/净利率、ROE/ROIC、EPS/FCF 共 4 组时序图表
- 评分维度详情（可展开子指标）
- 大师权重矩阵表
- 关键指标网格

### API 端点

| 端点 | 说明 |
|------|------|
| `GET /api/quarters` | 返回 `{quarters: [...], years: {2026: [...]}}`，按年分组 |
| `GET /api/stocks?quarter=2026-Q1` | 单季度所有股票摘要 |
| `GET /api/stocks?quarters=Q1,Q2,...` | 自定义多季度聚合（等权平均） |
| `GET /api/stocks?year=2026` | 年度聚合（该年所有季度平均） |
| `GET /api/stock/{ticker}?quarter=2026-Q1` | 单股完整数据 + 历史财务时序 |
| `GET /api/stock/{ticker}?quarters=Q1,Q2` | 多季度聚合的单股数据 |
| `GET /api/data?quarter=2026-Q1` | 兼容接口，返回 CSV 平铺数据 |

**参数优先级**：`quarters` > `year` > `quarter`

### 多季度聚合返回的额外字段

| 字段 | 说明 |
|------|------|
| `_quarters_included` | 包含的季度列表，如 `["2023-Q1", "2023-Q2"]` |
| `_quarter_scores` | 各季度综合分，如 `{"2023-Q1": 7.5, "2023-Q2": 8.0}` |
| `_quarter_dimensions` | 各季度五维分 |
| `_quarter_master_scores` | 各季度大师分 |

### 历史财务数据（`financials` 字段）

`/api/stock/{ticker}` 会从 `data/stock_cache.db` 中拼装以下时序（最近5年，倒序）：
- `years` / `revenue` / `net_income` / `gross_margin` / `net_margin`（来自 `income_annual`）
- `free_cash_flow`（来自 `cashflow_annual`）
- `roe` / `roic`（来自 `key_metrics_annual`）
- `eps`（来自 `income_annual.epsDiluted`）

---

## 打分回测验证（backtest.py）

### 核心概念

用 **打分时段** 的评分，验证 **评估时段** 的实际涨幅是否与打分排名一致。

### 时间段输入格式

| 格式 | 示例 | 展开结果 |
|------|------|---------|
| 单季度 | `2023-Q1` | `['2023-Q1']` |
| 多季度 | `"2023-Q1 2023-Q2"` | `['2023-Q1', '2023-Q2']` |
| 单年 | `2023` | `['2023-Q1', ..., '2023-Q4']` |
| 多年 | `"2023 2024"` 或 `2023,2024` | `['2023-Q1', ..., '2024-Q4']` |
| 混合 | `"2023 2024-Q1"` | `['2023-Q1', ..., '2023-Q4', '2024-Q1']` |

### 核心逻辑

```
1. 解析打分时段 + 评估时段 → 季度列表
2. 自动打分（若尚未打分，调用 run_scoring.py 离线打分）
3. 聚合多季度打分（默认等权平均 | 可选取最新）
4. 以打分时段末尾 → 评估时段末尾的股价涨幅计算实际收益
5. 计算 IC / 命中率 / 五分位组收益
6. 输出报告：整体评估 → 个股明细
```

### 多季度聚合方式

| 模式 | `--agg-mode` | 说明 |
|------|-------------|------|
| 等权平均（默认） | `quarter_weighted` | 多个季度的分数取平均 |
| 取最新 | `latest` | 用最新季度的分数 |

### 评估指标

| 指标 | 说明 | 有效阈值 |
|------|------|---------|
| **IC（Spearman 秩相关）** | 预测排名与实际涨幅排名的相关性 | > 0.03 认为有效 |
| **命中率** | 高分股跑赢低分股的概率 | > 50% 优于随机 |
| **五分位组收益** | Q1（高分）到 Q5（低分）各组平均涨幅 | Q1 > Q5 说明区分度好 |
| **Q1-Q5 年化超额** | 高低分位收益差 / 前瞻年数 | 正值且显著为佳 |

### 报告输出结构

```
[总体评估]
  评级: 优秀/良好/有效/较弱/无效
  说明: 一句话评价

[核心指标]
  IC / 命中率 / 有效样本数 / 前瞻期

[五分位组收益]
  Q1~Q5 各组总收益 + 年化收益

[个股明细]
  Top 20 高分股 / Bottom 10 低分股
  最大赢家 Top 5 / 最大输家 Bottom 5
  多季度得分趋势（聚合模式时）
```

### 命令行参数

| 参数 | 默认值 | 说明 |
|------|-------|------|
| `--score-period` | 最新季度 | 打分时段 (支持季度/年/混合格式) |
| `--eval-period` | 必填 | 评估时段 (必须晚于打分时段) |
| `--score-key` | composite_score | 排名依据：composite_score / buffett / munger / duan / lynch |
| `--agg-mode` | quarter_weighted | 多季度聚合：quarter_weighted / latest |
| `--top-n` | 全部 | 只取高分前 N 只 |
| `--all-keys` | — | 对所有打分维度逐一回测并对比 |
| `--include-veto` | — | 包含被否决股票 |
| `--no-auto-score` | — | 不自动打分，仅用已有数据 |
| `--fresh-score` | — | 强制重新打分 |
| `--no-save` | — | 不保存报告文件 |

### 前提条件

需要历史价格数据（`price_daily:{ticker}` 缓存 TTL 7 天）。
建议先运行 `fetch_data.py --all --mode full` 将价格写入缓存，回测时不发出额外网络请求。
打分会自动执行（调用 `run_offline_cached`），也可用 `--no-auto-score` 跳过。

---

## 开发路线图

- **V1（完成）**: 跑通打分流水线 ✓
- **V1.5（完成）**: 可视化看板 ✓（Flask + Chart.js，明亮/夜间模式）
- **V2（完成）**: 打分回测验证 ✓（IC / 五分位 / 命中率，backtest.py）+ PIT 历史打分 ✓（apply_pit_filter，防数据穿越）
- **V2.5（完成）**: 前端区间选择 ✓（单季度/区间模式切换，自定义 from~to 多季度聚合查看）+ 回测 v2 ✓（灵活时间段输入、自动打分、多季度聚合、结构化报告）
- **V3**: LLM 读 10-K 做定性分析
- **V4**: 权重自动优化（基于回测 IC 反向调参）

---

## 注意事项

1. **API Key** 在 `.env` 中，不提交 git。`.env.example` 中有示例。
2. **断点续跑（打分）**：`run_checkpoints` 表记录已打分的 `(quarter, ticker)`，重启不会重复打分。用 `--fresh` 参数强制重跑。
3. **断点续跑（拉取）**：`fetch_data.py` 通过缓存有效期判断自动跳过已拉取数据，重跑 `--all` 即可续跑，无需额外参数。
4. **ETF/基金过滤**：通过 Stage1 市值+流动性过滤，大多数 ETF 会被筛掉。
5. **missing_valuation_ttm**：TTM 估值数据缺失时估值维度得 0 分，JSON 中标记 `data_quality_flags`。
6. **`stable/search-symbol`** 是当前唯一可用的股票搜索端点，已缓存 A 股列表（key: `a_stocks:list`）。
7. **拉取与打分分离**：推荐先用 `fetch_data.py` 把数据拉进缓存，再跑 `run_scoring.py`。打分时若数据已在缓存则不会发出任何网络请求。
8. **cache_key 格式**：`{type}:{TICKER}`，如 `income_annual:AAPL`、`ratios_ttm:AAPL`。每个端点对应固定 key，见 `fetch_data.py` 中的 `_CACHE_KEY_PREFIX`。
9. **Point-in-Time 打分**：`run_scoring.py` 对历史季度（季度末 < 今天）自动启用 PIT 过滤，仅用该季度末之前已发布的财务数据打分，不产生数据穿越。`backtest.py` 直接读取打分 JSON 中的分数，无需重新计算。股价涨幅以季度末收盘价为起点，需确保 `price_daily` 缓存覆盖该日期。
10. **回测有效性条件**：至少需要 5 只股票有完整价格数据才计算 IC；五分位分析至少需要 5 只（每组 1 只）。样本太少时指标不具统计意义。
