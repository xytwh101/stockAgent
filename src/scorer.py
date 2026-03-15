"""
scorer.py — 打分引擎

包含：
- FilterFunnel:      三道过滤漏斗（6000 → ~1500）
- VetoEngine:        一票否决项检查（v1）
- VetoEngineV2:      一票否决项检查（v2，更严格）
- DimensionScorer:   五维打分 v1（0-10，绝对阈值法）
- DimensionScorerV2: 五维打分 v2（一致性优先+FCF视角+轻资产偏好）
- MasterScorer:      应用大师权重，输出最终得分（支持 v1/v2）

V2 核心改进（基于巴菲特致股东信 + 段永平投资问答录）：
  1. 一致性优先：连续多年高 ROE/ROIC 比均值更重要
  2. FCF 视角：Owner's Earnings 理念，现金流质量为核心
  3. 轻资产偏好：资本密集型业务天然降分
  4. 护城河代理：毛利率稳定性 + 定价权（利润率趋势）
  5. 估值弱化：避免过度偏好低 PE 的价值陷阱
  6. 成长质量：有利润支撑的成长 > 纯营收成长

打分逻辑与数据获取完全解耦，所有输入来自 NormalizedFinancials
"""
from __future__ import annotations

from typing import Optional

from config import (
    BUSINESS_QUALITY_THRESHOLDS,
    FINANCIAL_HEALTH_THRESHOLDS,
    FUNNEL_CONFIG,
    GROWTH_THRESHOLDS,
    MANAGEMENT_THRESHOLDS,
    MASTER_WEIGHTS,
    VETO_CONDITIONS,
)
from src.normalizer import NormalizedFinancials


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数：阈值区间打分
# ─────────────────────────────────────────────────────────────────────────────

def _threshold_score(value: Optional[float], thresholds: list[tuple]) -> float:
    """
    thresholds: [(min_value, score), ...] 降序排列
    value >= min_value → 返回对应 score
    thresholds 最后一项通常是 (float("-inf"), 0)
    """
    if value is None:
        return 0.0
    for threshold, score in thresholds:
        if value >= threshold:
            return float(score)
    return 0.0


def _inverse_threshold_score(value: Optional[float], thresholds: list[tuple]) -> float:
    """
    对越小越好的指标（如 CV、PE）使用升序阈值
    thresholds: [(max_value, score), ...] 升序排列
    value <= max_value → 返回对应 score
    """
    if value is None:
        return 0.0
    for threshold, score in thresholds:
        if value <= threshold:
            return float(score)
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 一票否决引擎
# ─────────────────────────────────────────────────────────────────────────────

class VetoEngine:
    def check(self, fin: NormalizedFinancials) -> list[str]:
        """返回触发的否决项列表，空列表 = 无否决"""
        triggers = []

        # 1. 过去N年自由现金流持续为负
        fcf_neg_years = VETO_CONDITIONS["fcf_negative_years"]
        fcf = fin.free_cash_flow[:fcf_neg_years]
        if len(fcf) >= fcf_neg_years and all((v or 0) < 0 for v in fcf):
            triggers.append(f"FCF连续{fcf_neg_years}年为负")

        # 2. 净利润率 < 0 且连续N年
        loss_years = VETO_CONDITIONS["net_loss_consecutive_years"]
        margins = fin.net_margin[:loss_years]
        if len(margins) >= loss_years and all((v or 0) < 0 for v in margins):
            triggers.append(f"净利润率连续{loss_years}年为负")

        # 3. 长期负债/FCF > 10
        if fin.total_debt and fin.free_cash_flow:
            latest_debt = fin.total_debt[0] or 0
            latest_fcf = fin.free_cash_flow[0] or 0
            if latest_fcf > 0 and latest_debt / latest_fcf > VETO_CONDITIONS["debt_to_fcf_max"]:
                triggers.append(f"长期负债/FCF > {VETO_CONDITIONS['debt_to_fcf_max']}")

        return triggers


# ─────────────────────────────────────────────────────────────────────────────
# 三道过滤漏斗
# ─────────────────────────────────────────────────────────────────────────────

