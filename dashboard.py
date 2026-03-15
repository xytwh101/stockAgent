"""
可视化看板服务
运行: python dashboard.py
访问: http://localhost:5001
"""
from flask import Flask, render_template, jsonify, request
import os
import json
import sqlite3
import pandas as pd

app = Flask(__name__)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
SCORES_DIR = os.path.join(BASE_DIR, "scores")
DB_PATH    = os.path.join(BASE_DIR, "data", "stock_cache.db")


# ─── helpers ──────────────────────────────────────────────────────────────────

def _safe_quarter(q):
    """防止路径穿越."""
    return os.path.basename(q or "")


def _db_get(ticker: str, key_type: str):
    """从 SQLite 缓存读取 JSON 数据，key 格式: {key_type}:{TICKER}."""
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT data_json FROM cache WHERE cache_key=?",
            (f"{key_type}:{ticker}",),
        ).fetchone()
        conn.close()
        return json.loads(row[0]) if row else None
    except Exception:
        return None


def _build_financials(ticker: str):
    """从 SQLite 提取收入表 + 现金流 + 关键指标，组装 financials 时序数据."""
    income = _db_get(ticker, "income_annual")
    cashflow = _db_get(ticker, "cashflow_annual")
    km = _db_get(ticker, "key_metrics_annual")

    if not income:
        return None

    # income 数据按时间倒序（最新在前），取最多 5 年
    rows_inc = income[:5]
    years = [r.get("fiscalYear") or r.get("date", "")[:4] for r in rows_inc]

    def pick(rows, *keys):
        result = []
        for r in rows:
            for k in keys:
                if r.get(k) is not None:
                    result.append(r[k])
                    break
            else:
                result.append(None)
        return result

    revenue    = pick(rows_inc, "revenue")
    net_income = pick(rows_inc, "netIncome", "bottomLineNetIncome")
    gross_profit = pick(rows_inc, "grossProfit")
    eps        = pick(rows_inc, "epsDiluted", "eps")

    # 毛利率 / 净利率
    gross_margin = [
        gp / rev if (gp is not None and rev and rev != 0) else None
        for gp, rev in zip(gross_profit, revenue)
    ]
    net_margin = [
        ni / rev if (ni is not None and rev and rev != 0) else None
        for ni, rev in zip(net_income, revenue)
    ]

    # 自由现金流
    if cashflow:
        rows_cf = cashflow[:5]
        # 对齐年份（cashflow 可能条数不同）
        cf_map = {r.get("fiscalYear") or r.get("date", "")[:4]: r for r in rows_cf}
        free_cash_flow = [
            cf_map.get(y, {}).get("freeCashFlow") for y in years
        ]
    else:
        free_cash_flow = [None] * len(years)

    # ROE / ROIC（来自 key_metrics_annual）
    roe_vals = roic_vals = [None] * len(years)
    if km:
        km_map = {r.get("date", "")[:4]: r for r in km[:5]}
        roe_vals  = [km_map.get(y, {}).get("roe")  for y in years]
        roic_vals = [km_map.get(y, {}).get("roic") for y in years]

    return {
        "years": years,
        "revenue": revenue,
        "net_income": net_income,
        "gross_margin": gross_margin,
        "net_margin": net_margin,
        "roe": roe_vals,
        "roic": roic_vals,
        "eps": eps,
        "free_cash_flow": free_cash_flow,
    }


# ─── routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/quarters")
def list_quarters():
    """返回季度列表 + 按年分组结构.

    返回格式:
    {
      "quarters": ["2026-Q1", "2025-Q4", ...],
      "years": {"2026": ["2026-Q1"], "2025": ["2025-Q4", ...]}
    }
    """
    quarters = []
    if os.path.exists(SCORES_DIR):
        for d in sorted(os.listdir(SCORES_DIR), reverse=True):
            csv_path = os.path.join(SCORES_DIR, d, "_summary.csv")
            if os.path.exists(csv_path):
                quarters.append(d)

    # 按年分组
    years = {}
    for q in quarters:
        year = q.split("-")[0]
        years.setdefault(year, []).append(q)

    return jsonify({"quarters": quarters, "years": years})


def _load_quarter_stocks(quarter: str):
    """加载单个季度的所有股票数据."""
    quarter_dir = os.path.join(SCORES_DIR, _safe_quarter(quarter))
    stocks = []
    if os.path.isdir(quarter_dir):
        for fname in os.listdir(quarter_dir):
            if not fname.endswith(".json") or fname.startswith("_"):
                continue
            try:
                with open(os.path.join(quarter_dir, fname), encoding="utf-8") as f:
                    stocks.append(json.load(f))
            except Exception:
                continue
    if not stocks:
        csv_path = os.path.join(quarter_dir, "_summary.csv")
        if os.path.exists(csv_path):
            df = pd.read_csv(csv_path)
            df = df.where(pd.notna(df), None)
            stocks = df.to_dict(orient="records")
    return stocks


