"""
app.py — 投资大师选股系统 Web 服务

用法:
    python app.py
    # 访问 http://localhost:8000
"""
from __future__ import annotations

import json
import os
import sys

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

sys.path.insert(0, os.path.dirname(__file__))

from src.fetcher import DataFetcher
from src.normalizer import Normalizer
from src.scorer import DimensionScorer

app = FastAPI(title="投资大师选股系统")
SCORES_DIR = "scores"


def get_quarters() -> list[str]:
    if not os.path.exists(SCORES_DIR):
        return []
    return sorted(
        [d for d in os.listdir(SCORES_DIR) if os.path.isdir(os.path.join(SCORES_DIR, d))],
        reverse=True,
    )


def load_all_results(quarter: str) -> list[dict]:
    dir_path = os.path.join(SCORES_DIR, quarter)
    if not os.path.exists(dir_path):
        return []
    results = []
    for fn in os.listdir(dir_path):
        if fn.endswith(".json") and not fn.startswith("_"):
            try:
                with open(os.path.join(dir_path, fn), encoding="utf-8") as f:
                    results.append(json.load(f))
            except Exception:
                pass
    return results


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(os.path.dirname(__file__), "templates", "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/quarters")
async def api_quarters():
    return get_quarters()


@app.get("/api/stocks")
async def api_stocks(quarter: str = "", q: str = ""):
    quarter = quarter or (get_quarters() or [""])[0]
    if not quarter:
        return []
    results = load_all_results(quarter)
    if q:
        ql = q.lower()
        results = [
            r for r in results
            if ql in r.get("ticker", "").lower()
            or ql in (r.get("company_info") or {}).get("name", "").lower()
        ]
    return sorted(results, key=lambda r: -r.get("composite_score", 0))


@app.get("/api/stock/{ticker}")
async def api_stock_detail(ticker: str, quarter: str = ""):
    quarter = quarter or (get_quarters() or [""])[0]
    if not quarter:
        raise HTTPException(404, "No scored data found")

    path = os.path.join(SCORES_DIR, quarter, f"{ticker}.json")
    if not os.path.exists(path):
        raise HTTPException(404, f"{ticker} not found in {quarter}")

    with open(path, encoding="utf-8") as f:
        result = json.load(f)

    # Load financial time-series from cache + compute score breakdown
    try:
        fetcher = DataFetcher()
        normalizer = Normalizer()
        dim_scorer = DimensionScorer()

        raw = fetcher.get_all_financial_data(ticker)
        fin = normalizer.normalize(raw)
        fetcher.close()

        years = [s.get("date", "")[:4] for s in raw.get("income_annual", [])[:10]]

        result["financials"] = {
            "years":           years,
            "revenue":         fin.revenue[:10],
            "net_income":      fin.net_income[:10],
            "gross_profit":    fin.gross_profit[:10],
            "eps":             fin.eps[:10],
            "free_cash_flow":  fin.free_cash_flow[:10],
            "gross_margin":    fin.gross_margin[:10],
            "net_margin":      fin.net_margin[:10],
            "roe":             fin.roe[:10],
            "roic":            fin.roic[:10],
            "debt_to_equity":  fin.debt_to_equity[:10],
            "operating_cash_flow": fin.operating_cash_flow[:10],
        }
        result["score_breakdown"] = dim_scorer.score_all_explain(fin)

    except Exception as e:
        result["financials"] = None
        result["score_breakdown"] = None
        result["_financial_error"] = str(e)

    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