class FilterFunnel:
    def __init__(self, fetcher):
        self.fetcher = fetcher

    def stage1_filter(self, tickers: list[str]) -> list[str]:
        """
        第一道：基础过滤（退市状态/市值/日均成交/上市年限）
        数据来源：profile（缓存30天），快速，API调用少
        """
        from datetime import datetime
        passed = []
        min_mktcap = FUNNEL_CONFIG["stage1_min_market_cap_usd"]
        min_volume = FUNNEL_CONFIG["stage1_min_avg_daily_volume_usd"]
        min_years = FUNNEL_CONFIG["stage1_min_listing_years"]

        for ticker in tickers:
            try:
                profile = self.fetcher.get_profile(ticker)
                if not profile:
                    continue

                # 退市过滤（零额外 API 调用，profile 已在此获取）：
                #   1. isActivelyTrading = False — 官方退市标记
                #   2. price = 0               — 没有报价，实质停止交易
                # 注意：不用 `not profile.get("isActivelyTrading", True)`，
                # 当字段值为 null（Python None）时，not None = True 会误杀正常股票。
                is_active = profile.get("isActivelyTrading")
                if is_active is not None and not bool(is_active):
                    continue
                price = float(profile.get("price") or 0)
                if price == 0:
                    continue

                mktcap = float(profile.get("marketCap", profile.get("mktCap")) or 0)
                vol_avg = float(profile.get("averageVolume", profile.get("volAvg")) or 0)
                avg_vol_usd = vol_avg * price

                # 市值过滤
                if mktcap < min_mktcap:
                    continue

                # 日均成交额过滤
                if avg_vol_usd < min_volume:
                    continue

                # 上市年限过滤
                ipo_date = profile.get("ipoDate", "")
                if ipo_date:
                    try:
                        ipo_year = datetime.fromisoformat(ipo_date).year
                        if (datetime.now().year - ipo_year) < min_years:
                            continue
                    except ValueError:
                        pass

                passed.append(ticker)
            except Exception as e:
                print(f"[Stage1] {ticker} 过滤失败: {e}")
                continue

        print(f"[漏斗] Stage1: {len(tickers)} → {len(passed)}")
        return passed

    def stage2_filter(self, tickers: list[str]) -> list[str]:
        """
        第二道：财务质量筛选（盈利年数/负债率）
        数据来源：income statement + balance sheet（缓存90天）
        """
        from src.normalizer import Normalizer
        normalizer = Normalizer()
        passed = []
        min_profitable = FUNNEL_CONFIG["stage2_min_profitable_years"]
        max_debt_ratio = FUNNEL_CONFIG["stage2_max_debt_to_assets"]
        lookback = FUNNEL_CONFIG["stage2_lookback_years"]

        for ticker in tickers:
            try:
                raw = self.fetcher.get_all_financial_data(ticker)
                fin = normalizer.normalize(raw)

                # 至少N年数据
                if fin.years_of_data < lookback:
                    continue

                # 过去N年至少 min_profitable 年盈利
                profitable_years = sum(
                    1 for ni in fin.net_income[:lookback] if (ni or 0) > 0
                )
                if profitable_years < min_profitable:
                    continue

                # 总负债/总资产
                if fin.total_assets and fin.total_debt:
                    ta = fin.total_assets[0] or 1
                    td = fin.total_debt[0] or 0
                    if ta > 0 and td / ta > max_debt_ratio:
                        continue

                passed.append(ticker)
            except Exception as e:
                print(f"[Stage2] {ticker} 过滤失败: {e}")
                continue

        print(f"[漏斗] Stage2: {len(tickers)} → {len(passed)}")
        return passed

    def run(self, tickers: list[str]) -> list[str]:
        """依次执行三道过滤，返回候选股票池"""
        after_s1 = self.stage1_filter(tickers)
        after_s2 = self.stage2_filter(after_s1)
        print(f"[漏斗] 最终候选池: {len(after_s2)} 只")
        return after_s2


# ─────────────────────────────────────────────────────────────────────────────
# 五维打分引擎
# ─────────────────────────────────────────────────────────────────────────────