def _aggregate_multi_quarter_stocks(quarter_list: list[str]):
    """将多个季度的打分取平均，返回聚合后的股票列表.

    通用聚合函数：支持年度汇总、自定义区间等。
    """
    if not quarter_list:
        return []

    # 收集所有季度数据，按 ticker 聚合
    ticker_data = {}  # ticker -> list of stock dicts
    for q in sorted(quarter_list):
        for s in _load_quarter_stocks(q):
            t = s.get("ticker")
            if t:
                ticker_data.setdefault(t, []).append(s)

    if not ticker_data:
        return []

    DIM_KEYS = ["business_quality", "financial_health", "growth", "management", "valuation"]
    MASTER_KEYS = ["buffett", "munger", "duan", "lynch"]

    aggregated = []
    for ticker, entries in ticker_data.items():
        latest = entries[-1]

        def avg_field(field_path, entries):
            vals = []
            for e in entries:
                v = e
                for k in field_path:
                    v = (v or {}).get(k) if isinstance(v, dict) else None
                if v is not None:
                    vals.append(v)
            return sum(vals) / len(vals) if vals else None

        label = quarter_list[0] if len(quarter_list) == 1 else f"{quarter_list[0]}~{quarter_list[-1]}"
        agg = {
            "ticker": ticker,
            "quarter": label,
            "composite_score": avg_field(["composite_score"], entries) or 0,
            "veto_triggered": any(e.get("veto_triggered") for e in entries),
            "veto_reasons": list({r for e in entries for r in (e.get("veto_reasons") or [])}),
            "dimensions": {k: avg_field(["dimensions", k], entries) or 0 for k in DIM_KEYS},
            "master_scores": {k: avg_field(["master_scores", k], entries) or 0 for k in MASTER_KEYS},
            "company_info": latest.get("company_info", {}),
            "key_metrics": latest.get("key_metrics", {}),
            "data_quality_flags": latest.get("data_quality_flags", []),
            "years_of_data": latest.get("years_of_data", 0),
            "_quarters_included": [e.get("quarter") for e in entries],
            "_quarter_scores": {
                e.get("quarter"): e.get("composite_score") for e in entries
            },
            "_quarter_dimensions": {
                e.get("quarter"): e.get("dimensions", {}) for e in entries
            },
            "_quarter_master_scores": {
                e.get("quarter"): e.get("master_scores", {}) for e in entries
            },
        }
        aggregated.append(agg)

    aggregated.sort(key=lambda s: (s.get("composite_score") or 0), reverse=True)
    return aggregated


def _aggregate_year_stocks(year: str):
    """将一年内所有季度的打分取平均（兼容旧调用）."""
    quarter_dirs = []
    if os.path.exists(SCORES_DIR):
        for d in sorted(os.listdir(SCORES_DIR)):
            if d.startswith(f"{year}-Q"):
                quarter_dirs.append(d)
    return _aggregate_multi_quarter_stocks(quarter_dirs)


@app.route("/api/stocks")
def list_stocks():
    """返回某季度/年度/自定义区间所有股票的摘要.

    参数（优先级从高到低）:
      quarters=2023-Q1,2023-Q2  → 自定义多季度聚合
      year=2026                 → 年度聚合
      quarter=2026-Q1           → 单季度
    """
    quarters_raw = request.args.get("quarters", "").strip()
    year = request.args.get("year", "").strip()
    quarter = _safe_quarter(request.args.get("quarter", ""))

    if quarters_raw:
        quarter_list = [_safe_quarter(q) for q in quarters_raw.split(",") if q.strip()]
        if len(quarter_list) == 1:
            stocks = _load_quarter_stocks(quarter_list[0])
            stocks.sort(key=lambda s: (s.get("composite_score") or 0), reverse=True)
        else:
            stocks = _aggregate_multi_quarter_stocks(quarter_list)
    elif year:
        stocks = _aggregate_year_stocks(year)
    else:
        stocks = _load_quarter_stocks(quarter)
        stocks.sort(key=lambda s: (s.get("composite_score") or 0), reverse=True)

    return jsonify(stocks)


@app.route("/api/stock/<ticker>")
def get_stock(ticker: str):
    """返回单只股票的完整数据，附带历史财务时序.

    支持:
      ?quarters=2023-Q1,2023-Q2  自定义多季度聚合
      ?year=2026                 年度聚合
      ?quarter=2026-Q1           单季度
    """
    quarters_raw = request.args.get("quarters", "").strip()
    year = request.args.get("year", "").strip()
    quarter = _safe_quarter(request.args.get("quarter", ""))

    if quarters_raw:
        quarter_list = [_safe_quarter(q) for q in quarters_raw.split(",") if q.strip()]
        if len(quarter_list) == 1:
            json_path = os.path.join(SCORES_DIR, quarter_list[0], f"{ticker}.json")
            if not os.path.exists(json_path):
                return jsonify({"error": "Not found"}), 404
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
        else:
            stocks = _aggregate_multi_quarter_stocks(quarter_list)
            data = next((s for s in stocks if s["ticker"] == ticker), None)
            if not data:
                return jsonify({"error": "Not found"}), 404
    elif year:
        stocks = _aggregate_year_stocks(year)
        data = next((s for s in stocks if s["ticker"] == ticker), None)
        if not data:
            return jsonify({"error": "Not found"}), 404
    else:
        json_path = os.path.join(SCORES_DIR, quarter, f"{ticker}.json")
        if not os.path.exists(json_path):
            return jsonify({"error": "Not found"}), 404
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

    # 附加历史财务数据（图表用）
    fin = _build_financials(ticker)
    if fin:
        data["financials"] = fin

    return jsonify(data)


@app.route("/api/data")
def get_data():
    """兼容接口：返回 CSV 平铺数据."""
    quarter = _safe_quarter(request.args.get("quarter", ""))
    csv_path = os.path.join(SCORES_DIR, quarter, "_summary.csv")
    if not os.path.exists(csv_path):
        return jsonify({"error": "File not found"}), 404
    df = pd.read_csv(csv_path)
    df = df.where(pd.notna(df), None)
    return jsonify(df.to_dict(orient="records"))


# ─── entry ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import threading
    import webbrowser

    threading.Timer(1.2, lambda: webbrowser.open("http://localhost:5001")).start()
    print("🚀 看板启动中... http://localhost:5001")
    app.run(debug=False, port=5001)
