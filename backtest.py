"""
回测验证脚本：打分准确度评估（v2）
====================================
自动打分 → 自动评估 → 输出清晰报告

核心流程:
  1. 解析打分时间段（scoring period）和评估时间段（eval period）
  2. 自动调用 run_scoring.py 对打分时间段内的季度打分（若尚未打分）
  3. 以评估时间段内的股价涨幅衡量打分准确性
  4. 输出: 整体评估结果 → 个股明细

─── 时间段输入格式 ──────────────────────────────────────────────────
  单季度:   2023-Q1
  多季度:   2023-Q1 2023-Q2
  单年:     2023
  多年:     2023 2024
  区间:     2023,2024  (等价于 2023 2024)
  混合:     2023 2024-Q1 2024-Q2

─── 用法示例 ────────────────────────────────────────────────────────
  # 用 2023-Q1 的打分，评估到 2024-Q1（1年前瞻）
  python backtest.py --score-period 2023-Q1 --eval-period 2024-Q1

  # 用 2023 年全年打分，评估到 2024 年末
  python backtest.py --score-period 2023 --eval-period 2024

  # 用 2023 和 2024 年的打分，评估到 2025-Q2
  python backtest.py --score-period 2023,2024 --eval-period 2025-Q2

  # 列出可用季度
  python backtest.py --list

  # 对比所有打分维度
  python backtest.py --score-period 2023-Q1 --eval-period 2024-Q1 --all-keys
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# ── 可选依赖 ─────────────────────────────────────────────────────────────────
try:
    from scipy.stats import spearmanr
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

# ── 项目内部模块 ──────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from src.fetcher import DataFetcher

SCORES_DIR = Path("scores")
VALID_SCORE_KEYS = [
    "composite_score", "buffett", "munger", "duan", "lynch"
]


# ══════════════════════════════════════════════════════════════════════════════
# 时间段解析
# ══════════════════════════════════════════════════════════════════════════════

def quarter_end_date(quarter: str) -> date:
    """'2023-Q1' → date(2023, 3, 31)"""
    year_str, q_str = quarter.split("-")
    year = int(year_str)
    q = int(q_str[1])
    quarter_ends = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    m, d = quarter_ends[q]
    return date(year, m, d)


def quarter_start_date(quarter: str) -> date:
    """'2023-Q1' → date(2023, 1, 1)"""
    year_str, q_str = quarter.split("-")
    year = int(year_str)
    q = int(q_str[1])
    quarter_starts = {1: (1, 1), 2: (4, 1), 3: (7, 1), 4: (10, 1)}
    m, d = quarter_starts[q]
    return date(year, m, d)


def year_to_quarters(year: int) -> list[str]:
    """2023 → ['2023-Q1', '2023-Q2', '2023-Q3', '2023-Q4']"""
    return [f"{year}-Q{q}" for q in range(1, 5)]


def parse_period(raw_input: str) -> list[str]:
    """
    解析灵活的时间段输入，返回季度列表（升序）。

    支持格式:
      '2023-Q1'            → ['2023-Q1']
      '2023-Q1 2023-Q2'    → ['2023-Q1', '2023-Q2']
      '2023'               → ['2023-Q1', '2023-Q2', '2023-Q3', '2023-Q4']
      '2023 2024'          → ['2023-Q1', ..., '2024-Q4']
      '2023,2024'          → ['2023-Q1', ..., '2024-Q4']
      '2023 2024-Q1'       → ['2023-Q1', ..., '2023-Q4', '2024-Q1']
    """
    # 按空格和逗号拆分
    tokens = re.split(r'[,\s]+', raw_input.strip())
    quarters = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if re.match(r'^\d{4}-Q[1-4]$', token):
            quarters.append(token)
        elif re.match(r'^\d{4}$', token):
            quarters.extend(year_to_quarters(int(token)))
        else:
            raise ValueError(f"无法识别的时间格式: '{token}'  "
                             f"(支持: 2023-Q1, 2023, 2023,2024)")
    # 去重 + 排序
    return sorted(set(quarters))


def validate_periods(score_quarters: list[str], eval_quarters: list[str]):
    """验证评估时间段必须晚于打分时间段"""
    latest_score = max(quarter_end_date(q) for q in score_quarters)
    earliest_eval = min(quarter_start_date(q) for q in eval_quarters)
    if earliest_eval < latest_score:
        raise ValueError(
            f"评估时间段起始 ({earliest_eval}) 不能早于打分时间段结束 ({latest_score})。\n"
            f"  打分季度: {score_quarters}\n"
            f"  评估季度: {eval_quarters}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 自动打分
# ══════════════════════════════════════════════════════════════════════════════

def ensure_scored(quarters: list[str], fresh: bool = False):
    """
    确保指定季度已有打分数据，若无则自动调用 run_scoring.py 打分。
    """
    from run_scoring import run_offline_cached

    for q in quarters:
        q_dir = SCORES_DIR / q
        json_files = list(q_dir.glob("*.json")) if q_dir.exists() else []
        json_files = [f for f in json_files if not f.name.startswith("_")]

        if json_files and not fresh:
            print(f"  [打分] {q}: 已有 {len(json_files)} 只股票，跳过")
            continue

        print(f"  [打分] {q}: 自动运行离线打分...")
        run_offline_cached(quarter=q, fresh=fresh)
        # 验证打分结果
        json_files = [f for f in q_dir.glob("*.json") if not f.name.startswith("_")] if q_dir.exists() else []
        if not json_files:
            print(f"  [警告] {q}: 打分后仍无数据，请检查缓存中是否有财务数据")


# ══════════════════════════════════════════════════════════════════════════════
# 数据加载
# ══════════════════════════════════════════════════════════════════════════════

def load_quarter_scores(quarter: str, score_key: str,
                        include_veto: bool = False) -> list[dict]:
    """加载单季度所有打分股票，返回列表（含完整数据）。"""
    q_dir = SCORES_DIR / quarter
    if not q_dir.exists():
        return []

    records = []
    for fp in q_dir.glob("*.json"):
        if fp.name.startswith("_"):
            continue
        try:
            with fp.open(encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        if data.get("veto_triggered") and not include_veto:
            continue

        if score_key == "composite_score":
            score = data.get("composite_score")
        else:
            score = data.get("master_scores", {}).get(score_key)

        if score is None:
            continue

        records.append({
            "ticker": data["ticker"],
            "score": score,
            "composite_score": data.get("composite_score"),
            "dimensions": data.get("dimensions", {}),
            "master_scores": data.get("master_scores", {}),
            "veto_triggered": data.get("veto_triggered", False),
            "veto_reasons": data.get("veto_reasons", []),
            "sector": data.get("company_info", {}).get("sector", ""),
            "name": data.get("company_info", {}).get("name", ""),
            "market_cap": data.get("company_info", {}).get("market_cap"),
            "quarter": quarter,
        })

    return records


def aggregate_multi_quarter_scores(
    quarters: list[str],
    score_key: str,
    mode: str = "quarter_weighted",
    include_veto: bool = False,
) -> list[dict]:
    """
    聚合多个季度的打分。

    mode:
      'quarter_weighted' (default): 多季度按等权平均聚合
      'latest':                     取最新季度的分数
    """
    all_quarter_data: dict[str, list[dict]] = {}  # ticker → [records]

    for q in quarters:
        for rec in load_quarter_scores(q, score_key, include_veto):
            all_quarter_data.setdefault(rec["ticker"], []).append(rec)

    if not all_quarter_data:
        return []

    aggregated = []
    for ticker, entries in all_quarter_data.items():
        if mode == "latest":
            # 取最新季度
            entries.sort(key=lambda r: r["quarter"])
            best = entries[-1]
            best["_quarters_included"] = [e["quarter"] for e in entries]
            best["_quarter_scores"] = {e["quarter"]: e["score"] for e in entries}
            aggregated.append(best)
        else:
            # quarter_weighted: 等权平均
            avg_score = sum(e["score"] for e in entries) / len(entries)
            latest = max(entries, key=lambda e: e["quarter"])
            aggregated.append({
                "ticker": ticker,
                "score": avg_score,
                "composite_score": sum(e["composite_score"] for e in entries) / len(entries),
                "dimensions": latest["dimensions"],
                "master_scores": latest["master_scores"],
                "veto_triggered": any(e["veto_triggered"] for e in entries),
                "veto_reasons": list({r for e in entries for r in e.get("veto_reasons", [])}),
                "sector": latest["sector"],
                "name": latest["name"],
                "market_cap": latest["market_cap"],
                "quarter": f"{quarters[0]}~{quarters[-1]}" if len(quarters) > 1 else quarters[0],
                "_quarters_included": [e["quarter"] for e in entries],
                "_quarter_scores": {e["quarter"]: e["score"] for e in entries},
            })

    aggregated.sort(key=lambda x: x["score"], reverse=True)
    return aggregated


# ══════════════════════════════════════════════════════════════════════════════
# 价格计算
# ══════════════════════════════════════════════════════════════════════════════

def build_price_index(price_history: list[dict]) -> tuple[list[str], list[float]]:
    """将价格序列转换为有序的 (dates, prices)，便于二分查找。"""
    from bisect import bisect_left
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
    """在有序价格序列中找最近交易日的价格（±10个交易日）。"""
    from bisect import bisect_left
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


def compute_return(ticker: str, start: date, end: date,
                   fetcher: DataFetcher) -> Optional[float]:
    """计算 start → end 期间的股价涨幅。"""
    try:
        history = fetcher.get_price_history_daily(ticker, years=20)
    except (ValueError, RuntimeError):
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
    n = len(scores)
    if n < 5:
        return {}

    pairs = sorted(zip(scores, returns), key=lambda x: x[0], reverse=True)
    group_size = n // 5
    result = {}
    for q in range(1, 6):
        si = (q - 1) * group_size
        ei = n if q == 5 else q * group_size
        group_returns = [r for _, r in pairs[si:ei]]
        result[f"Q{q}"] = sum(group_returns) / len(group_returns)

    result["spread_Q1_Q5"] = result.get("Q1", 0) - result.get("Q5", 0)
    return result


def hit_rate(scores: list[float], returns: list[float]) -> float:
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
# 主回测流程
# ══════════════════════════════════════════════════════════════════════════════

def _compute_veto_analysis(
    score_quarters: list[str],
    score_key: str,
    agg_mode: str,
    score_end: date,
    eval_end: date,
    fetcher: DataFetcher,
    non_veto_rows: list[dict],
    verbose: bool = True,
) -> dict:
    """
    否决机制有效性分析。

    加载所有被否决的股票，计算其实际涨幅，
    与非否决股对比，判断否决机制是否真的过滤掉了差股票。
    """
    # 加载全部股票（含否决），再筛出仅否决股
    all_scored = aggregate_multi_quarter_scores(
        score_quarters, score_key, mode=agg_mode, include_veto=True
    )
    veto_scored = [s for s in all_scored if s.get("veto_triggered")]

    if not veto_scored:
        return {"n_veto": 0}

    if verbose:
        print(f"\n[否决分析] 加载 {len(veto_scored)} 只否决股...")

    # 计算否决股涨幅
    veto_rows = []
    for rec in veto_scored:
        ret = compute_return(rec["ticker"], score_end, eval_end, fetcher)
        veto_rows.append({
            **rec,
            "forward_return": ret,
        })

    veto_valid = [r for r in veto_rows if r["forward_return"] is not None]
    non_veto_valid = [r for r in non_veto_rows if r["forward_return"] is not None]

    # 基础统计
    veto_returns = [r["forward_return"] for r in veto_valid]
    non_veto_returns = [r["forward_return"] for r in non_veto_valid]

    veto_avg = sum(veto_returns) / len(veto_returns) if veto_returns else None
    non_veto_avg = sum(non_veto_returns) / len(non_veto_returns) if non_veto_returns else None
    veto_median = sorted(veto_returns)[len(veto_returns) // 2] if veto_returns else None
    non_veto_median = sorted(non_veto_returns)[len(non_veto_returns) // 2] if non_veto_returns else None

    # 高分否决股分析（分数高于非否决股中位数，但被否决了）
    non_veto_scores = [r["score"] for r in non_veto_valid]
    score_median = sorted(non_veto_scores)[len(non_veto_scores) // 2] if non_veto_scores else 0
    high_score_vetoed = [r for r in veto_valid if r["score"] >= score_median]
    high_score_vetoed.sort(key=lambda r: r["score"], reverse=True)

    high_veto_avg = (
        sum(r["forward_return"] for r in high_score_vetoed) / len(high_score_vetoed)
        if high_score_vetoed else None
    )

    # 否决原因统计
    reason_counts: dict[str, int] = {}
    reason_returns: dict[str, list[float]] = {}
    for r in veto_valid:
        for reason in r.get("veto_reasons", []):
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            reason_returns.setdefault(reason, []).append(r["forward_return"])

    reason_stats = {}
    for reason, rets in reason_returns.items():
        reason_stats[reason] = {
            "count": reason_counts[reason],
            "avg_return": sum(rets) / len(rets),
        }

    # 判断否决有效性
    if veto_avg is not None and non_veto_avg is not None:
        saved_return = non_veto_avg - veto_avg  # 正值 = 否决帮你避开了差股
        if saved_return > 0.02:
            effectiveness = "有效"
        elif saved_return > -0.02:
            effectiveness = "中性"
        else:
            effectiveness = "过度否决"
    else:
        saved_return = None
        effectiveness = "数据不足"

    return {
        "n_veto": len(veto_scored),
        "n_veto_valid": len(veto_valid),
        "n_non_veto_valid": len(non_veto_valid),
        "veto_avg_return": veto_avg,
        "veto_median_return": veto_median,
        "non_veto_avg_return": non_veto_avg,
        "non_veto_median_return": non_veto_median,
        "saved_return": saved_return,
        "effectiveness": effectiveness,
        "high_score_vetoed": high_score_vetoed[:10],  # Top 10 高分否决股
        "high_score_vetoed_count": len(high_score_vetoed),
        "high_veto_avg_return": high_veto_avg,
        "score_median_threshold": score_median,
        "reason_stats": reason_stats,
        "veto_rows": veto_valid,
    }


def run_backtest(
    score_quarters: list[str],
    eval_quarters: list[str],
    score_key: str = "composite_score",
    agg_mode: str = "quarter_weighted",
    top_n: Optional[int] = None,
    include_veto: bool = False,
    auto_score: bool = True,
    fresh_score: bool = False,
    verbose: bool = True,
) -> dict:
    """
    执行完整回测流程。

    参数:
      score_quarters: 打分季度列表 (如 ['2023-Q1', '2023-Q2'])
      eval_quarters:  评估季度列表 (如 ['2024-Q1'])
      score_key:      排名依据
      agg_mode:       聚合方式 ('quarter_weighted' | 'latest')
      top_n:          只看前 N 只
      include_veto:   是否包含否决股（混入主排名分析）
      auto_score:     是否自动打分
      fresh_score:    是否重新打分

    返回:
      {
        'rows': [个股详情],
        'metrics': {整体指标},
        'config': {回测配置},
        'veto_analysis': {否决机制有效性分析},
      }
    """
    # 计算时间窗口
    score_start = min(quarter_start_date(q) for q in score_quarters)
    score_end = max(quarter_end_date(q) for q in score_quarters)
    eval_start = min(quarter_start_date(q) for q in eval_quarters)
    eval_end = max(quarter_end_date(q) for q in eval_quarters)

    today = date.today()
    if eval_end > today:
        eval_end = today

    forward_years = max((eval_end - score_end).days / 365, 0.01)

    config = {
        "score_quarters": score_quarters,
        "eval_quarters": eval_quarters,
        "score_key": score_key,
        "agg_mode": agg_mode,
        "score_date_range": f"{score_start} ~ {score_end}",
        "eval_date_range": f"{eval_start} ~ {eval_end}",
        "forward_years": round(forward_years, 2),
    }

    if verbose:
        print(f"\n{'='*65}")
        print(f"  回测配置")
        print(f"{'='*65}")
        print(f"  打分时间段:  {', '.join(score_quarters)}")
        print(f"               ({score_start} ~ {score_end})")
        print(f"  评估时间段:  {', '.join(eval_quarters)}")
        print(f"               ({eval_start} ~ {eval_end})")
        print(f"  前瞻期:      {forward_years:.2f} 年")
        print(f"  排名依据:    {score_key}")
        print(f"  聚合方式:    {agg_mode}")
        if top_n:
            print(f"  取前 N 只:   {top_n}")
        print(f"{'='*65}\n")

    # 1. 自动打分
    if auto_score:
        print("[Step 1] 检查/自动打分...")
        ensure_scored(score_quarters, fresh=fresh_score)
        print()

    # 2. 加载并聚合打分（非否决股用于主排名）
    print("[Step 2] 加载打分数据...")
    scored = aggregate_multi_quarter_scores(
        score_quarters, score_key, mode=agg_mode, include_veto=include_veto
    )
    if not scored:
        print("[错误] 无有效打分数据")
        return {"rows": [], "metrics": {}, "config": config, "veto_analysis": {}}

    if top_n:
        scored = scored[:top_n]
    print(f"  加载 {len(scored)} 只股票")

    # 3. 计算评估期间涨幅
    print(f"\n[Step 3] 计算评估期 ({score_end} → {eval_end}) 涨幅...")
    fetcher = DataFetcher()
    rows = []
    failed = []

    for i, rec in enumerate(scored):
        ticker = rec["ticker"]
        if verbose and (i % 20 == 0 or i == len(scored) - 1):
            print(f"  进度: {i + 1}/{len(scored)}  当前: {ticker}", end="\r", flush=True)

        ret = compute_return(ticker, score_end, eval_end, fetcher)
        rows.append({
            **rec,
            "predicted_rank": i + 1,
            "forward_return": ret,
        })
        if ret is None:
            failed.append(ticker)

    print()
    if failed:
        print(f"  [数据缺失] {len(failed)} 只无价格数据: "
              f"{failed[:10]}{'...' if len(failed) > 10 else ''}")

    # 4. 计算实际排名
    valid_rows = [r for r in rows if r["forward_return"] is not None]
    valid_rows.sort(key=lambda r: r["forward_return"], reverse=True)
    for rank, r in enumerate(valid_rows, 1):
        r["actual_rank"] = rank

    valid_rows.sort(key=lambda r: r["predicted_rank"])
    for rank, r in enumerate(valid_rows, 1):
        r["predicted_rank"] = rank

    # 5. 计算指标
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
        "score_end": score_end.isoformat(),
        "eval_end": eval_end.isoformat(),
    }

    # 6. 否决机制有效性分析（始终执行）
    print("\n[Step 4] 否决机制有效性分析...")
    veto_analysis = _compute_veto_analysis(
        score_quarters, score_key, agg_mode,
        score_end, eval_end, fetcher, valid_rows, verbose,
    )

    return {"rows": valid_rows, "metrics": metrics, "config": config,
            "veto_analysis": veto_analysis}


# ══════════════════════════════════════════════════════════════════════════════
# 报告输出
# ══════════════════════════════════════════════════════════════════════════════

def format_pct(v, digits=1) -> str:
    """格式化百分比"""
    if v is None or v != v:  # None or NaN
        return "N/A"
    return f"{v * 100:+.{digits}f}%"


def print_report(result: dict):
    """打印完整报告: 整体评估 → 个股明细"""
    metrics = result["metrics"]
    config = result["config"]
    rows = result["rows"]

    if not metrics:
        print("\n[报告] 无有效数据，无法生成报告")
        return

    ic = metrics["ic"]
    hr = metrics["hit_rate"]
    fwd = metrics["forward_years"]
    quintiles = metrics["quintiles"]

    # ── 第一部分: 整体评估 ──────────────────────────────────────────────
    print()
    print("=" * 70)
    print("  回测评估报告")
    print("=" * 70)

    # 总评
    print()
    print("  [总体评估]")
    if ic != ic:  # NaN
        grade = "数据不足"
        grade_detail = "有效样本不足5只，统计指标不可靠"
    elif ic > 0.10:
        grade = "优秀 ★★★"
        grade_detail = "打分与实际涨幅高度相关，预测能力强"
    elif ic > 0.05:
        grade = "良好 ★★"
        grade_detail = "打分有一定预测能力，高分股总体跑赢低分股"
    elif ic > 0.03:
        grade = "有效 ★"
        grade_detail = "打分具备微弱但正向的预测信号"
    elif ic > 0:
        grade = "较弱"
        grade_detail = "正相关但信号很弱，接近随机"
    else:
        grade = "无效"
        grade_detail = "负相关，打分未能预测未来涨幅"

    print(f"    评级:     {grade}")
    print(f"    说明:     {grade_detail}")
    print()

    # 核心指标
    print("  [核心指标]")
    print(f"    打分时段:     {', '.join(config['score_quarters'])}")
    print(f"    评估时段:     {', '.join(config['eval_quarters'])}")
    print(f"    前瞻期:       {fwd:.2f} 年")
    print(f"    排名依据:     {config['score_key']}")
    print(f"    聚合方式:     {config['agg_mode']}")
    print(f"    有效样本:     {metrics['n_valid']} / {metrics['n_total']}"
          f"  (覆盖率 {metrics['coverage_rate'] * 100:.0f}%)")
    print()
    ic_tag = "有效" if ic > 0.03 else "弱" if ic > 0 else "负"
    hr_tag = "优于随机" if hr > 0.5 else "低于随机"
    print(f"    IC (Spearman):  {ic:+.4f}    ({ic_tag})")
    print(f"    命中率:         {hr * 100:.1f}%      ({hr_tag})")
    print()

    # 五分位组收益
    if quintiles:
        print("  [五分位组收益]   （Q1=高分, Q5=低分）")
        for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
            v = quintiles.get(q)
            if v is not None:
                ann = v / fwd if fwd > 0 else v
                bar_len = max(0, int(ann * 100 / 5))
                bar = "█" * min(bar_len, 40)
                tag = ""
                if q == "Q1":
                    tag = "  ← 高分组"
                elif q == "Q5":
                    tag = "  ← 低分组"
                print(f"    {q}: 总收益 {v * 100:+7.1f}%  "
                      f"(年化 {ann * 100:+6.1f}%)  {bar}{tag}")
        spread = quintiles.get("spread_Q1_Q5", 0)
        ann_spread = spread / fwd if fwd > 0 else spread
        print(f"    ────────────────────────────────────")
        print(f"    Q1-Q5 超额:  {spread * 100:+.1f}%  "
              f"(年化 {ann_spread * 100:+.1f}%/年)")
    print()

    # ── 否决机制有效性分析 ──────────────────────────────────────────────
    veto_analysis = result.get("veto_analysis", {})
    n_veto = veto_analysis.get("n_veto", 0)
    if n_veto > 0:
        print("─" * 70)
        print("  否决机制有效性分析")
        print("─" * 70)
        print()

        n_vv = veto_analysis.get("n_veto_valid", 0)
        n_nv = veto_analysis.get("n_non_veto_valid", 0)
        va_ret = veto_analysis.get("veto_avg_return")
        nv_ret = veto_analysis.get("non_veto_avg_return")
        va_med = veto_analysis.get("veto_median_return")
        nv_med = veto_analysis.get("non_veto_median_return")
        eff = veto_analysis.get("effectiveness", "数据不足")

        print(f"  [否决概览]")
        print(f"    被否决股票:   {n_veto} 只 ({n_vv} 只有价格数据)")
        print(f"    通过股票:     {n_nv} 只")
        print()

        if va_ret is not None and nv_ret is not None:
            saved = veto_analysis.get("saved_return", 0)
            print(f"  [收益对比]     {'否决股':>10} {'通过股':>10} {'差值':>10}")
            print(f"    平均收益:    {va_ret*100:>+9.1f}% {nv_ret*100:>+9.1f}% {(nv_ret-va_ret)*100:>+9.1f}%")
            if va_med is not None and nv_med is not None:
                print(f"    中位收益:    {va_med*100:>+9.1f}% {nv_med*100:>+9.1f}% {(nv_med-va_med)*100:>+9.1f}%")
            print()
            print(f"  [否决有效性]   {eff}")
            if eff == "有效":
                print(f"    否决机制有效：被否决股平均跑输通过股 {saved*100:.1f}%")
            elif eff == "过度否决":
                print(f"    否决可能过严：被否决股平均跑赢通过股 {-saved*100:.1f}%")
                print(f"    建议检查否决阈值是否过于严格")
            else:
                print(f"    否决影响中性：两组收益差异不大 ({saved*100:+.1f}%)")
            print()

        # 高分否决股明细
        high_vetoed = veto_analysis.get("high_score_vetoed", [])
        if high_vetoed:
            threshold = veto_analysis.get("score_median_threshold", 0)
            hv_count = veto_analysis.get("high_score_vetoed_count", 0)
            hv_avg = veto_analysis.get("high_veto_avg_return")
            print(f"  [高分否决股]  分数 >= {threshold:.2f}（通过股中位数），共 {hv_count} 只")
            if hv_avg is not None:
                print(f"    这些股票平均涨幅: {hv_avg*100:+.1f}%")
                if nv_ret is not None:
                    diff = hv_avg - nv_ret
                    if diff > 0.02:
                        print(f"    --> 高分否决股跑赢通过股 {diff*100:.1f}%，否决可能误杀了好股票！")
                    elif diff < -0.02:
                        print(f"    --> 高分否决股跑输通过股 {-diff*100:.1f}%，否决判断正确")
                    else:
                        print(f"    --> 与通过股收益接近（差 {diff*100:+.1f}%）")
            print()
            print(f"    {'代码':<7} {'名称':<18} {'打分':>6}  {'涨幅':>9}  否决原因")
            print(f"    {'─'*7} {'─'*18} {'─'*6}  {'─'*9}  {'─'*25}")
            for r in high_vetoed[:10]:
                ret = r.get("forward_return")
                ret_str = format_pct(ret) if ret is not None else "N/A"
                reasons = "; ".join(r.get("veto_reasons", [])[:2])
                name = (r.get("name") or "")[:16]
                print(f"    {r['ticker']:<7} {name:<18} {r['score']:>6.2f}  {ret_str:>9}  {reasons}")
            print()

        # 否决原因统计
        reason_stats = veto_analysis.get("reason_stats", {})
        if reason_stats:
            print(f"  [否决原因效果]")
            print(f"    {'原因':<35} {'次数':>4}  {'平均涨幅':>10}")
            print(f"    {'─'*35} {'─'*4}  {'─'*10}")
            for reason, stats in sorted(reason_stats.items(),
                                         key=lambda x: x[1]["avg_return"]):
                print(f"    {reason[:33]:<35} {stats['count']:>4}  "
                      f"{stats['avg_return']*100:>+9.1f}%")
            print()
    else:
        print("  [否决分析] 本次回测无否决股票")
        print()

    # ── 第二部分: 个股明细 ──────────────────────────────────────────────
    if not rows:
        print("  [个股明细] 无数据")
        print("=" * 70)
        return

    print("─" * 70)
    print("  个股明细")
    print("─" * 70)

    # Top 20 高分股
    print()
    print("  [Top 20 高分股]")
    print(f"    {'排名':>4}  {'代码':<7} {'名称':<20} {'行业':<12} "
          f"{'打分':>6}  {'实际涨幅':>10}  {'实际排名':>6}")
    print(f"    {'─'*4}  {'─'*7} {'─'*20} {'─'*12} "
          f"{'─'*6}  {'─'*10}  {'─'*6}")
    for r in rows[:20]:
        ret = r.get("forward_return")
        ret_str = format_pct(ret) if ret is not None else "N/A"
        name = (r.get("name") or "")[:18]
        sector = (r.get("sector") or "")[:10]
        print(f"    {r['predicted_rank']:>4}  {r['ticker']:<7} {name:<20} {sector:<12} "
              f"{r['score']:>6.2f}  {ret_str:>10}  {r.get('actual_rank', 'N/A'):>6}")

    # Bottom 10 低分股
    if len(rows) > 20:
        print()
        print("  [Bottom 10 低分股]")
        bottom = sorted(rows, key=lambda r: r["score"])[:10]
        print(f"    {'排名':>4}  {'代码':<7} {'名称':<20} {'行业':<12} "
              f"{'打分':>6}  {'实际涨幅':>10}  {'实际排名':>6}")
        print(f"    {'─'*4}  {'─'*7} {'─'*20} {'─'*12} "
              f"{'─'*6}  {'─'*10}  {'─'*6}")
        for r in bottom:
            ret = r.get("forward_return")
            ret_str = format_pct(ret) if ret is not None else "N/A"
            name = (r.get("name") or "")[:18]
            sector = (r.get("sector") or "")[:10]
            print(f"    {r['predicted_rank']:>4}  {r['ticker']:<7} {name:<20} {sector:<12} "
                  f"{r['score']:>6.2f}  {ret_str:>10}  {r.get('actual_rank', 'N/A'):>6}")

    # 最大赢家 / 最大输家
    if rows:
        print()
        by_return = sorted(rows, key=lambda r: r.get("forward_return") or 0, reverse=True)
        winners = by_return[:5]
        losers = by_return[-5:]

        print("  [最大赢家 Top 5]")
        for r in winners:
            ret = r.get("forward_return")
            print(f"    {r['ticker']:<7} {(r.get('name') or '')[:18]:<20} "
                  f"打分: {r['score']:.2f}  涨幅: {format_pct(ret)}  "
                  f"预测排名: {r['predicted_rank']}")

        print()
        print("  [最大输家 Bottom 5]")
        for r in losers:
            ret = r.get("forward_return")
            print(f"    {r['ticker']:<7} {(r.get('name') or '')[:18]:<20} "
                  f"打分: {r['score']:.2f}  涨幅: {format_pct(ret)}  "
                  f"预测排名: {r['predicted_rank']}")

    # 多季度时展示各季度得分分布
    if any(r.get("_quarters_included") and len(r["_quarters_included"]) > 1 for r in rows[:5]):
        print()
        print("  [Top 5 各季度得分趋势]")
        for r in rows[:5]:
            qs = r.get("_quarter_scores", {})
            if qs:
                trend = "  ".join(f"{q}:{s:.2f}" for q, s in sorted(qs.items()))
                print(f"    {r['ticker']:<7} → {trend}  (均值: {r['score']:.2f})")

    print()
    print("=" * 70)


def save_report_files(result: dict, output_dir: Path) -> tuple[Path, Path]:
    """保存 CSV + 文本报告文件"""
    output_dir.mkdir(parents=True, exist_ok=True)
    config = result["config"]
    metrics = result["metrics"]
    rows = result["rows"]

    score_tag = "+".join(config["score_quarters"])
    eval_tag = "+".join(config["eval_quarters"])
    tag = f"{score_tag}_vs_{eval_tag}_{config['score_key']}"

    # ── CSV ──
    csv_path = output_dir / f"backtest_{tag}.csv"
    header = [
        "predicted_rank", "ticker", "name", "sector", "score",
        "forward_return_pct", "actual_rank", "rank_diff",
        "quarters_included", "quarter_scores",
    ]

    with csv_path.open("w", encoding="utf-8") as f:
        f.write(",".join(header) + "\n")
        for r in rows:
            ret = r.get("forward_return")
            ret_pct = f"{ret * 100:.2f}" if ret is not None else ""
            diff = r["predicted_rank"] - r.get("actual_rank", 0)
            qs_included = ";".join(r.get("_quarters_included", []))
            qs_scores = ";".join(f"{q}:{s:.2f}" for q, s in (r.get("_quarter_scores") or {}).items())
            f.write(",".join([
                str(r["predicted_rank"]),
                r["ticker"],
                f'"{r.get("name", "")}"',
                r.get("sector", ""),
                f"{r['score']:.4f}",
                ret_pct,
                str(r.get("actual_rank", "")),
                str(diff),
                qs_included,
                qs_scores,
            ]) + "\n")

    # ── 否决股 CSV（如果有否决分析数据）──
    veto_analysis = result.get("veto_analysis", {})
    veto_rows = veto_analysis.get("veto_rows", [])
    if veto_rows:
        veto_csv_path = output_dir / f"backtest_{tag}_veto.csv"
        veto_header = [
            "ticker", "name", "sector", "score",
            "forward_return_pct", "veto_reasons",
        ]
        with veto_csv_path.open("w", encoding="utf-8") as f:
            f.write(",".join(veto_header) + "\n")
            for r in sorted(veto_rows, key=lambda x: x["score"], reverse=True):
                ret = r.get("forward_return")
                ret_pct = f"{ret * 100:.2f}" if ret is not None else ""
                reasons = "; ".join(r.get("veto_reasons", []))
                f.write(",".join([
                    r["ticker"],
                    f'"{r.get("name", "")}"',
                    r.get("sector", ""),
                    f"{r['score']:.4f}",
                    ret_pct,
                    f'"{reasons}"',
                ]) + "\n")

    # ── 文本报告（重定向 print_report 到文件）──
    rpt_path = output_dir / f"backtest_{tag}_report.txt"
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_report(result)
    rpt_path.write_text(buf.getvalue(), encoding="utf-8")

    return rpt_path, csv_path


# ══════════════════════════════════════════════════════════════════════════════
# 列出可用季度
# ══════════════════════════════════════════════════════════════════════════════

def list_scored_quarters() -> list[str]:
    """列出 scores/ 下所有有效季度目录"""
    if not SCORES_DIR.exists():
        return []
    quarters = []
    for d in sorted(SCORES_DIR.iterdir()):
        if d.is_dir() and not d.name.startswith("_"):
            json_files = [f for f in d.glob("*.json") if not f.name.startswith("_")]
            if json_files:
                quarters.append(d.name)
    return quarters


def print_available():
    """打印可用季度列表"""
    quarters = list_scored_quarters()
    if not quarters:
        print("暂无打分数据（scores/ 目录为空）")
        print("  提示: 先运行 python run_scoring.py --offline-db 生成打分")
        return

    # 按年分组
    year_map: dict[str, list[str]] = {}
    for q in quarters:
        y = q.split("-")[0]
        year_map.setdefault(y, []).append(q)

    print("\n可用的打分数据：")
    print("─" * 50)
    for year, qs in sorted(year_map.items()):
        total_n = sum(
            len([f for f in (SCORES_DIR / q).glob("*.json") if not f.name.startswith("_")])
            for q in qs
        )
        print(f"  {year} 年  ({len(qs)} 个季度，共 {total_n} 只·次)")
        for q in qs:
            n = len([f for f in (SCORES_DIR / q).glob("*.json") if not f.name.startswith("_")])
            print(f"    {q}  ({n} 只)")
    print()
    print("用法示例:")
    print("  python backtest.py --score-period 2023-Q1 --eval-period 2024-Q1")
    print("  python backtest.py --score-period 2023 --eval-period 2024")
    print("  python backtest.py --score-period 2023,2024 --eval-period 2025-Q2")


# ══════════════════════════════════════════════════════════════════════════════
# CLI 入口
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="打分回测验证：自动打分 + 评估预测准确度",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
时间段格式:
  单季度:  2023-Q1
  多季度:  "2023-Q1 2023-Q2"
  单年:    2023  (= 2023-Q1 ~ Q4)
  多年:    "2023 2024"  或  2023,2024
  混合:    "2023 2024-Q1"

示例:
  python backtest.py --list
  python backtest.py --score-period 2023-Q1 --eval-period 2024-Q1
  python backtest.py --score-period 2023 --eval-period 2024
  python backtest.py --score-period 2023,2024 --eval-period 2025-Q2
  python backtest.py --score-period 2023-Q1 --eval-period 2024-Q1 --all-keys
  python backtest.py --score-period 2023-Q1 --eval-period 2024-Q1 --score-key buffett
        """
    )

    parser.add_argument("--score-period", dest="score_period",
                        help="打分时间段 (如 2023-Q1, 2023, '2023,2024')")
    parser.add_argument("--eval-period", dest="eval_period",
                        help="评估时间段 (必须晚于打分时间段)")
    parser.add_argument("--score-key", default="composite_score",
                        choices=VALID_SCORE_KEYS,
                        help="排名依据（默认 composite_score）")
    parser.add_argument("--agg-mode", default="quarter_weighted",
                        choices=["quarter_weighted", "latest"],
                        help="多季度聚合方式: quarter_weighted(等权平均,默认), latest(取最新)")
    parser.add_argument("--top-n", type=int, default=None,
                        help="只取打分最高的前 N 只")
    parser.add_argument("--no-save", action="store_true",
                        help="不保存 CSV/报告文件")
    parser.add_argument("--output-dir", default=None,
                        help="输出目录（默认 scores/backtest/）")
    parser.add_argument("--list", action="store_true",
                        help="列出所有可用的打分季度")
    parser.add_argument("--all-keys", action="store_true",
                        help="对所有打分维度分别回测并对比")
    parser.add_argument("--include-veto", action="store_true",
                        help="包含触发否决的股票")
    parser.add_argument("--no-auto-score", action="store_true",
                        help="不自动打分（仅使用已有打分数据）")
    parser.add_argument("--fresh-score", action="store_true",
                        help="强制重新打分（忽略已有结果）")

    # 向后兼容旧参数
    parser.add_argument("--quarter", help=argparse.SUPPRESS)
    parser.add_argument("--year", help=argparse.SUPPRESS)
    parser.add_argument("--forward-years", type=float, help=argparse.SUPPRESS)

    args = parser.parse_args()

    # ── --list ──
    if args.list:
        print_available()
        return

    # ── 向后兼容: 旧参数映射到新参数 ──
    if not args.score_period:
        if args.quarter:
            args.score_period = args.quarter
        elif args.year:
            args.score_period = args.year
        else:
            # 自动选最新季度
            quarters = list_scored_quarters()
            if quarters:
                args.score_period = quarters[-1]
                print(f"[自动选择] 打分时段: {args.score_period}")
            else:
                sys.exit("[错误] 请用 --score-period 指定打分时段，"
                         "或先运行 run_scoring.py 生成打分数据")

    if not args.eval_period:
        if args.forward_years:
            # 旧模式: 用 forward-years 推算评估时段
            sq = parse_period(args.score_period)
            end = max(quarter_end_date(q) for q in sq)
            eval_date = end + timedelta(days=int(args.forward_years * 365))
            # 找到对应的季度
            eval_year = eval_date.year
            eval_q = (eval_date.month - 1) // 3 + 1
            args.eval_period = f"{eval_year}-Q{eval_q}"
            print(f"[自动推算] 评估时段: {args.eval_period}  "
                  f"(基于 forward-years={args.forward_years})")
        else:
            sys.exit("[错误] 请用 --eval-period 指定评估时段\n"
                     "  示例: python backtest.py --score-period 2023-Q1 "
                     "--eval-period 2024-Q1")

    # ── 解析时间段 ──
    try:
        score_quarters = parse_period(args.score_period)
        eval_quarters = parse_period(args.eval_period)
    except ValueError as e:
        sys.exit(f"[错误] {e}")

    # ── 验证 ──
    try:
        validate_periods(score_quarters, eval_quarters)
    except ValueError as e:
        sys.exit(f"[错误] {e}")

    print(f"\n[时间段解析]")
    print(f"  打分季度: {score_quarters}")
    print(f"  评估季度: {eval_quarters}")

    output_dir = Path(args.output_dir) if args.output_dir else (
        SCORES_DIR / "backtest"
    )

    # ── --all-keys 模式 ──
    if args.all_keys:
        print("\n[对比模式] 对所有打分维度逐一回测\n")
        summary_rows = []
        for key in VALID_SCORE_KEYS:
            print(f"\n{'─' * 50}")
            print(f"  排名依据: {key}")
            result = run_backtest(
                score_quarters, eval_quarters,
                score_key=key, agg_mode=args.agg_mode,
                top_n=args.top_n,
                include_veto=args.include_veto,
                auto_score=(not args.no_auto_score),
                fresh_score=args.fresh_score,
                verbose=False,
            )
            m = result["metrics"]
            summary_rows.append({
                "score_key": key,
                "n_valid": m.get("n_valid", 0),
                "ic": m.get("ic", float("nan")),
                "hit_rate": m.get("hit_rate", float("nan")),
                "q1_return": m.get("quintiles", {}).get("Q1", float("nan")),
                "q5_return": m.get("quintiles", {}).get("Q5", float("nan")),
                "spread": m.get("quintiles", {}).get("spread_Q1_Q5", float("nan")),
            })

        # 对比表
        print("\n" + "=" * 72)
        print(f"  打分维度对比")
        print(f"  打分: {', '.join(score_quarters)}  →  评估: {', '.join(eval_quarters)}")
        print("=" * 72)
        print(f"  {'维度':<18} {'IC':>8} {'命中率':>8} "
              f"{'Q1收益':>9} {'Q5收益':>9} {'Q1-Q5':>9}")
        print(f"  {'─' * 18} {'─' * 8} {'─' * 8} "
              f"{'─' * 9} {'─' * 9} {'─' * 9}")
        summary_rows.sort(key=lambda r: r["ic"] if r["ic"] == r["ic"] else -999, reverse=True)
        for r in summary_rows:
            print(f"  {r['score_key']:<18} "
                  f"{r['ic']:>+8.4f} "
                  f"{r['hit_rate'] * 100:>7.1f}% "
                  f"{r['q1_return'] * 100:>+8.1f}% "
                  f"{r['q5_return'] * 100:>+8.1f}% "
                  f"{r['spread'] * 100:>+8.1f}%")
        print("=" * 72)
        return

    # ── 单次回测 ──
    result = run_backtest(
        score_quarters, eval_quarters,
        score_key=args.score_key, agg_mode=args.agg_mode,
        top_n=args.top_n,
        include_veto=args.include_veto,
        auto_score=(not args.no_auto_score),
        fresh_score=args.fresh_score,
    )

    # 输出报告
    print_report(result)

    if not args.no_save and result["rows"]:
        rpt_path, csv_path = save_report_files(result, output_dir)
        print(f"[输出] 报告: {rpt_path}")
        print(f"[输出] 数据: {csv_path}")


if __name__ == "__main__":
    main()
