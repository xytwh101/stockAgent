"""
run_scoring.py — 主入口：全量打分流水线

─── 股票来源（四选一）────────────────────────────────────────────────
  --ticker  AAPL                单只股票
  --tickers AAPL MSFT GOOGL     指定多只
  --file    watchlist.txt       从文件读取（每行一个 ticker）
  --all-cached                  数据库中所有已缓存的 ticker
  （不加任何来源参数）             全量宇宙（拉股票列表 → 三道漏斗 → 打分）

─── 网络模式（二选一）────────────────────────────────────────────────
  --offline     仅用缓存数据打分，不发任何 API 请求
  --offline-db  快捷方式：等价于 --offline --all-cached（只打数据库中已有的股票）
  （默认在线）    缺失数据自动从 FMP 拉取后再打分

─── 其他参数 ─────────────────────────────────────────────────────────
  --quarter 2025-Q4    指定季度（默认当前季度）
  --fresh              忽略断点，强制重跑所有股票
  --limit 100          只打前 N 只（测试用）
  --no-sleep           去掉每只之间的等待（仅离线模式建议使用）

─── 典型用法示例 ─────────────────────────────────────────────────────
  # 离线：只打数据库中已有的所有 ticker（最常用的离线模式）
  python run_scoring.py --offline-db

  # 离线：从缓存打单只
  python run_scoring.py --offline --ticker AAPL

  # 离线：从缓存打一批
  python run_scoring.py --offline --tickers AAPL MSFT NVDA

  # 离线：从缓存打所有已知 ticker
  python run_scoring.py --offline --all-cached

  # 在线：拉取单只数据并打分
  python run_scoring.py --ticker AAPL

  # 在线：拉取一批并打分
  python run_scoring.py --tickers AAPL MSFT NVDA

  # 在线：更新数据库（全量），然后打分
  python run_scoring.py
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import traceback
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.dirname(__file__))

from config import OUTPUT_CONFIG
from src.fetcher import DataFetcher
from src.normalizer import Normalizer
from src.scorer import DimensionScorer, FilterFunnel, MasterScorer, VetoEngine
from src.masters import buffett, munger, duan, lynch


MASTERS = {
    "buffett": buffett,
    "munger": munger,
    "duan": duan,
    "lynch": lynch,
}


# ─────────────────────────────────────────────
# 季度工具函数
# ─────────────────────────────────────────────

def current_quarter() -> str:
    now = datetime.now()
    q = (now.month - 1) // 3 + 1
    return f"{now.year}-Q{q}"


# ─────────────────────────────────────────────
# 输出目录管理
# ─────────────────────────────────────────────

def ensure_output_dir(quarter: str) -> str:
    out_dir = os.path.join(OUTPUT_CONFIG["scores_dir"], quarter)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


# ─────────────────────────────────────────────
# 单只股票打分
# ─────────────────────────────────────────────

def score_ticker(
    ticker: str,
    fetcher: DataFetcher,
    normalizer: Normalizer,
    dim_scorer: DimensionScorer,
    master_scorer: MasterScorer,
    veto_engine: VetoEngine,
    quarter: str,
) -> dict | None:
    """对单只股票完整打分，返回结果 dict"""
    try:
        # 1. 获取数据
        raw = fetcher.get_all_financial_data(ticker)

        # 2. 标准化
        fin = normalizer.normalize(raw)

        if fin.years_of_data < 2:
            return None

        # 3. 全局否决检查
        global_vetoes = veto_engine.check(fin)

        # 4. 五维打分
        dim_scores = dim_scorer.score_all(fin)

        # 5. 大师得分 + 大师特定否决
        master_scores = {}
        master_vetoes = []
        for master_name, master_module in MASTERS.items():
            master_scores[master_name] = master_module.apply_weights(dim_scores)
            master_vetoes.extend(master_module.extra_vetoes(fin))

        all_vetoes = global_vetoes + master_vetoes
        veto_triggered = len(all_vetoes) > 0

        # 6. 综合得分（默认权重加权）
        from config import DIMENSION_WEIGHTS
        composite = sum(
            DIMENSION_WEIGHTS[dim] * dim_scores.get(dim, 0)
            for dim in DIMENSION_WEIGHTS
        )

        return {
            "ticker": ticker,
            "scored_at": datetime.now().isoformat(),
            "quarter": quarter,
            "composite_score": round(composite, 4),
            "veto_triggered": veto_triggered,
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
                "revenue_cagr_5y": fin.revenue_cagr_5y,
                "eps_cagr_5y": fin.eps_cagr_5y,
                "fcf_cagr_3y": fin.fcf_cagr_3y,
                "gross_margin_cv": fin.gross_margin_cv,
            },
            "data_quality_flags": fin.data_quality_flags,
            "years_of_data": fin.years_of_data,
        }

    except Exception as e:
        print(f"  [ERROR] {ticker}: {e}")
        return None


# ─────────────────────────────────────────────
# 保存单只结果到 JSON
# ─────────────────────────────────────────────

def save_ticker_result(result: dict, out_dir: str):
    path = os.path.join(out_dir, f"{result['ticker']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)


# ─────────────────────────────────────────────
# 生成 _summary.csv
# ─────────────────────────────────────────────

def write_summary_csv(results: list[dict], out_dir: str):
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    path = os.path.join(out_dir, f"_summary_{ts}.csv")
    if not results:
        return

    fieldnames = [
        "quarter", "ticker", "name", "sector", "market_cap",
        "composite_score",
        "score_buffett", "score_munger", "score_duan", "score_lynch",
        "dim_business_quality", "dim_financial_health", "dim_growth",
        "dim_management", "dim_valuation",
        "veto_triggered", "veto_reasons",
        "pe_ratio", "pb_ratio", "peg_ratio", "ev_to_ebitda",
        "roe_5y_avg", "revenue_cagr_5y", "eps_cagr_5y",
        "years_of_data", "data_quality_flags",
    ]

    # 按综合分降序排列（否决股排在末尾）
    sorted_results = sorted(
        results,
        key=lambda r: (r.get("veto_triggered", True), -r.get("composite_score", 0))
    )

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in sorted_results:
            row = {
                "quarter": r.get("quarter"),
                "ticker": r.get("ticker"),
                "name": r.get("company_info", {}).get("name", ""),
                "sector": r.get("company_info", {}).get("sector", ""),
                "market_cap": r.get("company_info", {}).get("market_cap"),
                "composite_score": r.get("composite_score"),
                "score_buffett": r.get("master_scores", {}).get("buffett"),
                "score_munger": r.get("master_scores", {}).get("munger"),
                "score_duan": r.get("master_scores", {}).get("duan"),
                "score_lynch": r.get("master_scores", {}).get("lynch"),
                "dim_business_quality": r.get("dimensions", {}).get("business_quality"),
                "dim_financial_health": r.get("dimensions", {}).get("financial_health"),
                "dim_growth": r.get("dimensions", {}).get("growth"),
                "dim_management": r.get("dimensions", {}).get("management"),
                "dim_valuation": r.get("dimensions", {}).get("valuation"),
                "veto_triggered": r.get("veto_triggered"),
                "veto_reasons": "; ".join(r.get("veto_reasons", [])),
                **r.get("key_metrics", {}),
                "years_of_data": r.get("years_of_data"),
                "data_quality_flags": "; ".join(r.get("data_quality_flags", [])),
            }
            writer.writerow(row)

    print(f"[输出] 汇总表已写入: {path}")


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def get_all_cached_tickers(fetcher: DataFetcher) -> list[str]:
    """返回 SQLite 缓存中出现过的所有 ticker（去重、排序）"""
    # 让 SQL 提取 ticker 部分，减少传输量
    rows = fetcher.conn.execute(
        "SELECT DISTINCT substr(cache_key, instr(cache_key, ':') + 1) FROM cache"
    ).fetchall()
    tickers = set()
    for (sym,) in rows:
        sym = sym.upper()
        if sym and sym not in ("ALL", "") and sym.isalpha() and len(sym) <= 5:
            tickers.add(sym)
    return sorted(tickers)


def apply_offline_mode(fetcher: DataFetcher):
    """离线模式：屏蔽所有 FMP API 请求，仅读缓存，并切换为顺序读取（避免多线程锁竞争）"""
    fetcher._fmp_request = lambda *a: None
    fetcher._offline = True


def run_offline_cached(
    quarter: str | None = None,
    fresh: bool = False,
    limit: int | None = None,
    workers: int = 8,
) -> list[dict]:
    """
    只对数据库中已缓存的股票离线打分，不发任何 API 请求。
    使用多线程并行加速（默认 8 workers）。

    参数:
        quarter:  季度标识，如 "2026-Q1"（默认当前季度）
        fresh:    True 时忽略断点，强制重打所有 ticker
        limit:    只打前 N 只（测试/调试用）
        workers:  并发线程数（默认 8，纯离线可调到 16-32）

    返回:
        所有打分成功的结果列表（同时写入 JSON 和汇总 CSV）
    """
    quarter = quarter or current_quarter()
    out_dir = ensure_output_dir(quarter)

    print(f"\n{'='*60}")
    print(f"  离线打分（仅缓存数据）— {quarter}  [workers={workers}]")
    print(f"  输出目录: {out_dir}")
    print(f"{'='*60}\n")

    # 主 fetcher 只用于查询断点和写 checkpoint，不参与并发打分
    main_fetcher = DataFetcher()
    apply_offline_mode(main_fetcher)

    run_start = time.time()

    try:
        candidate_pool = get_all_cached_tickers(main_fetcher)
        print(f"[来源] 数据库已缓存 ticker，共 {len(candidate_pool)} 只")

        if limit:
            candidate_pool = candidate_pool[:limit]
            print(f"[限制] 只打前 {limit} 只\n")

        total = len(candidate_pool)

        if fresh:
            remaining = candidate_pool
            print(f"[断点] fresh 模式，全量重打 {total} 只\n")
        else:
            remaining = main_fetcher.get_remaining_tickers(candidate_pool, quarter)
            done = total - len(remaining)
            print(f"[断点] 已完成 {done}/{total}，剩余 {len(remaining)} 只\n")

        n = len(remaining)
        print(f"[打分] 开始（{n} 只，{workers} 线程并行）...\n")

        all_results: list[dict] = []
        failed: list[str] = []
        counter = [0]          # 进度计数（用列表以便闭包修改）
        print_lock = threading.Lock()
        checkpoint_lock = threading.Lock()

        # 每个线程独立的 fetcher/scorer 实例，避免锁竞争
        _thread_local = threading.local()

        def get_thread_components():
            if not hasattr(_thread_local, "fetcher"):
                f = DataFetcher()
                apply_offline_mode(f)
                _thread_local.fetcher = f
                _thread_local.normalizer = Normalizer()
                _thread_local.dim_scorer = DimensionScorer()
                _thread_local.master_scorer = MasterScorer()
                _thread_local.veto_engine = VetoEngine()
            return (
                _thread_local.fetcher,
                _thread_local.normalizer,
                _thread_local.dim_scorer,
                _thread_local.master_scorer,
                _thread_local.veto_engine,
            )

        def score_one(ticker: str) -> tuple[str, dict | None]:
            f, norm, dim_s, mstr_s, veto = get_thread_components()
            result = score_ticker(ticker, f, norm, dim_s, mstr_s, veto, quarter)
            return ticker, result

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(score_one, t): t for t in remaining}
            for future in as_completed(futures):
                ticker, result = future.result()
                with print_lock:
                    counter[0] += 1
                    pct = counter[0] / n * 100
                    if result:
                        save_ticker_result(result, out_dir)
                        all_results.append(result)
                        veto_mark = " [VETO]" if result.get("veto_triggered") else ""
                        print(f"  [{counter[0]:4d}/{n}] {pct:5.1f}%  {ticker}"
                              f"  ✓ {result['composite_score']:.3f}{veto_mark}")
                    else:
                        failed.append(ticker)
                        print(f"  [{counter[0]:4d}/{n}] {pct:5.1f}%  {ticker}  ✗ 跳过")

                if result:
                    with checkpoint_lock:
                        main_fetcher.mark_scored(ticker, quarter)

        # 加载已有结果，生成完整汇总表
        existing_results: list[dict] = []
        for fname in os.listdir(out_dir):
            if fname.endswith(".json") and not fname.startswith("_"):
                try:
                    with open(os.path.join(out_dir, fname), encoding="utf-8") as f:
                        existing_results.append(json.load(f))
                except Exception:
                    pass

        write_summary_csv(existing_results, out_dir)

        elapsed = time.time() - run_start
        speed = n / elapsed if elapsed > 0 else 0
        print(f"\n{'='*60}")
        print(f"  完成！成功 {len(all_results)} 只，失败 {len(failed)} 只")
        print(f"  耗时: {elapsed/60:.1f} 分钟  ({speed:.1f} 只/秒)")
        print(f"{'='*60}\n")

        return all_results

    finally:
        main_fetcher.close()


def main():
    parser = argparse.ArgumentParser(
        description="投资大师选股系统 — 全量打分",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── 股票来源（四选一）
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--ticker",     metavar="SYM",  help="单只股票")
    source.add_argument("--tickers",    nargs="+",       help="指定多只股票")
    source.add_argument("--file",       metavar="PATH",  help="从文件读取 ticker 列表（每行一个）")
    source.add_argument("--all-cached", action="store_true",
                        help="数据库中所有已缓存的 ticker")

    # ── 网络模式
    parser.add_argument("--offline", action="store_true",
                        help="离线模式：只读缓存，不发任何 API 请求")
    parser.add_argument("--offline-db", action="store_true",
                        help="快捷方式：等价于 --offline --all-cached（只打数据库中已缓存的 ticker）")

    # ── 其他
    parser.add_argument("--quarter",  default=None,       help="季度标识，如 2025-Q4（默认当前季度）")
    parser.add_argument("--fresh",    action="store_true", help="忽略断点，强制重跑所有股票")
    parser.add_argument("--limit",    type=int, default=None, help="只打前 N 只（测试用）")
    parser.add_argument("--no-sleep", action="store_true", help="去掉每只之间的等待（离线模式推荐）")
    parser.add_argument("--workers",  type=int, default=8,
                        help="离线并行线程数（仅 --offline-db 生效，默认 8，纯离线可调到 16-32）")
    args = parser.parse_args()

    # --offline-db 是 --offline --all-cached 的快捷方式
    if args.offline_db:
        run_offline_cached(
            quarter=args.quarter,
            fresh=args.fresh,
            limit=args.limit,
            workers=args.workers,
        )
        return

    quarter = args.quarter or current_quarter()
    out_dir = ensure_output_dir(quarter)
    log_path = os.path.join(out_dir, OUTPUT_CONFIG["run_log_filename"])

    net_tag = "离线(缓存)" if args.offline else "在线"
    print(f"\n{'='*60}")
    print(f"  投资大师选股系统 — {quarter}  [{net_tag}]")
    print(f"  输出目录: {out_dir}")
    print(f"{'='*60}\n")

    # 初始化组件
    fetcher = DataFetcher()
    if args.offline:
        apply_offline_mode(fetcher)

    normalizer = Normalizer()
    dim_scorer = DimensionScorer()
    master_scorer = MasterScorer()
    veto_engine = VetoEngine()
    funnel = FilterFunnel(fetcher)

    run_start = time.time()

    try:
        # ── 确定股票池 ────────────────────────────────
        if args.ticker:
            candidate_pool = [args.ticker.upper()]
            print(f"[来源] 单只股票: {candidate_pool[0]}")

        elif args.tickers:
            candidate_pool = [t.upper() for t in args.tickers]
            print(f"[来源] 指定 {len(candidate_pool)} 只: {candidate_pool[:5]}{'...' if len(candidate_pool)>5 else ''}")

        elif args.file:
            path = os.path.abspath(args.file)
            if not os.path.exists(path):
                print(f"文件不存在: {path}")
                return
            with open(path, encoding="utf-8") as f:
                candidate_pool = [line.strip().upper() for line in f if line.strip()]
            print(f"[来源] 文件 {args.file}，读取 {len(candidate_pool)} 只")

        elif args.all_cached:
            candidate_pool = get_all_cached_tickers(fetcher)
            print(f"[来源] 数据库已缓存 ticker，共 {len(candidate_pool)} 只")

        else:
            # 全量宇宙模式
            print("[Step 1] 获取全量股票列表...")
            all_tickers = fetcher.get_ticker_universe()
            print(f"  原始列表: {len(all_tickers)} 只\n")
            print("[Step 2] 执行三道过滤漏斗...")
            candidate_pool = funnel.run(all_tickers)

        if args.limit:
            candidate_pool = candidate_pool[: args.limit]
            print(f"[限制] 只打前 {args.limit} 只\n")

        total = len(candidate_pool)

        # ── 断点续跑 ──────────────────────────────────
        if args.fresh:
            remaining = candidate_pool
            print(f"[断点] fresh 模式，全量重打 {total} 只\n")
        else:
            remaining = fetcher.get_remaining_tickers(candidate_pool, quarter)
            done = total - len(remaining)
            print(f"[断点] 已完成 {done}/{total}，剩余 {len(remaining)} 只\n")

        # ── 逐只打分 ──────────────────────────────────
        print(f"[Step 3] 开始打分（{len(remaining)} 只）...\n")
        all_results = []
        failed = []

        for i, ticker in enumerate(remaining, 1):
            pct = (i / len(remaining)) * 100
            print(f"  [{i:4d}/{len(remaining)}] {pct:5.1f}%  {ticker}", end="", flush=True)

            result = score_ticker(
                ticker, fetcher, normalizer, dim_scorer,
                master_scorer, veto_engine, quarter
            )

            if result:
                save_ticker_result(result, out_dir)
                fetcher.mark_scored(ticker, quarter)
                all_results.append(result)
                veto_mark = " [VETO]" if result.get("veto_triggered") else ""
                print(f"  ✓ {result['composite_score']:.3f}{veto_mark}")
            else:
                failed.append(ticker)
                print(f"  ✗ 跳过")

            if not args.no_sleep:
                time.sleep(OUTPUT_CONFIG["sleep_between_tickers"])

        # ── 加载已有结果（用于生成完整汇总表）──────────
        existing_results = []
        for fname in os.listdir(out_dir):
            if fname.endswith(".json") and not fname.startswith("_"):
                try:
                    with open(os.path.join(out_dir, fname), encoding="utf-8") as f:
                        existing_results.append(json.load(f))
                except Exception:
                    pass

        # ── 生成汇总表 ────────────────────────────────
        write_summary_csv(existing_results, out_dir)

        # ── 运行日志 ──────────────────────────────────
        elapsed = time.time() - run_start
        progress = fetcher.progress_report(total, quarter)
        log_lines = [
            f"运行时间: {datetime.now().isoformat()}",
            f"季度: {quarter}",
            f"候选池: {total} 只",
            f"本次打分: {len(remaining)} 只",
            f"成功: {len(all_results)} 只",
            f"失败/跳过: {len(failed)} 只",
            f"累计完成: {progress['done']}/{progress['total']}",
            f"耗时: {elapsed:.0f}s ({elapsed/60:.1f}min)",
            f"失败列表: {failed[:20]}",
        ]
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(log_lines) + "\n\n")

        print(f"\n{'='*60}")
        print(f"  完成！成功 {len(all_results)} 只，失败 {len(failed)} 只")
        print(f"  耗时: {elapsed/60:.1f} 分钟")
        print(f"{'='*60}\n")

    finally:
        fetcher.close()


if __name__ == "__main__":
    main()