class DimensionScorer:
    """对单只股票打五维分（0-10），不涉及大师权重"""

    def score_business_quality(self, fin: NormalizedFinancials) -> float:
        """
        生意质量：护城河、毛利率稳定性、ROIC、定价权
        巴菲特/芒格最重视的维度
        """
        t = BUSINESS_QUALITY_THRESHOLDS
        scores = []

        # ROE 5年均值
        if fin.roe_5y_avg is not None:
            scores.append(_threshold_score(fin.roe_5y_avg, t["roe"]))

        # 毛利率均值
        gm_avg = self._avg(fin.gross_margin[:5])
        if gm_avg is not None:
            scores.append(_threshold_score(gm_avg, t["gross_margin"]))

        # 毛利率稳定性（CV 越小越好）
        if fin.gross_margin_cv is not None:
            scores.append(_inverse_threshold_score(fin.gross_margin_cv, [
                (0.05, 10), (0.10, 8), (0.20, 6), (0.30, 4), (0.50, 2)
            ]))

        # 资本支出/净利润（轻资产）
        capex_ratio_avg = self._avg(fin.capex_to_net_income[:5])
        if capex_ratio_avg is not None:
            scores.append(_inverse_threshold_score(capex_ratio_avg, [
                (0.10, 10), (0.25, 8), (0.50, 6), (0.75, 4), (1.0, 2)
            ]))

        # FCF 利润质量
        fcf_quality_avg = self._avg(fin.fcf_to_net_income[:5])
        if fcf_quality_avg is not None:
            scores.append(_threshold_score(fcf_quality_avg, t["fcf_to_net_income"]))

        return self._avg(scores) or 0.0

    def score_financial_health(self, fin: NormalizedFinancials) -> float:
        """财务健康：偿债能力、流动性、杠杆"""
        t = FINANCIAL_HEALTH_THRESHOLDS
        scores = []

        cr = fin.current_ratio[0] if fin.current_ratio else None
        de = fin.debt_to_equity[0] if fin.debt_to_equity else None
        ic = fin.interest_coverage[0] if fin.interest_coverage else None
        otd = fin.ocf_to_debt[0] if fin.ocf_to_debt else None

        if cr is not None:
            scores.append(_threshold_score(cr, t["current_ratio"]))
        if de is not None:
            scores.append(_inverse_threshold_score(de, [
                (0.3, 10), (0.5, 8), (1.0, 6), (2.0, 4), (3.0, 2)
            ]))
        if ic is not None:
            scores.append(_threshold_score(ic, t["interest_coverage"]))
        if otd is not None:
            scores.append(_threshold_score(otd, t["ocf_to_debt"]))

        return self._avg(scores) or 0.0

    def score_growth(self, fin: NormalizedFinancials) -> float:
        """成长性：营收/利润/FCF/EPS增速"""
        t = GROWTH_THRESHOLDS
        scores = []

        if fin.revenue_cagr_5y is not None:
            scores.append(_threshold_score(fin.revenue_cagr_5y, t["revenue_cagr_5y"]))
        if fin.net_income_cagr_5y is not None:
            scores.append(_threshold_score(fin.net_income_cagr_5y, t["net_income_cagr_5y"]))
        if fin.fcf_cagr_3y is not None:
            scores.append(_threshold_score(fin.fcf_cagr_3y, t["fcf_cagr_3y"]))
        if fin.eps_cagr_5y is not None:
            scores.append(_threshold_score(fin.eps_cagr_5y, t["eps_cagr_5y"]))

        return self._avg(scores) or 0.0

    def score_management(self, fin: NormalizedFinancials) -> float:
        """管理层：ROIC、资本配置、股权稀释、回购"""
        t = MANAGEMENT_THRESHOLDS
        scores = []

        if fin.roic_5y_avg is not None:
            scores.append(_threshold_score(fin.roic_5y_avg, t["roic"]))

        if fin.dilution_rate_3y is not None:
            scores.append(_inverse_threshold_score(fin.dilution_rate_3y, [
                (0.00, 10), (0.01, 8), (0.02, 6), (0.03, 4), (0.05, 2)
            ]))

        if fin.buyback_yield_3y is not None:
            scores.append(_threshold_score(fin.buyback_yield_3y, t["buyback_yield"]))

        fcf_conv_avg = self._avg(fin.fcf_to_net_income[:3])
        if fcf_conv_avg is not None:
            scores.append(_threshold_score(fcf_conv_avg, t["fcf_conversion"]))

        return self._avg(scores) or 0.0

    def score_valuation(self, fin: NormalizedFinancials) -> float:
        """估值：PEG / PE / EV-EBITDA / PB"""
        scores = []

        if fin.peg_ratio is not None and fin.peg_ratio > 0:
            scores.append(_inverse_threshold_score(fin.peg_ratio, [
                (0.5, 10), (1.0, 8), (1.5, 6), (2.0, 4), (3.0, 2)
            ]))

        if fin.pe_ratio is not None and fin.pe_ratio > 0:
            scores.append(_inverse_threshold_score(fin.pe_ratio, [
                (10, 10), (15, 8), (20, 6), (25, 4), (35, 2)
            ]))

        if fin.ev_to_ebitda is not None and fin.ev_to_ebitda > 0:
            scores.append(_inverse_threshold_score(fin.ev_to_ebitda, [
                (8, 10), (12, 8), (16, 6), (20, 4), (30, 2)
            ]))

        if fin.pb_ratio is not None and fin.pb_ratio > 0:
            scores.append(_inverse_threshold_score(fin.pb_ratio, [
                (1.0, 10), (2.0, 8), (3.0, 6), (5.0, 4), (10.0, 2)
            ]))

        return self._avg(scores) or 0.0

    def score_all(self, fin: NormalizedFinancials) -> dict[str, float]:
        return {
            "business_quality": self.score_business_quality(fin),
            "financial_health": self.score_financial_health(fin),
            "growth":           self.score_growth(fin),
            "management":       self.score_management(fin),
            "valuation":        self.score_valuation(fin),
        }

    def score_all_explain(self, fin: NormalizedFinancials) -> dict:
        """返回每个维度的详细子指标拆解，供前端可视化"""
        t_bq = BUSINESS_QUALITY_THRESHOLDS
        t_fh = FINANCIAL_HEALTH_THRESHOLDS
        t_gr = GROWTH_THRESHOLDS
        t_mg = MANAGEMENT_THRESHOLDS

        def comp(name, value, display, score):
            return {"name": name, "value": value, "display": display, "score": round(score, 2)}

        # ── 生意质量 ──────────────────────────────────────────────
        bq = []
        if fin.roe_5y_avg is not None:
            s = _threshold_score(fin.roe_5y_avg, t_bq["roe"])
            bq.append(comp("ROE 5年均值", fin.roe_5y_avg, f"{fin.roe_5y_avg*100:.1f}%", s))
        gm_avg = self._avg(fin.gross_margin[:5])
        if gm_avg is not None:
            s = _threshold_score(gm_avg, t_bq["gross_margin"])
            bq.append(comp("毛利率均值", gm_avg, f"{gm_avg*100:.1f}%", s))
        if fin.gross_margin_cv is not None:
            s = _inverse_threshold_score(fin.gross_margin_cv, [(0.05,10),(0.10,8),(0.20,6),(0.30,4),(0.50,2)])
            bq.append(comp("毛利率稳定性(CV↓)", fin.gross_margin_cv, f"{fin.gross_margin_cv:.3f}", s))
        cx_avg = self._avg(fin.capex_to_net_income[:5])
        if cx_avg is not None:
            s = _inverse_threshold_score(cx_avg, [(0.10,10),(0.25,8),(0.50,6),(0.75,4),(1.0,2)])
            bq.append(comp("资本支出/净利润(↓)", cx_avg, f"{cx_avg:.2f}x", s))
        fq_avg = self._avg(fin.fcf_to_net_income[:5])
        if fq_avg is not None:
            s = _threshold_score(fq_avg, t_bq["fcf_to_net_income"])
            bq.append(comp("FCF/净利润", fq_avg, f"{fq_avg:.2f}x", s))

        # ── 财务健康 ──────────────────────────────────────────────
        fh = []
        cr = fin.current_ratio[0] if fin.current_ratio else None
        if cr is not None:
            fh.append(comp("流动比率", cr, f"{cr:.2f}", _threshold_score(cr, t_fh["current_ratio"])))
        de = fin.debt_to_equity[0] if fin.debt_to_equity else None
        if de is not None:
            s = _inverse_threshold_score(de, [(0.3,10),(0.5,8),(1.0,6),(2.0,4),(3.0,2)])
            fh.append(comp("负债/权益(↓)", de, f"{de:.2f}", s))
        ic = fin.interest_coverage[0] if fin.interest_coverage else None
        if ic is not None:
            fh.append(comp("利息覆盖倍数", ic, f"{ic:.1f}x", _threshold_score(ic, t_fh["interest_coverage"])))
        otd = fin.ocf_to_debt[0] if fin.ocf_to_debt else None
        if otd is not None:
            fh.append(comp("经营现金流/总负债", otd, f"{otd:.2f}", _threshold_score(otd, t_fh["ocf_to_debt"])))

        # ── 成长性 ────────────────────────────────────────────────
        gr = []
        if fin.revenue_cagr_5y is not None:
            gr.append(comp("营收CAGR 5年", fin.revenue_cagr_5y, f"{fin.revenue_cagr_5y*100:.1f}%",
                           _threshold_score(fin.revenue_cagr_5y, t_gr["revenue_cagr_5y"])))
        if fin.net_income_cagr_5y is not None:
            gr.append(comp("净利润CAGR 5年", fin.net_income_cagr_5y, f"{fin.net_income_cagr_5y*100:.1f}%",
                           _threshold_score(fin.net_income_cagr_5y, t_gr["net_income_cagr_5y"])))
        if fin.fcf_cagr_3y is not None:
            gr.append(comp("FCF CAGR 3年", fin.fcf_cagr_3y, f"{fin.fcf_cagr_3y*100:.1f}%",
                           _threshold_score(fin.fcf_cagr_3y, t_gr["fcf_cagr_3y"])))
        if fin.eps_cagr_5y is not None:
            gr.append(comp("EPS CAGR 5年", fin.eps_cagr_5y, f"{fin.eps_cagr_5y*100:.1f}%",
                           _threshold_score(fin.eps_cagr_5y, t_gr["eps_cagr_5y"])))

        # ── 管理层 ────────────────────────────────────────────────
        mg = []
        if fin.roic_5y_avg is not None:
            mg.append(comp("ROIC 5年均值", fin.roic_5y_avg, f"{fin.roic_5y_avg*100:.1f}%",
                           _threshold_score(fin.roic_5y_avg, t_mg["roic"])))
        if fin.dilution_rate_3y is not None:
            s = _inverse_threshold_score(fin.dilution_rate_3y, [(0.00,10),(0.01,8),(0.02,6),(0.03,4),(0.05,2)])
            mg.append(comp("股权稀释率 3年(↓)", fin.dilution_rate_3y, f"{fin.dilution_rate_3y*100:.2f}%", s))
        if fin.buyback_yield_3y is not None:
            mg.append(comp("回购收益率 3年", fin.buyback_yield_3y, f"{fin.buyback_yield_3y*100:.2f}%",
                           _threshold_score(fin.buyback_yield_3y, t_mg["buyback_yield"])))
        fc_avg = self._avg(fin.fcf_to_net_income[:3])
        if fc_avg is not None:
            mg.append(comp("FCF转化率 3年", fc_avg, f"{fc_avg:.2f}x",
                           _threshold_score(fc_avg, t_mg["fcf_conversion"])))

        # ── 估值 ──────────────────────────────────────────────────
        vl = []
        if fin.peg_ratio is not None and fin.peg_ratio > 0:
            s = _inverse_threshold_score(fin.peg_ratio, [(0.5,10),(1.0,8),(1.5,6),(2.0,4),(3.0,2)])
            vl.append(comp("PEG(↓)", fin.peg_ratio, f"{fin.peg_ratio:.2f}", s))
        if fin.pe_ratio is not None and fin.pe_ratio > 0:
            s = _inverse_threshold_score(fin.pe_ratio, [(10,10),(15,8),(20,6),(25,4),(35,2)])
            vl.append(comp("市盈率 PE(↓)", fin.pe_ratio, f"{fin.pe_ratio:.1f}x", s))
        if fin.ev_to_ebitda is not None and fin.ev_to_ebitda > 0:
            s = _inverse_threshold_score(fin.ev_to_ebitda, [(8,10),(12,8),(16,6),(20,4),(30,2)])
            vl.append(comp("EV/EBITDA(↓)", fin.ev_to_ebitda, f"{fin.ev_to_ebitda:.1f}x", s))
        if fin.pb_ratio is not None and fin.pb_ratio > 0:
            s = _inverse_threshold_score(fin.pb_ratio, [(1.0,10),(2.0,8),(3.0,6),(5.0,4),(10.0,2)])
            vl.append(comp("市净率 PB(↓)", fin.pb_ratio, f"{fin.pb_ratio:.1f}x", s))

        def dim_result(components):
            scores = [c["score"] for c in components]
            return {"score": round(self._avg(scores) or 0, 3), "components": components}

        return {
            "business_quality": dim_result(bq),
            "financial_health": dim_result(fh),
            "growth":           dim_result(gr),
            "management":       dim_result(mg),
            "valuation":        dim_result(vl),
        }

    def _avg(self, values: list) -> Optional[float]:
        valid = [v for v in values if v is not None]
        return sum(valid) / len(valid) if valid else None


