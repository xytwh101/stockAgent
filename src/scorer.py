"""
scorer.py — 打分引擎

包含：
- FilterFunnel:     三道过滤漏斗（6000 → ~1500）
- VetoEngine:       一票否决项检查
- DimensionScorer:  五维打分（0-10，绝对阈值法）
- MasterScorer:     应用大师权重，输出最终得分

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

                # 退市过滤：isActivelyTrading = False 表示已退市，零额外 API 调用
                if not profile.get("isActivelyTrading", True):
                    continue

                mktcap = float(profile.get("marketCap", profile.get("mktCap")) or 0)
                vol_avg = float(profile.get("averageVolume", profile.get("volAvg")) or 0)
                price = float(profile.get("price") or 0)
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
    """将五维分按各大师权重加权汇总"""

    def score(self, dim_scores: dict[str, float], master: str) -> float:
        weights = MASTER_WEIGHTS.get(master)
        if not weights:
            raise ValueError(f"未知大师: {master}")
        total = sum(weights[dim] * dim_scores.get(dim, 0) for dim in weights)
        return round(total, 4)

    def score_all_masters(self, dim_scores: dict[str, float]) -> dict[str, float]:
        return {
            master: self.score(dim_scores, master)
            for master in MASTER_WEIGHTS
        }
