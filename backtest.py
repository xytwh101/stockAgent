"""
回测验证脚本：打分准确度评估
=================================
用某季度的历史打分，衡量其对未来涨幅的预测能力。

核心逻辑：
  预测排名 —— 由打分（composite_score / 各大师分）决定
  实际排名 —— 从打分季度末到 N 年后的股价涨幅
  评估指标 —— IC（Spearman 秩相关）、分位组收益、命中率

用法示例：
  python backtest.py --quarter 2023-Q1 --forward-years 3
  python backtest.py --quarter 2023-Q1 --forward-years 1 --score-key buffett
  python backtest.py --quarter 2022-Q4 --forward-years 2 --top-n 100
  python backtest.py --list
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from bisect import bisect_left
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# ── 可选依赖（scipy/pandas），未安装时降级到纯 Python 实现 ──────────────
try:
    from scipy.stats import spearmanr
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

# ── 项目内部模块 ───────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from src.fetcher import DataFetcher

SCORES_DIR = Path("scores")
VALID_SCORE_KEYS = [
    "composite_score", "buffett", "munger", "duan", "lynch"
]


# ══════════════════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════════════════

def quarter_end_date(quarter: str) -> date:
    """'2023-Q1' → date(2023, 3, 31)"""
    year_str, q_str = quarter.split("-")
    year = int(year_str)
    q = int(q_str[1])
    quarter_ends = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    m, d = quarter_ends[q]
    return date(year, m, d)


def list_scored_quarters() -> list[str]:
    """列出 scores/ 下所有有效季度目录"""
    if not SCORES_DIR.exists():
        return []
    quarters = []
    for d in sorted(SCORES_DIR.iterdir()):
        if d.is_dir() and not d.name.startswith("_"):
            json_files = list(d.glob("*.json"))
            # 排除 _开头的文件
            json_files = [f for f in json_files if not f.name.startswith("_")]
            if json_files:
                quarters.append(d.name)
    return quarters


def load_quarter_scores(
    quarter: str, score_key: str,
    top_n: Optional[int] = None,
    include_veto: bool = False,
) -> list[dict]:
    """
    加载某季度所有已打分股票。
    返回按 score_key 降序排列的列表，每项包含:
      ticker, scored_at, composite_score, master_scores, veto_triggered
    include_veto=True 时也包含触发否决的股票（用于验证否决规则的有效性）。
    """
    q_dir = SCORES_DIR / quarter
    if not q_dir.exists():
        sys.exit(f"[错误] 未找到打分目录: {q_dir}\n可用季度: {list_scored_quarters()}")

    records = []
    for fp in q_dir.glob("*.json"):
        if fp.name.startswith("_"):
            continue
        try:
            with fp.open(encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        # 过滤触发否决的股票（排名无意义），除非 include_veto=True
        if data.get("veto_triggered") and not include_veto:
            continue

        # 取分
        if score_key == "composite_score":
            score = data.get("composite_score")
        else:
            score = data.get("master_scores", {}).get(score_key)

        if score is None:
            continue

        records.append({
            "ticker": data["ticker"],
            "scored_at": data.get("scored_at", ""),
            "score": score,
            "composite_score": data.get("composite_score"),
            "master_scores": data.get("master_scores", {}),
            "veto_triggered": data.get("veto_triggered", False),
            "sector": data.get("company_info", {}).get("sector", ""),
            "market_cap": data.get("company_info", {}).get("market_cap"),
        })

    if not records:
        sys.exit(f"[错误] {quarter} 无有效打分数据（全被否决或为空）")

    records.sort(key=lambda x: x["score"], reverse=True)
    if top_n:
        records = records[:top_n]
    return records


def build_price_index(price_history: list[dict]) -> tuple[list[str], list[float]]:
    """
    将价格序列转换为有序的 (dates, prices) 列表，便于二分查找。
    price_history 格式：[{"date": "2023-01-03", "price": 125.5, "volume": ...}, ...]
    """
    pairs = sorted(
        ((r["date"], r.get("price") or r.get("close") or r.get("adjClose", 0))
         for r in price_history if r.get("date")),
        key=lambda x: x[0]
    )
    if not pairs:
        return [], []
    dates, prices = zip(*pairs)
    return list(dates), list(prices)


def find_price_at(dates: list[str], prices: list[float], target: date,
                  tolerance_days: int = 10) -> Optional[float]:
    """
    在有序价格序列中，找最近交易日的价格（±10个交易日内）。
    使用二分查找，优先找 target 当日或之后最近的交易日。
    """
    target_str = target.isoformat()
    idx = bisect_left(dates, target_str)

    best_price = None
    best_diff = float("inf")

    for i in [idx, idx - 1]:
        if 0 <= i < len(dates):
            d = date.fromisoformat(dates[i])
            diff = abs((d - target).days)
            if diff <= tolerance_days and diff < best_diff:
                best_diff = diff
                best_price = prices[i]
    return best_price


def compute_forward_return(
    ticker: str,
    start: date,
    end: date,
    fetcher: DataFetcher,
) -> Optional[float]:
    """
    计算 start → end 期间股价涨幅（已复权）。
    返回 None 表示数据不足或无法获取。
    """
    try:
        history = fetcher.get_price_history_daily(ticker, years=20)
    except (ValueError, RuntimeError):
        # API key 未设置或网络错误
        return None

    if not history:
        return None

    dates, prices = build_price_index(history)
    if not dates:
        return None

    p_start = find_price_at(dates, prices, start)
    p_end = find_price_at(dates, prices, end)

    if p_start and p_end and p_start > 0:
        return (p_end - p_start) / p_start
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 统计计算
# ══════════════════════════════════════════════════════════════════════════════

def spearman_correlation(x: list[float], y: list[float]) -> float:
    """计算 Spearman 秩相关系数（不依赖 scipy 的纯 Python 实现）"""
    if HAS_SCIPY:
        corr, _ = spearmanr(x, y)
        return float(corr)

    n = len(x)
    if n < 2:
        return float("nan")

    def rank_list(lst):
        sorted_idx = sorted(range(n), key=lambda i: lst[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n and lst[sorted_idx[j]] == lst[sorted_idx[i]]:
                j += 1
            avg_rank = (i + j - 1) / 2.0
            for k in range(i, j):
                ranks[sorted_idx[k]] = avg_rank
            i = j
        return ranks

    rx = rank_list(x)
    ry = rank_list(y)
    d2 = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    return 1 - 6 * d2 / (n * (n ** 2 - 1))


def quintile_analysis(scores: list[float], returns: list[float]) -> dict:
    """
    将股票按打分分成5组（五分位），计算各组的平均涨幅。
    返回：{Q1: avg_return, Q2: ..., Q5: ..., spread: Q1-Q5}
    """
    n = len(scores)
    if n < 5:
        return {}

    # 按打分降序排列，分成5组
    pairs = sorted(zip(scores, returns), key=lambda x: x[0], reverse=True)
    group_size = n // 5
    result = {}
    for q in range(1, 6):
        start_idx = (q - 1) * group_size
        end_idx = n if q == 5 else q * group_size
        group_returns = [r for _, r in pairs[start_idx:end_idx]]
        result[f"Q{q}"] = sum(group_returns) / len(group_returns)

    result["spread_Q1_Q5"] = result.get("Q1", 0) - result.get("Q5", 0)
    return result


def hit_rate(scores: list[float], returns: list[float]) -> float:
    """
    命中率：对所有股票对 (i, j)，若 score_i > score_j 且 return_i > return_j，
    或 score_i < score_j 且 return_i < return_j，则算命中。
    """
    n = len(scores)
    if n < 2:
        return float("nan")
    hits = 0
    total = 0
    for i in range(n):
        for j in range(i + 1, n):
            if scores[i] == scores[j] or returns[i] == returns[j]:
                continue
            total += 1
            if (scores[i] > scores[j]) == (returns[i] > returns[j]):
                hits += 1
    return hits / total if total else float("nan")


# ══════════════════════════════════════════════════════════════════════════════
# 报告输出
# ══════════════════════════════════════════════════════════════════════════════

def save_report(quarter: str, forward_years: int, score_key: str,
                rows: list[dict], metrics: dict, output_dir: Path):
    """保存 CSV + 文本报告"""
    output_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{quarter}_{score_key}_{forward_years}y"

    # ── CSV ──────────────────────────────────────────────────────────────────
    csv_path = output_dir / f"backtest_{tag}.csv"
    header = [
        "predicted_rank", "ticker", "sector", "score",
        "forward_return_pct", "actual_rank", "return_rank_diff"
    ]
    valid_rows = [r for r in rows if r.get("forward_return") is not None]
    valid_rows.sort(key=lambda r: r["predicted_rank"])

    with csv_path.open("w", encoding="utf-8") as f:
        f.write(",".join(header) + "\n")
        for r in valid_rows:
            ret_pct = f"{r['forward_return'] * 100:.2f}"
            diff = r["predicted_rank"] - r["actual_rank"]
            f.write(",".join([
                str(r["predicted_rank"]),
                r["ticker"],
                r.get("sector", ""),
                f"{r['score']:.4f}",
                ret_pct,
                str(r["actual_rank"]),
                str(diff),
            ]) + "\n")

    # ── 文本报告 ─────────────────────────────────────────────────────────────
    rpt_path = output_dir / f"backtest_{tag}_report.txt"
    with rpt_path.open("w", encoding="utf-8") as f:
        def w(line=""):
            f.write(line + "\n")

        w("=" * 60)
        w(f"  打分回测报告")
        w(f"  季度: {quarter}  |  前瞻期: {forward_years} 年  |  排名依据: {score_key}")
        w("=" * 60)
        w()
        w(f"  有效样本量:   {metrics['n_valid']} / {metrics['n_total']}")
        w(f"  数据覆盖率:   {metrics['coverage_rate'] * 100:.1f}%")
        w()
        w("── 核心指标 ──────────────────────────────────────────────")
        w(f"  IC (Spearman):  {metrics['ic']:.4f}    "
          f"{'★ 有效预测' if metrics['ic'] > 0.03 else '（较弱）'}")
        w(f"  命中率:         {metrics['hit_rate'] * 100:.1f}%    "
          f"{'★ 优于随机' if metrics['hit_rate'] > 0.5 else '（低于随机）'}")
        w()
        w("── 五分位组平均收益 ──────────────────────────────────────")
        w("  （Q1=高分组，Q5=低分组）")
        for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
            v = metrics["quintiles"].get(q)
            if v is not None:
                bar = "█" * max(0, int(v * 100 / 5))
                w(f"  {q}: {v * 100:+7.1f}%  {bar}")
        spread = metrics["quintiles"].get("spread_Q1_Q5", 0)
        w(f"  高低分位差:  {spread * 100:+.1f}%")
        w()
        w("── Top 20 预测高分股 ──────────────────────────────────────")
        w(f"  {'排名':>4}  {'Ticker':>6}  {'打分':>6}  {'实际涨幅':>10}  {'实际排名':>6}")
        w(f"  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*10}  {'-'*6}")
        for r in valid_rows[:20]:
            ret = r.get("forward_return")
            ret_str = f"{ret * 100:+.1f}%" if ret is not None else "N/A"
            w(f"  {r['predicted_rank']:>4}  {r['ticker']:>6}  "
              f"{r['score']:>6.2f}  {ret_str:>10}  {r['actual_rank']:>6}")
        w()
        w(f"  详细数据见: {csv_path}")
        w("=" * 60)

    return rpt_path, csv_path


def print_summary(quarter: str, forward_years: int, score_key: str, metrics: dict):
    """在控制台打印简洁摘要"""
    sep = "─" * 55
    print()
    print("=" * 55)
    print(f"  📊 打分回测结果")
    print(f"  季度: {quarter} | 前瞻: {forward_years}年 | 依据: {score_key}")
    print("=" * 55)
    print(f"  有效样本: {metrics['n_valid']} / {metrics['n_total']}"
          f"  (覆盖率 {metrics['coverage_rate'] * 100:.0f}%)")
    print(sep)
    ic = metrics["ic"]
    hr = metrics["hit_rate"]
    ic_tag = "★ 有效" if ic > 0.03 else "（弱）" if ic > 0 else "（负相关）"
    hr_tag = "★ 优于随机" if hr > 0.5 else "（低于随机）"
    print(f"  IC (Spearman 秩相关): {ic:+.4f}  {ic_tag}")
    print(f"  命中率:               {hr * 100:.1f}%  {hr_tag}")
    print(sep)
    print("  五分位组平均年化收益（Q1=高分）：")
    for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
        v = metrics["quintiles"].get(q)
        if v is not None:
            ann = v / forward_years
            bar = "█" * max(0, int(ann * 100 / 5))
            print(f"    {q}: {ann * 100:+6.1f}%/年  {bar}")
    spread = metrics["quintiles"].get("spread_Q1_Q5", 0)
    fwd = forward_years if forward_years else 1
    print(f"  Q1-Q5 年化超额: {spread / fwd * 100:+.1f}%/年")
    print("=" * 55)
    print()


# ══════════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════════

def run_backtest(
    quarter: str,
    forward_years: int,
    score_key: str,
    top_n: Optional[int],
    end_date_override: Optional[date] = None,
    verbose: bool = True,
    include_veto: bool = False,
) -> dict:
    """
    执行一次完整回测，返回结果 dict。
    """
    start = quarter_end_date(quarter)
    end = end_date_override or (start + timedelta(days=365 * forward_years))

    today = date.today()
    # 若季度截止日还没到，以打分文件时间为起点（用第一个 JSON 的 scored_at）
    if start > today:
        print(f"[提示] 季度截止日 {start} 还未到，改用今天 {today} 作为起点")
        start = today

    if end > today:
        actual_years = max((today - start).days / 365, 0.01)
        print(f"[提示] 终止日期 {end} 超过今天 ({today})，"
              f"改用今天（实际前瞻期 {actual_years:.1f} 年）")
        end = today
        forward_years = round(actual_years, 1)

    print(f"\n[回测配置]")
    print(f"  打分季度:   {quarter}  (截止日 {start})")
    print(f"  前瞻期:     {forward_years} 年  (终止日 {end})")
    print(f"  排名依据:   {score_key}")
    if top_n:
        print(f"  取前 N 只:  {top_n}")

    # 1. 加载打分
    scored = load_quarter_scores(quarter, score_key, top_n, include_veto)
    print(f"\n[打分加载] {len(scored)} 只股票（已过滤否决）")

    # 2. 拉取历史价格，计算涨幅
    fetcher = DataFetcher()
    rows = []
    failed = []

    for i, rec in enumerate(scored):
        ticker = rec["ticker"]
        if verbose and (i % 20 == 0 or i == len(scored) - 1):
            print(f"  进度: {i + 1}/{len(scored)}  当前: {ticker}", end="\r", flush=True)

        ret = compute_forward_return(ticker, start, end, fetcher)
        rows.append({
            **rec,
            "predicted_rank": i + 1,
            "forward_return": ret,
        })
        if ret is None:
            failed.append(ticker)

    print()
    if failed:
        print(f"[数据缺失] {len(failed)} 只股票无价格数据: {failed[:10]}"
              + ("..." if len(failed) > 10 else ""))
    if len(failed) == len(rows):
        print("\n[提示] 所有股票均无价格数据。请确认：")
        print("  1. FMP_API_KEY 已在 .env 文件中配置")
        print("  2. 或先运行 fetch_data.py 将价格数据写入缓存")
        print("  3. price_daily:{ticker} 缓存 TTL 为 7 天")

    # 3. 过滤出有涨幅数据的股票，计算实际排名
    valid_rows = [r for r in rows if r["forward_return"] is not None]
    valid_rows.sort(key=lambda r: r["forward_return"], reverse=True)
    for rank, r in enumerate(valid_rows, 1):
        r["actual_rank"] = rank

    # 还原 predicted_rank（在有数据的子集中重排）
    valid_rows.sort(key=lambda r: r["predicted_rank"])
    for rank, r in enumerate(valid_rows, 1):
        r["predicted_rank"] = rank

    # 4. 计算指标
    scores_list = [r["score"] for r in valid_rows]
    returns_list = [r["forward_return"] for r in valid_rows]

    ic = spearman_correlation(scores_list, returns_list) if len(valid_rows) >= 5 else float("nan")
    hr = hit_rate(scores_list, returns_list) if len(valid_rows) >= 5 else float("nan")
    quintiles = quintile_analysis(scores_list, returns_list)

    metrics = {
        "n_total": len(rows),
        "n_valid": len(valid_rows),
        "coverage_rate": len(valid_rows) / len(rows) if rows else 0,
        "ic": ic,
        "hit_rate": hr,
        "quintiles": quintiles,
        "forward_years": forward_years,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }

    return {"rows": valid_rows, "metrics": metrics}


def main():
    parser = argparse.ArgumentParser(
        description="打分回测：验证历史打分对未来涨幅的预测能力",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python backtest.py --list
  python backtest.py --quarter 2023-Q1 --forward-years 3
  python backtest.py --quarter 2023-Q1 --score-key buffett --top-n 50
  python backtest.py --quarter 2022-Q4 --forward-years 1 --no-save
        """
    )
    parser.add_argument("--quarter", help="回测季度，如 2023-Q1")
    parser.add_argument("--forward-years", type=float, default=3,
                        help="前瞻年数（默认 3，支持小数如 1.5）")
    parser.add_argument("--score-key", default="composite_score",
                        choices=VALID_SCORE_KEYS,
                        help="排名依据（默认 composite_score）")
    parser.add_argument("--top-n", type=int, default=None,
                        help="只取打分最高的前 N 只（默认全部）")
    parser.add_argument("--end-date", help="手动指定终止日期 YYYY-MM-DD")
    parser.add_argument("--no-save", action="store_true",
                        help="不保存 CSV/报告文件")
    parser.add_argument("--output-dir", default=None,
                        help="输出目录（默认 scores/{quarter}/backtest/）")
    parser.add_argument("--list", action="store_true",
                        help="列出所有可回测的季度")
    parser.add_argument("--all-keys", action="store_true",
                        help="对所有 5 个打分维度分别跑一次并汇总对比")
    parser.add_argument("--include-veto", action="store_true",
                        help="包含触发否决的股票（可验证否决规则的有效性）")
    args = parser.parse_args()

    if args.list:
        quarters = list_scored_quarters()
        if not quarters:
            print("暂无打分数据（scores/ 目录为空）")
        else:
            print("可回测的季度：")
            for q in quarters:
                q_dir = SCORES_DIR / q
                n = len([f for f in q_dir.glob("*.json") if not f.name.startswith("_")])
                print(f"  {q}  ({n} 只股票)")
        return

    if not args.quarter:
        quarters = list_scored_quarters()
        if not quarters:
            sys.exit("[错误] 请用 --quarter 指定季度，或先运行 run_scoring.py 生成打分")
        args.quarter = quarters[-1]
        print(f"[自动选择] 使用最新季度: {args.quarter}")

    end_date = date.fromisoformat(args.end_date) if args.end_date else None
    output_dir = Path(args.output_dir) if args.output_dir else (
        SCORES_DIR / args.quarter / "backtest"
    )

    if args.all_keys:
        # 对所有打分维度跑一次，汇总对比
        print("\n[对比模式] 对所有打分维度逐一回测\n")
        summary_rows = []
        for key in VALID_SCORE_KEYS:
            print(f"\n{'─' * 40}")
            print(f"  维度: {key}")
            result = run_backtest(
                args.quarter, args.forward_years, key,
                args.top_n, end_date, verbose=False,
                include_veto=args.include_veto,
            )
            m = result["metrics"]
            summary_rows.append({
                "score_key": key,
                "n_valid": m["n_valid"],
                "ic": m["ic"],
                "hit_rate": m["hit_rate"],
                "q1_return": m["quintiles"].get("Q1", float("nan")),
                "q5_return": m["quintiles"].get("Q5", float("nan")),
                "spread": m["quintiles"].get("spread_Q1_Q5", float("nan")),
            })

        # 打印对比表
        print("\n" + "=" * 65)
        print(f"  {'维度':<18} {'IC':>8} {'命中率':>8} {'Q1收益':>9} {'Q5收益':>9} {'Q1-Q5':>9}")
        print(f"  {'─' * 18} {'─' * 8} {'─' * 8} {'─' * 9} {'─' * 9} {'─' * 9}")
        summary_rows.sort(key=lambda r: r["ic"], reverse=True)
        for r in summary_rows:
            print(f"  {r['score_key']:<18} "
                  f"{r['ic']:>+8.4f} "
                  f"{r['hit_rate'] * 100:>7.1f}% "
                  f"{r['q1_return'] * 100:>+8.1f}% "
                  f"{r['q5_return'] * 100:>+8.1f}% "
                  f"{r['spread'] * 100:>+8.1f}%")
        print("=" * 65)
        return

    # 单次回测
    result = run_backtest(
        args.quarter, args.forward_years, args.score_key,
        args.top_n, end_date, include_veto=args.include_veto,
    )
    metrics = result["metrics"]
    rows = result["rows"]

    print_summary(args.quarter, metrics["forward_years"], args.score_key, metrics)

    if not args.no_save:
        rpt_path, csv_path = save_report(
            args.quarter, int(metrics["forward_years"]),
            args.score_key, rows, metrics, output_dir
        )
        print(f"[输出] 报告: {rpt_path}")
        print(f"[输出] 数据: {csv_path}")


if __name__ == "__main__":
    main()