# ─────────────────────────────────────────────────────────────────────────────
# 大师权重应用器
# ─────────────────────────────────────────────────────────────────────────────

class MasterScorer:
    """将五维分按各大师权重加权汇总，支持 v1/v2"""

    def __init__(self, version: str = "v1"):
        self.version = version
        if version == "v2":
            from config import V2_MASTER_WEIGHTS
            self._weights = V2_MASTER_WEIGHTS
        else:
            self._weights = MASTER_WEIGHTS

    def score(self, dim_scores: dict[str, float], master: str) -> float:
        weights = self._weights.get(master)
        if not weights:
            raise ValueError(f"未知大师: {master}")
        total = sum(weights[dim] * dim_scores.get(dim, 0) for dim in weights)
        return round(total, 4)

    def score_all_masters(self, dim_scores: dict[str, float]) -> dict[str, float]:
        return {
            master: self.score(dim_scores, master)
            for master in self._weights
        }


# ═════════════════════════════════════════════════════════════════════════════
# V2 打分引擎 — 基于巴菲特致股东信 + 段永平投资问答录
# ═════════════════════════════════════════════════════════════════════════════

class VetoEngineV2:
    """V2 一票否决引擎（更严格）

    新增否决项：
    - 5年均ROE低于8%（资本配置差，无法跑赢资本成本）
    - 毛利率3年下滑超15pct（失去定价权/护城河崩塌）
    """

    def __init__(self):
        from config import V2_VETO_CONDITIONS
        self.cond = V2_VETO_CONDITIONS

    def check(self, fin: NormalizedFinancials) -> list[str]:
        triggers = []

        # 1. 连续N年FCF为负（v2缩到2年）
        n = self.cond["fcf_negative_years"]
        fcf = fin.free_cash_flow[:n]
        if len(fcf) >= n and all((v or 0) < 0 for v in fcf):
            triggers.append(f"FCF连续{n}年为负")

        # 2. 净利润率连续N年为负
        n2 = self.cond["net_loss_consecutive_years"]
        margins = fin.net_margin[:n2]
        if len(margins) >= n2 and all((v or 0) < 0 for v in margins):
            triggers.append(f"净利润率连续{n2}年为负")

        # 3. 长期负债/FCF > 阈值
        if fin.total_debt and fin.free_cash_flow:
            latest_debt = fin.total_debt[0] or 0
            latest_fcf = fin.free_cash_flow[0] or 0
            if latest_fcf > 0 and latest_debt / latest_fcf > self.cond["debt_to_fcf_max"]:
                triggers.append(f"长期负债/FCF > {self.cond['debt_to_fcf_max']}")

        # 4. 新增：5年均ROE低于资本成本（8%）
        if fin.roe_5y_avg is not None and fin.roe_5y_avg < self.cond["roe_below_cost_of_capital"]:
            triggers.append(f"ROE 5年均值 {fin.roe_5y_avg:.1%} < {self.cond['roe_below_cost_of_capital']:.0%}")

        # 5. 新增：毛利率大幅下滑（定价权丧失）
        gm = [v for v in fin.gross_margin[:5] if v is not None]
        if len(gm) >= 4:
            recent = sum(gm[:2]) / 2
            older = sum(gm[-2:]) / 2
            if older > 0 and (recent - older) < self.cond["gross_margin_collapse"]:
                triggers.append(f"毛利率大幅下滑 {(recent - older):.1%}")

        return triggers


