"""
inspect_cache.py — 查询 stock_cache.db 离线数据概况

用法:
  python inspect_cache.py                          # 总览
  python inspect_cache.py --detail                 # 列出每只 ticker 已有哪些数据
  python inspect_cache.py --ticker AAPL            # 查看某只股票的缓存明细
  python inspect_cache.py --ticker AAPL --show balance_annual   # 格式化展示财务报表
  python inspect_cache.py --ticker AAPL --json income_annual    # 打印原始 JSON
  python inspect_cache.py --list                   # 列出所有已缓存的 ticker

--show 支持的 key_type:
  balance_annual / balance_quarter   资产负债表
  income_annual  / income_quarter    损益表
  cashflow_annual / cashflow_quarter 现金流量表
  key_metrics_annual / key_metrics_ttm
  ratios_annual  / ratios_ttm
  profile
"""

import sqlite3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "stock_cache.db"

# cache_key 前缀 → 可读名称
PREFIX_MAP = {
    "a_stocks":         "A股列表",
    "universe":         "全量股票列表",
    "income":           "损益表",
    "balance":          "资产负债表",
    "cashflow":         "现金流量表",
    "key_metrics_ttm":  "关键指标 TTM",
    "key_metrics":      "关键指标",
    "ratios_ttm":       "财务比率 TTM",
    "ratios":           "财务比率",
    "profile":          "公司 Profile",
    "price_daily":      "每日价格",
    "dividend":         "分红数据",
    "ev_annual":        "EV 年度",
    "income_growth":    "营收增长",
}


def classify(key: str) -> str:
    for prefix, label in PREFIX_MAP.items():
        if key.startswith(prefix):
            return label
    return key.split(":")[0]


