"""
fetch_data.py — 专项数据拉取脚本（纯拉取，不打分）

拉取模式（四选一）：
  --ticker  AAPL                   单只股票
  --tickers AAPL MSFT GOOGL        指定多只
  --file    tickers.txt            从文件读取（每行一个 ticker）
  --cached                         补全已缓存 ticker 中的缺失/过期数据
  --all                            全量拉取（建议升级无限套餐后使用）

可选参数：
  --mode  core       只拉打分必需数据（8 个端点，约 8 次 API 调用/只）[默认]
          full       拉全量数据（19 个端点）
  --dry-run          预估调用量，不实际拉取
  --sleep  N         每只股票之间间隔 N 秒（默认 0.3，限速保护）

示例：
  python fetch_data.py --ticker AAPL
  python fetch_data.py --tickers AAPL MSFT NVDA --mode full
  python fetch_data.py --file my_watchlist.txt
  python fetch_data.py --cached --mode core
  python fetch_data.py --all --dry-run
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from src.fetcher import DataFetcher
from config import CACHE_CONFIG

DB_PATH = Path(CACHE_CONFIG["db_path"])


# ─────────────────────────────────────────────
# 端点分层定义
# ─────────────────────────────────────────────

# 打分必需（8 个端点）
CORE_ENDPOINTS: list[tuple[str, str, dict]] = [
    ("profile",           "get_profile",             {}),
    ("income_annual",     "get_income_statement",    {"period": "annual",  "limit": 20}),
    ("balance_annual",    "get_balance_sheet",       {"period": "annual",  "limit": 20}),
    ("cashflow_annual",   "get_cash_flow_statement", {"period": "annual",  "limit": 20}),
    ("key_metrics_annual","get_key_metrics",         {"period": "annual",  "limit": 20}),
    ("ratios_annual",     "get_financial_ratios",    {"period": "annual",  "limit": 20}),
    ("ratios_ttm",        "get_ratios_ttm",          {}),
    ("key_metrics_ttm",   "get_key_metrics_ttm",     {}),
]

# 补充数据（11 个端点）
EXTENDED_ENDPOINTS: list[tuple[str, str, dict]] = [
    ("income_quarter",    "get_income_statement",    {"period": "quarter", "limit": 20}),
    ("balance_quarter",   "get_balance_sheet",       {"period": "quarter", "limit": 20}),
    ("cashflow_quarter",  "get_cash_flow_statement", {"period": "quarter", "limit": 20}),
    ("income_growth",     "get_income_growth",       {"limit": 20}),
    ("ev_annual",         "get_enterprise_values",   {"period": "annual",  "limit": 20}),
    ("dividend",          "get_dividend_history",    {}),
    ("earnings",          "get_earnings_history",    {"limit": 40}),
    ("price_daily",       "get_price_history_daily", {"years": 20}),
    ("insider",           "get_insider_trading",     {"limit": 100}),
    ("analyst",           "get_analyst_estimates",   {"limit": 8}),
    ("cashflow_growth",   "get_cashflow_growth",     {"limit": 20}),
]

FULL_ENDPOINTS = CORE_ENDPOINTS + EXTENDED_ENDPOINTS


# ─────────────────────────────────────────────
# 缓存检查工具（不发网络请求）
# ─────────────────────────────────────────────

def is_cached_and_valid(conn: sqlite3.Connection, cache_key: str, ttl_days: int) -> bool:
    now = int(time.time())
    row = conn.execute(
        "SELECT fetched_at, ttl_days FROM cache WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    if not row:
        return False
    fetched_at, stored_ttl = row
    return (now - fetched_at) <= min(ttl_days, stored_ttl) * 86400


def get_cache_key(cache_key_prefix: str, ticker: str, kwargs: dict) -> str:
    """根据端点和参数推断 cache_key"""
    period = kwargs.get("period", "")
    if period:
        return f"{cache_key_prefix.replace('_annual','').replace('_quarter','').replace('_ttm','')}_{period}:{ticker}"
    return f"{cache_key_prefix}:{ticker}"


# 各端点对应的 cache_key 前缀和 TTL
_TTL_MAP = {
    "profile":            CACHE_CONFIG["universe_ttl_days"],
    "income_annual":      CACHE_CONFIG["financial_data_ttl_days"],
    "balance_annual":     CACHE_CONFIG["financial_data_ttl_days"],
    "cashflow_annual":    CACHE_CONFIG["financial_data_ttl_days"],
    "key_metrics_annual": CACHE_CONFIG["financial_data_ttl_days"],
    "ratios_annual":      CACHE_CONFIG["financial_data_ttl_days"],
    "ratios_ttm":         CACHE_CONFIG["valuation_data_ttl_days"],
    "key_metrics_ttm":    CACHE_CONFIG["valuation_data_ttl_days"],
    "income_quarter":     CACHE_CONFIG["financial_data_ttl_days"],
    "balance_quarter":    CACHE_CONFIG["financial_data_ttl_days"],
    "cashflow_quarter":   CACHE_CONFIG["financial_data_ttl_days"],
    "income_growth":      CACHE_CONFIG["financial_data_ttl_days"],
    "ev_annual":          CACHE_CONFIG["financial_data_ttl_days"],
    "dividend":           CACHE_CONFIG["financial_data_ttl_days"],
    "earnings":           CACHE_CONFIG["financial_data_ttl_days"],
    "price_daily":        CACHE_CONFIG["valuation_data_ttl_days"],
    "insider":            CACHE_CONFIG["universe_ttl_days"],
    "analyst":            CACHE_CONFIG["valuation_data_ttl_days"],
    "cashflow_growth":    CACHE_CONFIG["financial_data_ttl_days"],
}

# 端点 → 实际存储的 cache_key 前缀（和 fetcher.py 保持一致）
_CACHE_KEY_PREFIX = {
    "profile":            "profile",
    "income_annual":      "income_annual",
    "balance_annual":     "balance_annual",
    "cashflow_annual":    "cashflow_annual",
    "key_metrics_annual": "key_metrics_annual",
    "ratios_annual":      "ratios_annual",
    "ratios_ttm":         "ratios_ttm",
    "key_metrics_ttm":    "key_metrics_ttm",
    "income_quarter":     "income_quarter",
    "balance_quarter":    "balance_quarter",
    "cashflow_quarter":   "cashflow_quarter",
    "income_growth":      "income_growth",
    "ev_annual":          "ev_annual",
    "dividend":           "dividend",
    "earnings":           "earnings",
    "price_daily":        "price_daily",
    "insider":            "insider",
    "analyst":            "analyst",
    "cashflow_growth":    "cashflow_growth",
}


def missing_endpoints(conn: sqlite3.Connection, ticker: str, endpoints: list) -> list:
    """返回该 ticker 中尚未缓存或已过期的端点列表"""
    missing = []
    for ep_name, method_name, kwargs in endpoints:
        ck = f"{_CACHE_KEY_PREFIX[ep_name]}:{ticker}"
        ttl = _TTL_MAP[ep_name]
        if not is_cached_and_valid(conn, ck, ttl):
            missing.append((ep_name, method_name, kwargs))
    return missing


# ─────────────────────────────────────────────
# API 调用计数器（包装 fetcher）
# ─────────────────────────────────────────────

class CountingFetcher:
    """包装 DataFetcher，统计实际 API 调用次数（缓存命中不计）"""

    def __init__(self):
        self._fetcher = DataFetcher()
        self.api_calls = 0
        self.cache_hits = 0

        # 拦截 _fmp_request，在发出真实请求时计数
        original_fmp = self._fetcher._fmp_request

        def counting_fmp(endpoint, params=None):
            self.api_calls += 1
            return original_fmp(endpoint, params)

        self._fetcher._fmp_request = counting_fmp

    def fetch_endpoint(self, ticker: str, method_name: str, kwargs: dict):
        method = getattr(self._fetcher, method_name)
        if kwargs:
            return method(ticker, **kwargs)
        return method(ticker)

    def close(self):
        self._fetcher.close()

    @property
    def conn(self):
        return self._fetcher.conn

    def get_ticker_universe(self):
        return self._fetcher.get_ticker_universe()

    def get_all_cached_tickers(self) -> list[str]:
        """返回数据库中所有已出现过的 ticker（不论是否过期）"""
        rows = self.conn.execute("SELECT DISTINCT cache_key FROM cache").fetchall()
        tickers = set()
        for (key,) in rows:
            parts = key.split(":")
            if len(parts) >= 2 and parts[1] not in ("", "all"):
                sym = parts[1].upper()
                if sym.isalpha() and len(sym) <= 5:
                    tickers.add(sym)
        return sorted(tickers)


# ─────────────────────────────────────────────
# 拉取单只股票（仅补缺失）
# ─────────────────────────────────────────────

def fetch_ticker(
    ticker: str,
    fetcher: CountingFetcher,
    endpoints: list,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    返回 (api_calls_made, endpoints_skipped)
    """
    sym = ticker.upper()
    missing = missing_endpoints(fetcher.conn, sym, endpoints)
    skipped = len(endpoints) - len(missing)

    if not missing:
        return 0, skipped

    if dry_run:
        return len(missing), skipped

    before = fetcher.api_calls

    def _fetch_one_ep(item):
        ep_name, method_name, kwargs = item
        try:
            fetcher.fetch_endpoint(sym, method_name, kwargs)
        except Exception as e:
            print(f"    [错误] {ep_name}: {e}")

    # 单只 ticker 内部也并发拉取所有缺失端点
    with ThreadPoolExecutor(max_workers=len(missing)) as pool:
        list(pool.map(_fetch_one_ep, missing))

    made = fetcher.api_calls - before
    return made, skipped


