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
    quarters = []
    if os.path.exists(SCORES_DIR):
        for d in sorted(os.listdir(SCORES_DIR), reverse=True):
            csv_path = os.path.join(SCORES_DIR, d, "_summary.csv")
            if os.path.exists(csv_path):
                quarters.append(d)
    return jsonify(quarters)


@app.route("/api/stocks")
def list_stocks():
    """返回某季度所有股票的摘要（供侧边栏列表使用）."""
    quarter = _safe_quarter(request.args.get("quarter", ""))
    quarter_dir = os.path.join(SCORES_DIR, quarter)

    # 优先从 JSON 文件读（包含 company_info / master_scores 等嵌套结构）
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

    # fallback: CSV
    if not stocks:
        csv_path = os.path.join(quarter_dir, "_summary.csv")
        if os.path.exists(csv_path):
            df = pd.read_csv(csv_path)
            df = df.where(pd.notna(df), None)
            stocks = df.to_dict(orient="records")

    stocks.sort(key=lambda s: (s.get("composite_score") or 0), reverse=True)
    return jsonify(stocks)


@app.route("/api/stock/<ticker>")
def get_stock(ticker: str):
    """返回单只股票的完整数据，附带历史财务时序."""
    quarter = _safe_quarter(request.args.get("quarter", ""))
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