def fmt_ts(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def is_expired(ts: int, ttl: int) -> bool:
    now = datetime.now(tz=timezone.utc).timestamp()
    return now > ts + ttl * 86400


# ── 总览 ────────────────────────────────────────────────────────────────────

def summary(conn: sqlite3.Connection):
    print("\n=== 缓存总览 ===")
    cur = conn.execute("SELECT COUNT(*), ROUND(SUM(LENGTH(data_json))/1024.0/1024.0,2) FROM cache")
    total_records, total_mb = cur.fetchone()
    print(f"  总条数: {total_records}   总大小: {total_mb} MB")

    cur = conn.execute("SELECT COUNT(*) FROM run_checkpoints")
    (checkpointed,) = cur.fetchone()
    print(f"  已打分股票 (checkpoints): {checkpointed}")

    print("\n=== 按数据类型分布 ===")
    cur = conn.execute("SELECT cache_key, LENGTH(data_json), fetched_at, ttl_days FROM cache")

    from collections import defaultdict
    groups = defaultdict(lambda: {"count": 0, "expired": 0, "size": 0, "oldest": None, "newest": None})
    for key, sz, ts, ttl in cur:
        label = classify(key)
        g = groups[label]
        g["count"] += 1
        g["size"] += sz
        if is_expired(ts, ttl):
            g["expired"] += 1
        if g["oldest"] is None or ts < g["oldest"]:
            g["oldest"] = ts
        if g["newest"] is None or ts > g["newest"]:
            g["newest"] = ts

    header = f"  {'数据类型':<20} {'条数':>6} {'已过期':>6} {'大小':>8}  {'最早获取':<18} {'最新获取'}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for label, g in sorted(groups.items(), key=lambda x: -x[1]["count"]):
        size_str = f"{g['size']/1024/1024:.2f} MB" if g['size'] >= 1024 * 100 else f"{g['size']//1024} KB"
        expired_str = f"{g['expired']}" if g["expired"] else "-"
        print(f"  {label:<20} {g['count']:>6} {expired_str:>6} {size_str:>8}  {fmt_ts(g['oldest']):<18} {fmt_ts(g['newest'])}")


# ── 已打分记录 ───────────────────────────────────────────────────────────────

def checkpoints(conn: sqlite3.Connection):
    print("\n=== 已打分记录 ===")
    try:
        cur = conn.execute("SELECT quarter, ticker, scored_at FROM run_checkpoints ORDER BY scored_at DESC")
        rows = cur.fetchall()
        if not rows:
            print("  (空)")
            return
        for quarter, ticker, ts in rows:
            print(f"  {quarter}  {ticker:<8}  {fmt_ts(ts)}")
    except sqlite3.OperationalError:
        print("  run_checkpoints 表结构不符，跳过")


# ── 全部 ticker 列表 ─────────────────────────────────────────────────────────

def list_tickers(conn: sqlite3.Connection):
    print("\n=== 已缓存的 ticker 列表 ===")
    cur = conn.execute("SELECT DISTINCT cache_key FROM cache ORDER BY cache_key")
    tickers = set()
    for (key,) in cur:
        parts = key.split(":")
        if len(parts) >= 2 and parts[1] not in ("", "all"):
            tickers.add(parts[1].upper())

    tickers = sorted(tickers)
    cols = 8
    for i, t in enumerate(tickers):
        end = "\n" if (i + 1) % cols == 0 else "  "
        print(f"  {t:<8}", end=end)
    print(f"\n\n  共 {len(tickers)} 只")


# ── 按 ticker 展示明细 ───────────────────────────────────────────────────────

def ticker_detail(conn: sqlite3.Connection, ticker: str):
    sym = ticker.upper()
    print(f"\n=== {sym} 缓存明细 ===")

    cur = conn.execute(
        "SELECT cache_key, LENGTH(data_json), fetched_at, ttl_days FROM cache "
        "WHERE cache_key LIKE ? OR cache_key LIKE ?",
        (f"%:{sym}", f"%:{sym}:%")
    )
    rows = cur.fetchall()
    if not rows:
        print(f"  未找到 {sym} 的缓存数据，可用 --list 查看所有已缓存 ticker")
        return

    now = datetime.now(tz=timezone.utc).timestamp()
    print(f"  {'数据类型':<16} {'缓存 key':<30} {'大小':>7}  {'获取时间':<18} {'过期时间':<12} {'状态'}")
    print("  " + "-" * 100)
    for key, sz, ts, ttl in sorted(rows, key=lambda r: r[0]):
        expires_ts = ts + ttl * 86400
        expires_str = datetime.fromtimestamp(expires_ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
        status = "✗ 已过期" if now > expires_ts else "✓ 有效"
        sz_str = f"{sz//1024} KB" if sz >= 1024 else f"{sz} B"
        label = classify(key)
        print(f"  {label:<16} {key:<30} {sz_str:>7}  {fmt_ts(ts):<18} {expires_str:<12} {status}")

    # 是否有打分记录
    try:
        cur = conn.execute(
            "SELECT quarter, scored_at FROM run_checkpoints WHERE ticker=? ORDER BY scored_at DESC",
            (sym,)
        )
        scored = cur.fetchall()
        if scored:
            print(f"\n  打分记录:")
            for quarter, ts in scored:
                print(f"    {quarter}  {fmt_ts(ts)}")
    except sqlite3.OperationalError:
        pass


# ── 格式化展示财务报表 ────────────────────────────────────────────────────────

# 各报表需要展示的字段（中文名, key）
SHOW_FIELDS = {
    "balance": [
        ("现金及短期投资",       "cashAndShortTermInvestments"),
        ("  现金",              "cashAndCashEquivalents"),
        ("  短期投资",           "shortTermInvestments"),
        ("应收账款",             "netReceivables"),
        ("存货",                "inventory"),
        ("流动资产合计",          "totalCurrentAssets"),
        ("─" * 30,             None),
        ("固定资产(净)",          "propertyPlantEquipmentNet"),
        ("商誉",                "goodwill"),
        ("无形资产",             "intangibleAssets"),
        ("长期投资",             "longTermInvestments"),
        ("非流动资产合计",         "totalNonCurrentAssets"),
        ("─" * 30,             None),
        ("总资产",              "totalAssets"),
        ("─" * 30,             None),
        ("短期债务",             "shortTermDebt"),
        ("应付账款",             "accountPayables"),
        ("流动负债合计",          "totalCurrentLiabilities"),
        ("长期债务",             "longTermDebt"),
        ("非流动负债合计",         "totalNonCurrentLiabilities"),
        ("总负债",              "totalLiabilities"),
        ("─" * 30,             None),
        ("股东权益合计",          "totalStockholdersEquity"),
        ("负债+权益合计",         "totalLiabilitiesAndStockholdersEquity"),
    ],
    "income": [
        ("营业收入",             "revenue"),
        ("营业成本",             "costOfRevenue"),
        ("毛利润",              "grossProfit"),
        ("─" * 30,             None),
        ("研发费用",             "researchAndDevelopmentExpenses"),
        ("销售管理费用",          "sellingGeneralAndAdministrativeExpenses"),
        ("营业费用合计",          "operatingExpenses"),
        ("营业利润",             "operatingIncome"),
        ("─" * 30,             None),
        ("利息费用",             "interestExpense"),
        ("税前利润",             "incomeBeforeTax"),
        ("所得税",              "incomeTaxExpense"),
        ("净利润",              "netIncome"),
        ("─" * 30,             None),
        ("EBITDA",             "ebitda"),
        ("基本EPS($)",          "eps"),
        ("稀释EPS($)",          "epsDiluted"),
        ("加权平均股数(稀释,亿)",   "weightedAverageShsOutDil"),
    ],
    "cashflow": [
        ("经营活动现金流(OCF)",    "operatingCashFlow"),
        ("  净利润",             "netIncome"),
        ("  折旧摊销",            "depreciationAndAmortization"),
        ("  营运资本变动",         "changeInWorkingCapital"),
        ("─" * 30,             None),
        ("资本支出(Capex)",       "capitalExpenditure"),
        ("自由现金流(FCF)",       "freeCashFlow"),
        ("─" * 30,             None),
        ("投资活动现金流",         "netCashUsedForInvestingActivites"),
        ("融资活动现金流",         "netCashUsedProvidedByFinancingActivities"),
        ("净现金变动",            "netChangeInCash"),
        ("期末现金",             "cashAtEndOfPeriod"),
    ],
    "key_metrics": [
        ("市值(亿)",             "marketCap"),
        ("企业价值(亿)",          "enterpriseValue"),
        ("PE",                 "peRatio"),
        ("PB",                 "pbRatio"),
        ("PS",                 "priceToSalesRatio"),
        ("EV/EBITDA",          "evToEbitda"),
        ("EV/FCF",             "evToFreeCashFlow"),
        ("─" * 30,             None),
        ("ROE",                "roe"),
        ("ROA",                "roa"),
        ("ROIC",               "roic"),
        ("─" * 30,             None),
        ("每股FCF",             "freeCashFlowPerShare"),
        ("每股收入",             "revenuePerShare"),
        ("净债务/EBITDA",        "netDebtToEBITDA"),
        ("流动比率",             "currentRatio"),
        ("负债/权益",            "debtToEquity"),
    ],
    "ratios": [
        ("毛利率",              "grossProfitMargin"),
        ("营业利润率",            "operatingProfitMargin"),
        ("净利润率",             "netProfitMargin"),
        ("FCF利润率",           "freeCashFlowMargin"),
        ("─" * 30,             None),
        ("ROE",                "returnOnEquity"),
        ("ROA",                "returnOnAssets"),
        ("ROIC",               "returnOnCapitalEmployed"),
        ("─" * 30,             None),
        ("PE",                 "priceEarningsRatio"),
        ("PB",                 "priceToBookRatio"),
        ("PS",                 "priceToSalesRatio"),
        ("PEG",                "priceEarningsToGrowthRatio"),
        ("EV/EBITDA",          "enterpriseValueMultiple"),
        ("─" * 30,             None),
        ("流动比率",             "currentRatio"),
        ("速动比率",             "quickRatio"),
        ("负债/权益",            "debtEquityRatio"),
        ("利息覆盖倍数",          "interestCoverage"),
    ],
    # ratios_ttm 字段名带 TTM 后缀
    "ratios_ttm": [
        ("毛利率",              "grossProfitMarginTTM"),
        ("营业利润率",            "operatingProfitMarginTTM"),
        ("净利润率",             "netProfitMarginTTM"),
        ("FCF/OCF",            "freeCashFlowOperatingCashFlowRatioTTM"),
        ("─" * 30,             None),
        ("PE",                 "priceToEarningsRatioTTM"),
        ("PB",                 "priceToBookRatioTTM"),
        ("PS",                 "priceToSalesRatioTTM"),
        ("PEG",                "priceToEarningsGrowthRatioTTM"),
        ("EV/EBITDA",          "enterpriseValueMultipleTTM"),
        ("─" * 30,             None),
        ("流动比率",             "currentRatioTTM"),
        ("速动比率",             "quickRatioTTM"),
        ("负债/权益",            "debtToEquityRatioTTM"),
        ("利息覆盖倍数",          "interestCoverageRatioTTM"),
        ("─" * 30,             None),
        ("每股FCF($)",          "freeCashFlowPerShareTTM"),
        ("每股收入($)",          "revenuePerShareTTM"),
        ("股息率",              "dividendYieldTTM"),
    ],
}

# TTM/单期报表字段映射（key_metrics_ttm / ratios_ttm / profile 是 dict 不是 list）
SHOW_FIELDS["balance_annual"]  = SHOW_FIELDS["balance"]
SHOW_FIELDS["balance_quarter"] = SHOW_FIELDS["balance"]
SHOW_FIELDS["income_annual"]   = SHOW_FIELDS["income"]
SHOW_FIELDS["income_quarter"]  = SHOW_FIELDS["income"]
SHOW_FIELDS["cashflow_annual"] = SHOW_FIELDS["cashflow"]
SHOW_FIELDS["cashflow_quarter"]= SHOW_FIELDS["cashflow"]
SHOW_FIELDS["key_metrics_annual"] = SHOW_FIELDS["key_metrics"]
SHOW_FIELDS["key_metrics_ttm"]    = SHOW_FIELDS["key_metrics"]
SHOW_FIELDS["ratios_annual"]   = SHOW_FIELDS["ratios"]
# ratios_ttm 已在字典内单独定义，不覆盖


def fmt_val(v, is_ratio: bool = False, is_multiple: bool = False, is_dollar: bool = False) -> str:
    """将数值格式化为易读形式"""
    if v is None:
        return "N/A"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        if is_dollar:
            return f"${v:.2f}"
        if is_ratio:
            # 利润率/收益率等：小数转百分比
            return f"{v*100:.1f}%"
        if is_multiple:
            # 倍数（流动比率、覆盖倍数等）
            return f"{v:.2f}x"
        abs_v = abs(v)
        sign = "-" if v < 0 else ""
        if abs_v >= 1e9:
            return f"{sign}{abs_v/1e8:.1f}亿"
        if abs_v >= 1e6:
            return f"{sign}{abs_v/1e6:.1f}M"
        if abs_v < 500:
            return f"{v:.2f}x"   # 小数值（PE/PB/EPS 等）
        return f"{v:,.0f}"
    return str(v)


def show_financials(conn: sqlite3.Connection, ticker: str, key_type: str):
    """格式化展示财务报表数据"""
    sym = ticker.upper()
    cur = conn.execute(
        "SELECT data_json FROM cache WHERE cache_key = ? OR cache_key LIKE ?",
        (f"{key_type}:{sym}", f"{key_type}:{sym}:%")
    )
    row = cur.fetchone()
    if not row:
        print(f"  未找到 {key_type}:{sym}，请先运行打分流水线拉取数据")
        return

    data = json.loads(row[0])

    # 找到字段定义：先精确匹配，再按前缀匹配（避免 ratios 截获 ratios_ttm）
    fields = SHOW_FIELDS.get(key_type)
    if fields is None:
        for k in SHOW_FIELDS:
            if key_type.startswith(k):
                fields = SHOW_FIELDS[k]
                break

    # profile 单独处理
    if key_type == "profile":
        info = data[0] if isinstance(data, list) else data
        print(f"\n=== {sym} 公司信息 ===")
        for label, key in [
            ("公司名",    "companyName"), ("交易所",   "exchangeShortName"),
            ("行业",      "industry"),    ("板块",     "sector"),
            ("市值(亿)",  "mktCap"),      ("员工数",   "fullTimeEmployees"),
            ("上市日期",  "ipoDate"),     ("网站",     "website"),
            ("描述",      "description"),
        ]:
            val = info.get(key, "N/A")
            if key == "mktCap" and isinstance(val, (int, float)):
                val = f"{val/1e8:.1f}亿"
            if key == "description":
                val = str(val)[:200] + "..." if len(str(val)) > 200 else val
            print(f"  {label:<10} {val}")
        return

    if fields is None:
        print(f"  暂不支持 {key_type} 的格式化展示，请用 --json 查看原始数据")
        return

    # 统一为列表
    records = data if isinstance(data, list) else [data]

    # 只取最近5期（TTM 只有1条）
    records = records[:5]
    dates = [r.get("date", r.get("calendarYear", "TTM")) for r in records]

    print(f"\n=== {sym} {classify(key_type + ':x')} ===  (单位: 亿=×1e8, M=×1e6, %=百分比)\n")
    col_w = 14
    header = f"  {'指标':<28}" + "".join(f"{d:>{col_w}}" for d in dates)
    print(header)
    print("  " + "-" * (28 + col_w * len(dates)))

    # 字段名包含这些关键词时当比率（小数→%）处理
    MARGIN_KEYWORDS = ("Margin", "margin", "Yield", "yield", "Rate", "rate",
                       "Payout", "payout", "TaxRate", "taxRate")
    # 这些字段强制当倍数（不转%）
    MULTIPLE_KEYWORDS = ("Ratio", "ratio", "Coverage", "coverage",
                         "Leverage", "leverage", "Multiple", "multiple",
                         "Turnover", "turnover")
    for label, key in fields:
        if key is None:
            print(f"  {label}")
            continue
        is_dollar = label.endswith("($)")
        is_margin = (not is_dollar) and any(k in key for k in MARGIN_KEYWORDS)
        is_multiple = (not is_dollar) and (not is_margin) and any(k in key for k in MULTIPLE_KEYWORDS)
        vals = [fmt_val(r.get(key), is_ratio=is_margin, is_multiple=is_multiple, is_dollar=is_dollar) for r in records]
        print(f"  {label:<28}" + "".join(f"{v:>{col_w}}" for v in vals))


# ── 打印原始 JSON ────────────────────────────────────────────────────────────

def print_raw_json(conn: sqlite3.Connection, ticker: str, key_type: str):
    sym = ticker.upper()
    # 精确匹配 key_type:SYM 或 key_type:SYM:...
    cur = conn.execute(
        "SELECT cache_key, data_json FROM cache "
        "WHERE cache_key = ? OR cache_key LIKE ?",
        (f"{key_type}:{sym}", f"{key_type}:{sym}:%")   # 精确匹配 + 带后缀的变体
    )
    rows = cur.fetchall()
    if not rows:
        print(f"  未找到 key: {key_type}:{sym}")
        return
    for key, raw in rows:
        print(f"\n--- {key} ---")
        try:
            parsed = json.loads(raw)
            # 如果是列表，只打印前2条
            if isinstance(parsed, list):
                print(json.dumps(parsed[:2], indent=2, ensure_ascii=False))
                if len(parsed) > 2:
                    print(f"  ... (共 {len(parsed)} 条，仅显示前 2 条)")
            else:
                print(json.dumps(parsed, indent=2, ensure_ascii=False))
        except json.JSONDecodeError:
            print(raw[:500])


# ── 一键展示所有财务数据 ──────────────────────────────────────────────────────

# --show detail 展示顺序
DETAIL_SECTIONS = [
    "profile",
    "income_annual",
    "cashflow_annual",
    "balance_annual",
    "ratios_ttm",
    "key_metrics_annual",
]


def show_all(conn: sqlite3.Connection, ticker: str):
    sym = ticker.upper()
    # 先显示缓存明细，让用户知道哪些数据可用
    ticker_detail(conn, sym)
    print("\n" + "═" * 80)

    for key_type in DETAIL_SECTIONS:
        # 检查数据是否存在
        cur = conn.execute(
            "SELECT 1 FROM cache WHERE cache_key = ? OR cache_key LIKE ? LIMIT 1",
            (f"{key_type}:{sym}", f"{key_type}:{sym}:%")
        )
        if cur.fetchone() is None:
            print(f"\n  ⚠  {key_type} 数据未缓存，跳过")
            continue
        show_financials(conn, sym, key_type)
        print()


# ── 每只 ticker 已有哪些数据（紧凑列表）────────────────────────────────────

def detail(conn: sqlite3.Connection):
    print("\n=== 各 ticker 已缓存数据类型 ===")
    cur = conn.execute("SELECT cache_key, fetched_at, ttl_days FROM cache ORDER BY cache_key")

    from collections import defaultdict
    tickers = defaultdict(list)
    for (key, *_) in cur:
        parts = key.split(":")
        if len(parts) >= 2 and parts[1] not in ("", "all"):
            tickers[parts[1].upper()].append(classify(key))
        else:
            tickers["(meta)"].append(key)

    for ticker, types in sorted(tickers.items()):
        unique = sorted(set(types))
        print(f"  {ticker:<8} {', '.join(unique)}")


# ── 入口 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="查询 stock_cache.db 离线数据概况",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python inspect_cache.py
  python inspect_cache.py --list
  python inspect_cache.py --ticker AAPL
  python inspect_cache.py --ticker AAPL --json income_annual
  python inspect_cache.py --detail
"""
    )
    parser.add_argument("--detail", action="store_true", help="列出每只 ticker 已有哪些数据类型")
    parser.add_argument("--list",   action="store_true", help="列出所有已缓存的 ticker")
    parser.add_argument("--ticker", metavar="SYM",       help="查看指定股票的缓存明细（如 AAPL）")
    parser.add_argument("--show",   metavar="KEY_TYPE",  help="格式化展示财务数据（如 balance_annual / income_annual / cashflow_annual / ratios_ttm / profile）")
    parser.add_argument("--json",   metavar="KEY_TYPE",  help="打印某条缓存的原始 JSON（如 income_annual）")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"数据库不存在: {DB_PATH}")
        exit(1)

    with sqlite3.connect(DB_PATH) as conn:
        if args.ticker:
            if args.show == "detail":
                show_all(conn, args.ticker)
            elif args.show:
                show_financials(conn, args.ticker, args.show)
            elif args.json:
                ticker_detail(conn, args.ticker)
                print_raw_json(conn, args.ticker, args.json)
            else:
                ticker_detail(conn, args.ticker)
        elif args.list:
            list_tickers(conn)
        elif args.detail:
            detail(conn)
        else:
            summary(conn)
            checkpoints(conn)
