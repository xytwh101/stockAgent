"""
run_a_stocks.py — 对所有A开头的美股打分，输出Top 10

用法：
    python run_a_stocks.py
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from config import DIMENSION_WEIGHTS, OUTPUT_CONFIG
from src.fetcher import DataFetcher
from src.normalizer import Normalizer
from src.scorer import DimensionScorer, FilterFunnel, MasterScorer, VetoEngine
from src.masters import buffett, munger, duan, lynch

MASTERS = {
    "buffett": buffett,
    "munger":  munger,
    "duan":    duan,
    "lynch":   lynch,
}

QUARTER = datetime.now().strftime("%Y-Q") + str((datetime.now().month - 1) // 3 + 1)
OUT_DIR = os.path.join("scores", QUARTER)
os.makedirs(OUT_DIR, exist_ok=True)


def score_ticker(ticker, fetcher, normalizer, dim_scorer, master_scorer, veto_engine):
    try:
        raw = fetcher.get_all_financial_data(ticker)
        fin = normalizer.normalize(raw)
        if fin.years_of_data < 2:
            return None

        global_vetoes = veto_engine.check(fin)
        dim_scores = dim_scorer.score_all(fin)

        master_scores = {}
        master_vetoes = []
        for name, mod in MASTERS.items():
            master_scores[name] = mod.apply_weights(dim_scores)
            master_vetoes.extend(mod.extra_vetoes(fin))

        all_vetoes = global_vetoes + master_vetoes
        composite = sum(DIMENSION_WEIGHTS[d] * dim_scores.get(d, 0) for d in DIMENSION_WEIGHTS)

        return {
            "ticker": ticker,
            "scored_at": datetime.now().isoformat(),
            "quarter": QUARTER,
            "composite_score": round(composite, 4),
            "veto_triggered": len(all_vetoes) > 0,
            "veto_reasons": all_vetoes,
            "dimensions": {k: round(v, 4) for k, v in dim_scores.items()},
            "master_scores": {k: round(v, 4) for k, v in master_scores.items()},
            "company_info": {
                "name": fin.company_name,
                "sector": fin.sector,
                "industry": fin.industry,
                "market_cap": fin.market_cap,
            },
            "key_metrics": {
                "pe_ratio": fin.pe_ratio,
                "pb_ratio": fin.pb_ratio,
                "peg_ratio": fin.peg_ratio,
                "ev_to_ebitda": fin.ev_to_ebitda,
                "roe_5y_avg": fin.roe_5y_avg,
                "roic_5y_avg": fin.roic_5y_avg,
                "gross_margin_avg": (sum(v for v in fin.gross_margin[:5] if v) / max(1, sum(1 for v in fin.gross_margin[:5] if v))) if fin.gross_margin else None,
                "revenue_cagr_5y": fin.revenue_cagr_5y,
                "eps_cagr_5y": fin.eps_cagr_5y,
                "fcf_cagr_3y": fin.fcf_cagr_3y,
            },
            "data_quality_flags": fin.data_quality_flags,
            "years_of_data": fin.years_of_data,
        }
    except Exception as e:
        print(f"  [ERROR] {ticker}: {e}")
        return None


def main():
    fetcher = DataFetcher()
    normalizer = Normalizer()
    dim_scorer = DimensionScorer()
    master_scorer = MasterScorer()
    veto_engine = VetoEngine()
    funnel = FilterFunnel(fetcher)

    print(f"\n{'='*60}")
    print(f"  投资大师选股系统 — A开头美股打分")
    print(f"  季度: {QUARTER}")
    print(f"{'='*60}\n")

    # Step 1: 直接获取 A 开头的美股列表（stable/search-symbol API）
    print("[Step 1] 搜索 A 开头的美股列表...")
    cache_key = "a_stocks:list"
    a_tickers = fetcher._cache_get(cache_key, 30)
    if a_tickers is None:
        TARGET_EXCHANGES = {"NYSE", "NASDAQ", "AMEX"}
        import time as _time
        a_tickers = {}
        for letter2 in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            data = fetcher._fmp_request("/search-symbol", {"query": f"A{letter2}", "limit": 100})
            if isinstance(data, list):
                for item in data:
                    sym = item.get("symbol", "")
                    exch = item.get("exchange", "")
                    if sym.startswith("A") and sym.isalpha() and len(sym) <= 5 and exch in TARGET_EXCHANGES:
                        a_tickers[sym] = item.get("name", "")
            _time.sleep(0.1)
        # 也搜索单字母 A
        data = fetcher._fmp_request("/search-symbol", {"query": "A", "limit": 50})
        if isinstance(data, list):
            for item in data:
                sym = item.get("symbol", "")
                exch = item.get("exchange", "")
                if sym == "A" and exch in TARGET_EXCHANGES:
                    a_tickers[sym] = item.get("name", "")
        a_tickers = sorted(a_tickers.keys())
        fetcher._cache_set(cache_key, a_tickers, 30)
    print(f"  找到 A 开头的美股: {len(a_tickers)} 只\n")

    # Step 2: 三道过滤漏斗
    print("[Step 2] 三道过滤漏斗...")
    candidate_pool = funnel.run(a_tickers)
    print(f"  过滤后候选: {len(candidate_pool)} 只\n")

    # Step 3: 断点续跑
    remaining = fetcher.get_remaining_tickers(candidate_pool, QUARTER)
    done_count = len(candidate_pool) - len(remaining)
    print(f"[断点] 已完成 {done_count}/{len(candidate_pool)}，剩余 {len(remaining)} 只\n")

    # Step 4: 逐只打分
    print(f"[Step 3] 开始打分（{len(remaining)} 只）...\n")
    new_results = []
    failed = []
    total = len(remaining)

    for i, ticker in enumerate(remaining, 1):
        pct = i / total * 100 if total else 0
        print(f"  [{i:3d}/{total}] {pct:5.1f}%  {ticker}", end="", flush=True)

        result = score_ticker(ticker, fetcher, normalizer, dim_scorer, master_scorer, veto_engine)
        if result:
            path = os.path.join(OUT_DIR, f"{ticker}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2, default=str)
            fetcher.mark_scored(ticker, QUARTER)
            new_results.append(result)
            veto_mark = " [VETO]" if result["veto_triggered"] else ""
            print(f"  ✓ {result['composite_score']:.3f}{veto_mark}")
        else:
            failed.append(ticker)
            print("  ✗ 跳过")

        time.sleep(0.5)  # A股专项，稍减等待

    # 加载所有已有结果
    all_results = []
    for fname in os.listdir(OUT_DIR):
        if fname.endswith(".json") and not fname.startswith("_"):
            ticker = fname[:-5]
            if ticker in candidate_pool:
                try:
                    with open(os.path.join(OUT_DIR, fname), encoding="utf-8") as f:
                        all_results.append(json.load(f))
                except Exception:
                    pass

    # Step 5: 排序，输出Top 10
    valid = [r for r in all_results if not r.get("veto_triggered")]
    veto_list = [r for r in all_results if r.get("veto_triggered")]

    valid.sort(key=lambda r: -r["composite_score"])
    veto_list.sort(key=lambda r: -r["composite_score"])

    print(f"\n{'='*60}")
    print(f"  A开头美股 Top 10（无否决）  共 {len(valid)} 只通过")
    print(f"{'='*60}")
    print(f"  {'排名':<4} {'代码':<8} {'公司名称':<30} {'综合分':>6} {'巴菲特':>7} {'芒格':>7} {'段永平':>7} {'林奇':>7}  行业")
    print(f"  {'─'*4} {'─'*8} {'─'*30} {'─'*6} {'─'*7} {'─'*7} {'─'*7} {'─'*7}  {'─'*20}")

    top10 = valid[:10]
    for rank, r in enumerate(top10, 1):
        ms = r.get("master_scores", {})
        info = r.get("company_info", {})
        print(f"  #{rank:<3} {r['ticker']:<8} {info.get('name','')[:30]:<30} "
              f"{r['composite_score']:>6.3f} "
              f"{ms.get('buffett',0):>7.3f} "
              f"{ms.get('munger',0):>7.3f} "
              f"{ms.get('duan',0):>7.3f} "
              f"{ms.get('lynch',0):>7.3f}  "
              f"{info.get('sector','')[:20]}")

    if veto_list:
        print(f"\n  [注] 另有 {len(veto_list)} 只触发否决项（不参与排名）")

    # 写 CSV 汇总
    csv_path = os.path.join(OUT_DIR, "_a_stocks_summary.csv")
    fieldnames = [
        "rank", "ticker", "name", "sector", "market_cap",
        "composite_score", "score_buffett", "score_munger", "score_duan", "score_lynch",
        "dim_business_quality", "dim_financial_health", "dim_growth", "dim_management", "dim_valuation",
        "veto_triggered", "veto_reasons",
        "pe_ratio", "pb_ratio", "peg_ratio", "ev_to_ebitda", "roe_5y_avg", "roic_5y_avg",
        "gross_margin_avg", "revenue_cagr_5y", "eps_cagr_5y",
        "years_of_data",
    ]
    all_sorted = valid + veto_list
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for rank, r in enumerate(all_sorted, 1):
            info = r.get("company_info", {})
            ms = r.get("master_scores", {})
            dims = r.get("dimensions", {})
            km = r.get("key_metrics", {})
            w.writerow({
                "rank": rank if not r.get("veto_triggered") else "VETO",
                "ticker": r["ticker"],
                "name": info.get("name", ""),
                "sector": info.get("sector", ""),
                "market_cap": info.get("market_cap"),
                "composite_score": r["composite_score"],
                "score_buffett": ms.get("buffett"),
                "score_munger": ms.get("munger"),
                "score_duan": ms.get("duan"),
                "score_lynch": ms.get("lynch"),
                "dim_business_quality": dims.get("business_quality"),
                "dim_financial_health": dims.get("financial_health"),
                "dim_growth": dims.get("growth"),
                "dim_management": dims.get("management"),
                "dim_valuation": dims.get("valuation"),
                "veto_triggered": r.get("veto_triggered"),
                "veto_reasons": "; ".join(r.get("veto_reasons", [])),
                **km,
                "years_of_data": r.get("years_of_data"),
            })

    print(f"\n[输出] 汇总CSV: {csv_path}")
    print(f"[完成] 成功 {len(all_results)} 只，失败/跳过 {len(failed)} 只\n")
    fetcher.close()


if __name__ == "__main__":
    main()