class DimensionScorerV2:
    """V2 五维打分引擎

    核心理念：
    - 巴菲特："demonstrated consistent earning power" + "good ROE with little debt"
    - 段永平："好生意模式" + "产品差异化" + "10年后还在" + "轻资产"

    五大维度：
    1. 生意质量(35%)：ROE一致性 + 毛利率稳定 + FCF/营收 + 轻资产
    2. 财务健康(20%)：净负债/FCF + 流动性 + 利息覆盖 + OCF持续性
    3. 成长质量(15%)：CAGR + 成长一致性 + 利润率趋势
    4. 资本配置(20%)：ROIC一致性 + 股东回报 + 低稀释 + FCF转化
    5. 估值(10%)：FCF收益率 + 盈利收益率 + PEG + EV/EBITDA
    """

    def __init__(self):
        from config import (
            V2_BUSINESS_QUALITY,
            V2_FINANCIAL_HEALTH,
            V2_GROWTH_QUALITY,
            V2_CAPITAL_ALLOCATION,
            V2_VALUATION,
        )
        self.t_bq = V2_BUSINESS_QUALITY
        self.t_fh = V2_FINANCIAL_HEALTH
        self.t_gr = V2_GROWTH_QUALITY
        self.t_ca = V2_CAPITAL_ALLOCATION
        self.t_vl = V2_VALUATION

    def score_business_quality(self, fin: NormalizedFinancials) -> float:
        """生意质量：护城河宽度 + 现金流品质 + 轻资产偏好"""
        scores = []

        # ROE 一致性：5年中有几年 > 15%
        roe_vals = [v for v in fin.roe[:5] if v is not None]
        if roe_vals:
            roe_above_15 = sum(1 for v in roe_vals if v > 0.15)
            scores.append(_threshold_score(roe_above_15, self.t_bq["roe_consistency"]))

        # ROE 5年均值（辅助）
        if fin.roe_5y_avg is not None:
            scores.append(_threshold_score(fin.roe_5y_avg, self.t_bq["roe_avg"]))

        # 毛利率均值
        gm_avg = self._avg(fin.gross_margin[:5])
        if gm_avg is not None:
            scores.append(_threshold_score(gm_avg, self.t_bq["gross_margin"]))

        # 毛利率稳定性 CV
        if fin.gross_margin_cv is not None:
            scores.append(_inverse_threshold_score(fin.gross_margin_cv, self.t_bq["gross_margin_cv"]))

        # FCF/营收（Owner's Earnings 视角）
        fcf_rev = self._fcf_to_revenue(fin)
        if fcf_rev is not None:
            scores.append(_threshold_score(fcf_rev, self.t_bq["fcf_to_revenue"]))

        # 轻资产度：Capex/营收
        capex_rev = self._capex_to_revenue(fin)
        if capex_rev is not None:
            scores.append(_inverse_threshold_score(capex_rev, self.t_bq["capex_to_revenue"]))

        return self._avg(scores) or 0.0

    def score_financial_health(self, fin: NormalizedFinancials) -> float:
        """财务健康：真实偿债能力 + 流动性 + 现金流持续性"""
        scores = []

        # 净负债/FCF（比 D/E 更有意义）
        net_debt_fcf = self._net_debt_to_fcf(fin)
        if net_debt_fcf is not None:
            scores.append(_inverse_threshold_score(net_debt_fcf, self.t_fh["net_debt_to_fcf"]))

        # 流动比率
        cr = fin.current_ratio[0] if fin.current_ratio else None
        if cr is not None:
            scores.append(_threshold_score(cr, self.t_fh["current_ratio"]))

        # 利息覆盖倍数
        ic = fin.interest_coverage[0] if fin.interest_coverage else None
        if ic is not None:
            scores.append(_threshold_score(ic, self.t_fh["interest_coverage"]))

        # 经营现金流持续为正（5年中几年为正）
        ocf = fin.operating_cash_flow[:5]
        if ocf:
            positive_years = sum(1 for v in ocf if (v or 0) > 0)
            scores.append(_threshold_score(positive_years, self.t_fh["ocf_positive_years"]))

        return self._avg(scores) or 0.0

    def score_growth(self, fin: NormalizedFinancials) -> float:
        """成长质量：有利润支撑的稳定成长"""
        scores = []

        # 营收 CAGR 5年
        if fin.revenue_cagr_5y is not None:
            scores.append(_threshold_score(fin.revenue_cagr_5y, self.t_gr["revenue_cagr_5y"]))

        # EPS CAGR 5年
        if fin.eps_cagr_5y is not None:
            scores.append(_threshold_score(fin.eps_cagr_5y, self.t_gr["eps_cagr_5y"]))

        # 营收成长一致性
        rev = fin.revenue[:6]
        if len(rev) >= 2:
            growth_years = sum(
                1 for i in range(min(5, len(rev) - 1))
                if rev[i] is not None and rev[i+1] is not None and rev[i+1] > 0 and rev[i] > rev[i+1]
            )
            scores.append(_threshold_score(growth_years, self.t_gr["revenue_growth_consistency"]))

        # 利润率趋势（近3年 vs 前3年）
        margin_trend = self._margin_trend(fin)
        if margin_trend is not None:
            scores.append(_threshold_score(margin_trend, self.t_gr["margin_trend"]))

        return self._avg(scores) or 0.0

    def score_management(self, fin: NormalizedFinancials) -> float:
        """资本配置：ROIC一致性 + 股东回报 + 利润质量"""
        scores = []

        # ROIC 一致性：5年中有几年 > 12%
        roic_vals = [v for v in fin.roic[:5] if v is not None]
        if roic_vals:
            roic_above_12 = sum(1 for v in roic_vals if v > 0.12)
            scores.append(_threshold_score(roic_above_12, self.t_ca["roic_consistency"]))

        # ROIC 5年均值
        if fin.roic_5y_avg is not None:
            scores.append(_threshold_score(fin.roic_5y_avg, self.t_ca["roic_avg"]))

        # 股权稀释率（负值=回购=好）
        if fin.dilution_rate_3y is not None:
            scores.append(_inverse_threshold_score(fin.dilution_rate_3y, self.t_ca["dilution_rate"]))

        # 股东总回报率（回购+分红/市值）
        sy = self._shareholder_yield(fin)
        if sy is not None:
            scores.append(_threshold_score(sy, self.t_ca["shareholder_yield"]))

        # FCF转化率
        fcf_conv = self._avg(fin.fcf_to_net_income[:3])
        if fcf_conv is not None:
            scores.append(_threshold_score(fcf_conv, self.t_ca["fcf_conversion"]))

        return self._avg(scores) or 0.0

    def score_valuation(self, fin: NormalizedFinancials) -> float:
        """估值：FCF收益率优先，弱化绝对PE"""
        scores = []

        # FCF收益率（最重要）
        if fin.fcf_yield is not None and fin.fcf_yield > 0:
            scores.append(_threshold_score(fin.fcf_yield, self.t_vl["fcf_yield"]))

        # 盈利收益率 (1/PE)
        if fin.pe_ratio is not None and fin.pe_ratio > 0:
            ey = 1.0 / fin.pe_ratio
            scores.append(_threshold_score(ey, self.t_vl["earnings_yield"]))

        # PEG（仅当增长率为正时有效）
        if fin.peg_ratio is not None and 0 < fin.peg_ratio < 10:
            scores.append(_inverse_threshold_score(fin.peg_ratio, self.t_vl["peg_ratio"]))

        # EV/EBITDA（放宽阈值）
        if fin.ev_to_ebitda is not None and fin.ev_to_ebitda > 0:
            scores.append(_inverse_threshold_score(fin.ev_to_ebitda, self.t_vl["ev_to_ebitda"]))

        return self._avg(scores) or 0.0

    def score_all(self, fin: NormalizedFinancials) -> dict[str, float]:
        return {
            "business_quality": self.score_business_quality(fin),
            "financial_health": self.score_financial_health(fin),
            "growth":           self.score_growth(fin),
            "management":       self.score_management(fin),
            "valuation":        self.score_valuation(fin),
        }

    def score_all_explain(self, fin: NormalizedFinancials) -> dict:
        """返回每个维度的详细子指标拆解，供前端可视化"""

        def comp(name, value, display, score):
            return {"name": name, "value": value, "display": display, "score": round(score, 2)}

        # ── 生意质量 ──────────────────────────────────────────────
        bq = []
        roe_vals = [v for v in fin.roe[:5] if v is not None]
        if roe_vals:
            roe_above_15 = sum(1 for v in roe_vals if v > 0.15)
            s = _threshold_score(roe_above_15, self.t_bq["roe_consistency"])
            bq.append(comp("ROE>15%年数(5年)", roe_above_15, f"{roe_above_15}/5年", s))
        if fin.roe_5y_avg is not None:
            s = _threshold_score(fin.roe_5y_avg, self.t_bq["roe_avg"])
            bq.append(comp("ROE 5年均值", fin.roe_5y_avg, f"{fin.roe_5y_avg*100:.1f}%", s))
        gm_avg = self._avg(fin.gross_margin[:5])
        if gm_avg is not None:
            s = _threshold_score(gm_avg, self.t_bq["gross_margin"])
            bq.append(comp("毛利率均值", gm_avg, f"{gm_avg*100:.1f}%", s))
        if fin.gross_margin_cv is not None:
            s = _inverse_threshold_score(fin.gross_margin_cv, self.t_bq["gross_margin_cv"])
            bq.append(comp("毛利率稳定性(CV↓)", fin.gross_margin_cv, f"{fin.gross_margin_cv:.3f}", s))
        fcf_rev = self._fcf_to_revenue(fin)
        if fcf_rev is not None:
            s = _threshold_score(fcf_rev, self.t_bq["fcf_to_revenue"])
            bq.append(comp("FCF/营收", fcf_rev, f"{fcf_rev*100:.1f}%", s))
        capex_rev = self._capex_to_revenue(fin)
        if capex_rev is not None:
            s = _inverse_threshold_score(capex_rev, self.t_bq["capex_to_revenue"])
            bq.append(comp("Capex/营收(↓)", capex_rev, f"{capex_rev*100:.1f}%", s))

        # ── 财务健康 ──────────────────────────────────────────────
        fh = []
        net_debt_fcf = self._net_debt_to_fcf(fin)
        if net_debt_fcf is not None:
            s = _inverse_threshold_score(net_debt_fcf, self.t_fh["net_debt_to_fcf"])
            fh.append(comp("净负债/FCF(↓)", net_debt_fcf, f"{net_debt_fcf:.1f}x", s))
        cr = fin.current_ratio[0] if fin.current_ratio else None
        if cr is not None:
            fh.append(comp("流动比率", cr, f"{cr:.2f}", _threshold_score(cr, self.t_fh["current_ratio"])))
        ic = fin.interest_coverage[0] if fin.interest_coverage else None
        if ic is not None:
            fh.append(comp("利息覆盖倍数", ic, f"{ic:.1f}x", _threshold_score(ic, self.t_fh["interest_coverage"])))
        ocf = fin.operating_cash_flow[:5]
        if ocf:
            pos_y = sum(1 for v in ocf if (v or 0) > 0)
            s = _threshold_score(pos_y, self.t_fh["ocf_positive_years"])
            fh.append(comp("OCF为正年数(5年)", pos_y, f"{pos_y}/5年", s))

        # ── 成长质量 ────────────────────────────────────────────────
        gr = []
        if fin.revenue_cagr_5y is not None:
            gr.append(comp("营收CAGR 5年", fin.revenue_cagr_5y, f"{fin.revenue_cagr_5y*100:.1f}%",
                           _threshold_score(fin.revenue_cagr_5y, self.t_gr["revenue_cagr_5y"])))
        if fin.eps_cagr_5y is not None:
            gr.append(comp("EPS CAGR 5年", fin.eps_cagr_5y, f"{fin.eps_cagr_5y*100:.1f}%",
                           _threshold_score(fin.eps_cagr_5y, self.t_gr["eps_cagr_5y"])))
        rev = fin.revenue[:6]
        if len(rev) >= 2:
            gy = sum(
                1 for i in range(min(5, len(rev) - 1))
                if rev[i] is not None and rev[i+1] is not None and rev[i+1] > 0 and rev[i] > rev[i+1]
            )
            gr.append(comp("营收正增长年数", gy, f"{gy}/5年",
                           _threshold_score(gy, self.t_gr["revenue_growth_consistency"])))
        mt = self._margin_trend(fin)
        if mt is not None:
            gr.append(comp("净利率趋势", mt, f"{mt*100:+.1f}pct",
                           _threshold_score(mt, self.t_gr["margin_trend"])))

        # ── 资本配置 ────────────────────────────────────────────────
        mg = []
        roic_vals = [v for v in fin.roic[:5] if v is not None]
        if roic_vals:
            ra12 = sum(1 for v in roic_vals if v > 0.12)
            mg.append(comp("ROIC>12%年数(5年)", ra12, f"{ra12}/5年",
                           _threshold_score(ra12, self.t_ca["roic_consistency"])))
        if fin.roic_5y_avg is not None:
            mg.append(comp("ROIC 5年均值", fin.roic_5y_avg, f"{fin.roic_5y_avg*100:.1f}%",
                           _threshold_score(fin.roic_5y_avg, self.t_ca["roic_avg"])))
        if fin.dilution_rate_3y is not None:
            s = _inverse_threshold_score(fin.dilution_rate_3y, self.t_ca["dilution_rate"])
            mg.append(comp("股权稀释率(↓)", fin.dilution_rate_3y, f"{fin.dilution_rate_3y*100:.2f}%", s))
        sy = self._shareholder_yield(fin)
        if sy is not None:
            mg.append(comp("股东回报率", sy, f"{sy*100:.1f}%",
                           _threshold_score(sy, self.t_ca["shareholder_yield"])))
        fc_avg = self._avg(fin.fcf_to_net_income[:3])
        if fc_avg is not None:
            mg.append(comp("FCF转化率", fc_avg, f"{fc_avg:.2f}x",
                           _threshold_score(fc_avg, self.t_ca["fcf_conversion"])))

        # ── 估值 ──────────────────────────────────────────────────
        vl = []
        if fin.fcf_yield is not None and fin.fcf_yield > 0:
            vl.append(comp("FCF收益率", fin.fcf_yield, f"{fin.fcf_yield*100:.1f}%",
                           _threshold_score(fin.fcf_yield, self.t_vl["fcf_yield"])))
        if fin.pe_ratio is not None and fin.pe_ratio > 0:
            ey = 1.0 / fin.pe_ratio
            vl.append(comp("盈利收益率(1/PE)", ey, f"{ey*100:.1f}%",
                           _threshold_score(ey, self.t_vl["earnings_yield"])))
        if fin.peg_ratio is not None and 0 < fin.peg_ratio < 10:
            s = _inverse_threshold_score(fin.peg_ratio, self.t_vl["peg_ratio"])
            vl.append(comp("PEG(↓)", fin.peg_ratio, f"{fin.peg_ratio:.2f}", s))
        if fin.ev_to_ebitda is not None and fin.ev_to_ebitda > 0:
            s = _inverse_threshold_score(fin.ev_to_ebitda, self.t_vl["ev_to_ebitda"])
            vl.append(comp("EV/EBITDA(↓)", fin.ev_to_ebitda, f"{fin.ev_to_ebitda:.1f}x", s))

        def dim_result(components):
            s = [c["score"] for c in components]
            return {"score": round(self._avg(s) or 0, 3), "components": components}

        return {
            "business_quality": dim_result(bq),
            "financial_health": dim_result(fh),
            "growth":           dim_result(gr),
            "management":       dim_result(mg),
            "valuation":        dim_result(vl),
        }

    # ── 辅助计算方法 ──────────────────────────────────────────────

    def _fcf_to_revenue(self, fin: NormalizedFinancials) -> Optional[float]:
        """FCF/营收 5年均值"""
        ratios = []
        for i in range(min(5, len(fin.free_cash_flow), len(fin.revenue))):
            fcf = fin.free_cash_flow[i]
            rev = fin.revenue[i]
            if fcf is not None and rev is not None and rev > 0:
                ratios.append(fcf / rev)
        return self._avg(ratios)

    def _capex_to_revenue(self, fin: NormalizedFinancials) -> Optional[float]:
        """Capex/营收 5年均值"""
        ratios = []
        for i in range(min(5, len(fin.capex), len(fin.revenue))):
            capex = fin.capex[i]
            rev = fin.revenue[i]
            if capex is not None and rev is not None and rev > 0:
                ratios.append(abs(capex) / rev)
        return self._avg(ratios)

    def _net_debt_to_fcf(self, fin: NormalizedFinancials) -> Optional[float]:
        """净负债/FCF（负值=净现金头寸，非常好）"""
        if not fin.total_debt or not fin.cash or not fin.free_cash_flow:
            return None
        debt = fin.total_debt[0] or 0
        cash = fin.cash[0] or 0
        fcf = fin.free_cash_flow[0] or 0
        net_debt = debt - cash
        if net_debt <= 0:
            return 0.0  # 净现金 = 最好
        if fcf <= 0:
            return 20.0  # 有净负债但FCF为负 = 很差
        return net_debt / fcf

    def _margin_trend(self, fin: NormalizedFinancials) -> Optional[float]:
        """净利润率趋势：近2年均值 - 前2年均值"""
        nm = [v for v in fin.net_margin[:5] if v is not None]
        if len(nm) < 4:
            return None
        recent = sum(nm[:2]) / 2
        older = sum(nm[-2:]) / 2
        return recent - older

    def _shareholder_yield(self, fin: NormalizedFinancials) -> Optional[float]:
        """股东总回报率 = 回购收益率 + 分红收益率"""
        total = 0.0
        has_data = False
        if fin.buyback_yield_3y is not None:
            total += fin.buyback_yield_3y
            has_data = True
        # 估算分红率
        if (fin.dividends_paid and fin.market_cap and
                fin.dividends_paid[0] is not None and fin.market_cap and fin.market_cap > 0):
            div_yield = abs(fin.dividends_paid[0]) / fin.market_cap
            total += div_yield
            has_data = True
        return total if has_data else None

    def _avg(self, values: list) -> Optional[float]:
        valid = [v for v in values if v is not None]
        return sum(valid) / len(valid) if valid else None