# ─────────────────────────────────────────────
# 获取 ticker 列表
# ─────────────────────────────────────────────

def resolve_tickers(args, fetcher: CountingFetcher) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]

    if args.tickers:
        return [t.upper() for t in args.tickers]

    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"文件不存在: {path}")
            sys.exit(1)
        tickers = [line.strip().upper() for line in path.read_text().splitlines() if line.strip()]
        print(f"从文件读取 {len(tickers)} 只: {path}")
        return tickers

    if args.cached:
        tickers = fetcher.get_all_cached_tickers()
        print(f"从缓存读取 {len(tickers)} 只已知 ticker")
        return tickers

    if args.all:
        print("[全量模式] 拉取股票列表...")
        tickers = fetcher.get_ticker_universe()
        print(f"  共 {len(tickers)} 只")
        return tickers

    print("请指定拉取模式，用 -h 查看帮助")
    sys.exit(1)


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="投资大师选股系统 — 数据拉取",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python fetch_data.py --ticker AAPL
  python fetch_data.py --tickers AAPL MSFT NVDA GOOGL
  python fetch_data.py --file watchlist.txt
  python fetch_data.py --cached --mode core
  python fetch_data.py --all --dry-run
"""
    )

    # 拉取目标（四选一）
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker",  metavar="SYM",      help="单只股票")
    group.add_argument("--tickers", nargs="+",           help="指定多只股票")
    group.add_argument("--file",    metavar="PATH",      help="从文件读取 ticker 列表（每行一个）")
    group.add_argument("--cached",  action="store_true", help="补全已缓存 ticker 的缺失/过期数据")
    group.add_argument("--all",     action="store_true", help="全量拉取（建议无限套餐）")

    # 可选参数
    parser.add_argument("--mode",    choices=["core", "full"], default="core",
                        help="core=打分必需8端点（默认）  full=全量19端点")
    parser.add_argument("--dry-run", action="store_true", help="只估算调用量，不实际拉取")
    parser.add_argument("--sleep",   type=float, default=0.1, help="每只股票间隔秒数（默认0.1）")
    parser.add_argument("--workers", type=int, default=8, help="并发拉取股票数（默认8）")
    args = parser.parse_args()

    endpoints = CORE_ENDPOINTS if args.mode == "core" else FULL_ENDPOINTS
    ep_per_ticker = len(endpoints)

    print(f"\n{'='*60}")
    print(f"  数据拉取  模式={args.mode}  端点/只={ep_per_ticker}")
    if args.dry_run:
        print(f"  *** DRY-RUN 模式，不发送网络请求 ***")
    print(f"{'='*60}\n")

    fetcher = CountingFetcher()

    try:
        tickers = resolve_tickers(args, fetcher)
        total = len(tickers)

        if not tickers:
            print("没有需要处理的股票")
            return

        # 预估
        with sqlite3.connect(DB_PATH) as conn:
            total_missing = sum(len(missing_endpoints(conn, t, endpoints)) for t in tickers)
        cached_count = total * ep_per_ticker - total_missing
        print(f"  股票数:   {total}")
        print(f"  端点总数: {total * ep_per_ticker}")
        print(f"  已缓存:   {cached_count}  ({cached_count/(total*ep_per_ticker)*100:.0f}%)")
        print(f"  待拉取:   {total_missing} 次 API 调用\n")

        if args.dry_run:
            print("DRY-RUN 完成，未发送任何请求。")
            # 按缺口大小排序，列出前20
            with sqlite3.connect(DB_PATH) as conn:
                needs = [(t, len(missing_endpoints(conn, t, endpoints))) for t in tickers]
            needs = [(t, n) for t, n in needs if n > 0]
            needs.sort(key=lambda x: -x[1])
            if needs:
                print(f"\n需要最多调用的股票（Top 20）：")
                for t, n in needs[:20]:
                    print(f"  {t:<8} 缺 {n}/{ep_per_ticker} 个端点")
            return

        if total_missing == 0:
            print("所有数据均已缓存，无需拉取。")
            return

        # 正式拉取
        run_start = time.time()
        total_api = 0
        total_skip = 0
        workers = args.workers
        completed_count = 0

        import threading
        print_lock = threading.Lock()

        if workers <= 1:
            # 单线程模式（兼容旧行为）
            for i, ticker in enumerate(tickers, 1):
                pct = i / total * 100
                print(f"  [{i:4d}/{total}] {pct:5.1f}%  {ticker:<8}", end="", flush=True)
                made, skipped = fetch_ticker(ticker, fetcher, endpoints, dry_run=False)
                total_api += made
                total_skip += skipped
                if made > 0:
                    print(f"  +{made} API  (缓存命中 {skipped})")
                else:
                    print(f"  全部命中缓存")
                if i < total and made > 0:
                    time.sleep(args.sleep)
        else:
            # 多线程并发拉取
            print(f"  并发线程数: {workers}\n")

            def _fetch_one(ticker_idx_tuple):
                idx, ticker = ticker_idx_tuple
                made, skipped = fetch_ticker(ticker, fetcher, endpoints, dry_run=False)
                if args.sleep > 0 and made > 0:
                    time.sleep(args.sleep)
                return idx, ticker, made, skipped

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_fetch_one, (i, t)): t
                    for i, t in enumerate(tickers, 1)
                }
                for future in as_completed(futures):
                    idx, ticker, made, skipped = future.result()
                    total_api += made
                    total_skip += skipped
                    completed_count += 1
                    pct = completed_count / total * 100
                    with print_lock:
                        if made > 0:
                            print(f"  [{completed_count:4d}/{total}] {pct:5.1f}%  {ticker:<8}  +{made} API  (缓存命中 {skipped})")
                        else:
                            print(f"  [{completed_count:4d}/{total}] {pct:5.1f}%  {ticker:<8}  全部命中缓存")

        elapsed = time.time() - run_start
        print(f"\n{'='*60}")
        print(f"  完成！股票数: {total}  API调用: {total_api}  缓存命中: {total_skip}")
        print(f"  耗时: {elapsed:.0f}s ({elapsed/60:.1f}min)")
        print(f"{'='*60}\n")

    finally:
        fetcher.close()


if __name__ == "__main__":
    main()
