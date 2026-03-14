"""
normalizer.py — 数据标准化层

职责：
- 将 FMP / yfinance 原始 API 响应 → 类型化的 NormalizedFinancials 数据类
- 计算所有衍生比率（ROE / ROIC / FCF / CAGR 等）
- 记录数据缺失标志（data_quality_flags）
- scorer.py 只消费这里输出的数据，不直接碰原始 API 字段
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class NormalizedFinancials:
    ticker: str

    # ── 损益表（列表，index 0 = 最新年度，降序）──────────────────
    revenue:          list[float] = field(default_factory=list)
    gross_profit:     list[float] = field(default_factory=list)
    operating_income: list[float] = field(default_factory=list)
    net_income:       list[float] = field(default_factory=list)
    ebitda:           list[float] = field(default_factory=list)
    eps:              list[float] = field(default_factory=list)

    # ── 资产负债表 ───────────────────────────────────────────────
    total_assets:            list[float] = field(default_factory=list)
    total_debt:              list[float] = field(default_factory=list)
    total_equity:            list[float] = field(default_factory=list)
    cash:                    list[float] = field(default_factory=list)
    current_assets:          list[float] = field(default_factory=list)
    current_liabilities:     list[float] = field(default_factory=list)
    shares_outstanding:      list[float] = field(default_factory=list)

    # ── 现金流量表 ───────────────────────────────────────────────
    operating_cash_flow: list[float] = field(default_factory=list)
    capex:               list[float] = field(default_factory=list)
    free_cash_flow:      list[float] = field(default_factory=list)
    dividends_paid:      list[float] = field(default_factory=list)

    # ── 衍生比率（每年，index 0 = 最新）─────────────────────────
    gross_margin:       list[float] = field(default_factory=list)
    operating_margin:   list[float] = field(default_factory=list)
    net_margin:         list[float] = field(default_factory=list)
    roe:                list[float] = field(default_factory=list)
    roa:                list[float] = field(default_factory=list)
    roic:               list[float] = field(default_factory=list)
    current_ratio:      list[float] = field(default_factory=list)
    debt_to_equity:     list[float] = field(default_factory=list)
    interest_coverage:  list[float] = field(default_factory=list)
    fcf_to_net_income:  list[float] = field(default_factory=list)
    capex_to_net_income: list[float] = field(default_factory=list)
    ocf_to_debt:        list[float] = field(default_factory=list)

    # ── 增长率（标量，基于CAGR计算）─────────────────────────────
    revenue_cagr_5y:    Optional[float] = None
    revenue_cagr_3y:    Optional[float] = None
    net_income_cagr_5y: Optional[float] = None
    eps_cagr_5y:        Optional[float] = None
    fcf_cagr_3y:        Optional[float] = None

    # ── 稳定性指标 ───────────────────────────────────────────────
    gross_margin_cv:    Optional[float] = None  # 变异系数（越低越稳定）
    roe_5y_avg:         Optional[float] = None
    roic_5y_avg:        Optional[float] = None
    dilution_rate_3y:   Optional[float] = None  # 年均股权稀释率
    buyback_yield_3y:   Optional[float] = None

    # ── 当前估值（标量）─────────────────────────────────────────
    market_cap:         Optional[float] = None
    pe_ratio:           Optional[float] = None
    pb_ratio:           Optional[float] = None
    ps_ratio:           Optional[float] = None
    ev_to_ebitda:       Optional[float] = None
    peg_ratio:          Optional[float] = None
    fcf_yield:          Optional[float] = None

    # ── 元数据 ───────────────────────────────────────────────────
    years_of_data:      int = 0
    sector:             str = ""
    industry:           str = ""
    company_name:       str = ""
    data_quality_flags: list[str] = field(default_factory=list)


class Normalizer:
    """将 fetcher.get_all_financial_data() 的原始 dict 转换为 NormalizedFinancials"""

    def normalize(self, raw: dict) -> NormalizedFinancials:
        ticker = raw.get("ticker", "")
        fin = NormalizedFinancials(ticker=ticker)

        # 基本信息
        profile = raw.get("profile", {})
        fin.company_name = profile.get("companyName", "")
        fin.sector = profile.get("sector", "")
        fin.industry = profile.get("industry", "")
        fin.market_cap = self._safe_float(profile.get("marketCap", profile.get("mktCap")))

        # 报表数据（新 stable API 键名）
        income_stmts = raw.get("income_annual", [])
        balance_sheets = raw.get("balance_annual", [])
        cf_stmts = raw.get("cashflow_annual", [])
        key_metrics = raw.get("key_metrics_annual", [])
        ratios_annual = raw.get("ratios_annual", [])

        # TTM 估值：合并 ratios_ttm + key_metrics_ttm 两个接口
        valuation = {**(raw.get("ratios_ttm") or {}), **(raw.get("key_metrics_ttm") or {})}

        self._parse_income(fin, income_stmts)
        self._parse_balance(fin, balance_sheets)
        self._parse_cashflow(fin, cf_stmts)
        self._parse_valuation(fin, valuation)
        self._compute_ratios(fin, ratios_annual)
        self._compute_growth(fin)
        self._compute_stability(fin)
        self._parse_key_metrics(fin, key_metrics)

        fin.years_of_data = len(fin.revenue)
        return fin

    # ─────────────────────────────────────────────
    # 解析损益表
    # ─────────────────────────────────────────────

    def _parse_income(self, fin: NormalizedFinancials, stmts: list[dict]):
        if not stmts:
            fin.data_quality_flags.append("missing_income_statement")
            return

        source = stmts[0].get("_source", "fmp")

        for stmt in stmts:
            if source == "yfinance":
                fin.revenue.append(self._safe_float(stmt.get("revenue"), "revenue", fin))
                fin.gross_profit.append(self._safe_float(stmt.get("grossProfit"), "gross_profit", fin))
                fin.operating_income.append(self._safe_float(stmt.get("operatingIncome"), "operating_income", fin))
                fin.net_income.append(self._safe_float(stmt.get("netIncome"), "net_income", fin))
                fin.ebitda.append(self._safe_float(stmt.get("ebitda"), "ebitda", fin))
                fin.eps.append(self._safe_float(stmt.get("eps"), "eps", fin))
                fin.shares_outstanding.append(self._safe_float(stmt.get("weightedAverageShsOutDil"), "shares_outstanding", fin))
            else:
                fin.revenue.append(self._safe_float(stmt.get("revenue"), "revenue", fin))
                fin.gross_profit.append(self._safe_float(stmt.get("grossProfit"), "gross_profit", fin))
                fin.operating_income.append(self._safe_float(stmt.get("operatingIncome"), "operating_income", fin))
                fin.net_income.append(self._safe_float(stmt.get("netIncome"), "net_income", fin))
                fin.ebitda.append(self._safe_float(stmt.get("ebitda"), "ebitda", fin))
                # 新 stable API 用 epsDiluted（大写D），旧 v3 用 epsdiluted
                fin.eps.append(self._safe_float(
                    stmt.get("epsDiluted", stmt.get("epsdiluted", stmt.get("eps"))), "eps", fin
                ))
                # 用加权稀释股数代替资产负债表的 commonStock（后者是美元金额）
                fin.shares_outstanding.append(self._safe_float(
                    stmt.get("weightedAverageShsOutDil", stmt.get("weightedAverageShsOut")),
                    "shares_outstanding", fin
                ))

    # ─────────────────────────────────────────────
    # 解析资产负债表
    # ─────────────────────────────────────────────

    def _parse_balance(self, fin: NormalizedFinancials, stmts: list[dict]):
        if not stmts:
            fin.data_quality_flags.append("missing_balance_sheet")
            return

        for stmt in stmts:
            fin.total_assets.append(self._safe_float(stmt.get("totalAssets"), "total_assets", fin))
            fin.total_debt.append(self._safe_float(stmt.get("totalDebt", stmt.get("longTermDebt")), "total_debt", fin))
            fin.total_equity.append(self._safe_float(
                stmt.get("totalStockholdersEquity", stmt.get("totalEquity")), "total_equity", fin
            ))
            fin.cash.append(self._safe_float(stmt.get("cashAndCashEquivalents"), "cash", fin))
            fin.current_assets.append(self._safe_float(stmt.get("totalCurrentAssets"), "current_assets", fin))
            fin.current_liabilities.append(self._safe_float(stmt.get("totalCurrentLiabilities"), "current_liabilities", fin))

    # ─────────────────────────────────────────────
    # 解析现金流量表
    # ─────────────────────────────────────────────

    def _parse_cashflow(self, fin: NormalizedFinancials, stmts: list[dict]):
        if not stmts:
            fin.data_quality_flags.append("missing_cashflow_statement")
            return

        for stmt in stmts:
            ocf = self._safe_float(stmt.get("operatingCashFlow"), "operating_cash_flow", fin)
            capex = self._safe_float(stmt.get("capitalExpenditure"), "capex", fin)
            fcf = self._safe_float(stmt.get("freeCashFlow"))

            fin.operating_cash_flow.append(ocf)
            fin.capex.append(capex)
            # 若没有 FCF 字段，自己算
            if fcf is None and ocf is not None and capex is not None:
                fcf = ocf + capex  # FMP capex 为负值
            fin.free_cash_flow.append(fcf if fcf is not None else 0.0)
            fin.dividends_paid.append(self._safe_float(stmt.get("dividendsPaid"), "dividends_paid", fin))

    # ─────────────────────────────────────────────
    # 解析估值数据（TTM）
    # ─────────────────────────────────────────────

    def _parse_valuation(self, fin: NormalizedFinancials, val: dict):
        if not val:
            fin.data_quality_flags.append("missing_valuation_ttm")
            return

        # 新 stable API 字段（ratios-ttm）
        fin.pe_ratio = self._safe_float(val.get("priceToEarningsRatioTTM"))
        fin.pb_ratio = self._safe_float(val.get("priceToBookRatioTTM"))
        fin.ps_ratio = self._safe_float(val.get("priceToSalesRatioTTM"))
        fin.peg_ratio = self._safe_float(val.get("priceToEarningsGrowthRatioTTM"))
        # EV/EBITDA 和 FCF Yield 来自 key-metrics-ttm
        fin.ev_to_ebitda = self._safe_float(val.get("evToEBITDATTM"))
        fin.fcf_yield = self._safe_float(val.get("freeCashFlowYieldTTM"))

    # ─────────────────────────────────────────────
    # 解析 key metrics（ROIC 等）
    # ─────────────────────────────────────────────

    def _parse_key_metrics(self, fin: NormalizedFinancials, metrics: list[dict]):
        if not metrics:
            return

        roic_values = []
        for m in metrics:
            # 新 stable API: returnOnInvestedCapital（旧 v3: roic）
            v = self._safe_float(m.get("returnOnInvestedCapital", m.get("roic")))
            if v is not None:
                roic_values.append(v)

        if roic_values:
            fin.roic = roic_values
            fin.roic_5y_avg = sum(roic_values[:5]) / len(roic_values[:5])

        # buyback yield
        buyback_values = []
        for m in metrics[:3]:
            v = self._safe_float(m.get("buybackYield"))
            if v is not None:
                buyback_values.append(v)
        if buyback_values:
            fin.buyback_yield_3y = sum(buyback_values) / len(buyback_values)

    # ─────────────────────────────────────────────
    # 计算衍生比率
    # ─────────────────────────────────────────────

    def _compute_ratios(self, fin: NormalizedFinancials, ratios_annual: list[dict] = None):
        n = len(fin.revenue)
        for i in range(n):
            rev = fin.revenue[i] if i < len(fin.revenue) else None
            gp = fin.gross_profit[i] if i < len(fin.gross_profit) else None
            oi = fin.operating_income[i] if i < len(fin.operating_income) else None
            ni = fin.net_income[i] if i < len(fin.net_income) else None
            ta = fin.total_assets[i] if i < len(fin.total_assets) else None
            td = fin.total_debt[i] if i < len(fin.total_debt) else None
            te = fin.total_equity[i] if i < len(fin.total_equity) else None
            ca = fin.current_assets[i] if i < len(fin.current_assets) else None
            cl = fin.current_liabilities[i] if i < len(fin.current_liabilities) else None
            ocf = fin.operating_cash_flow[i] if i < len(fin.operating_cash_flow) else None
            fcf = fin.free_cash_flow[i] if i < len(fin.free_cash_flow) else None
            capex = fin.capex[i] if i < len(fin.capex) else None

            fin.gross_margin.append(self._div(gp, rev))
            fin.operating_margin.append(self._div(oi, rev))
            fin.net_margin.append(self._div(ni, rev))
            fin.roe.append(self._div(ni, te))
            fin.roa.append(self._div(ni, ta))
            fin.current_ratio.append(self._div(ca, cl))
            fin.debt_to_equity.append(self._div(td, te))
            fin.fcf_to_net_income.append(self._div(fcf, ni) if ni and ni > 0 else None)
            fin.capex_to_net_income.append(
                self._div(abs(capex) if capex else None, ni) if ni and ni > 0 else None
            )
            fin.ocf_to_debt.append(self._div(ocf, td) if td and td > 0 else None)

            # 利息覆盖：从 ratios_annual 取 interestCoverageRatio
            ic = None
            if ratios_annual and i < len(ratios_annual):
                ic = self._safe_float(ratios_annual[i].get("interestCoverageRatio"))
            fin.interest_coverage.append(ic)

        # ROIC（若 key_metrics 没拿到，自己算）
        if not fin.roic and fin.net_income and fin.total_assets and fin.total_debt:
            for i in range(len(fin.net_income)):
                ni = fin.net_income[i]
                ta = fin.total_assets[i] if i < len(fin.total_assets) else None
                td = fin.total_debt[i] if i < len(fin.total_debt) else None
                te = fin.total_equity[i] if i < len(fin.total_equity) else None
                invested_capital = (td or 0) + (te or 0)
                fin.roic.append(self._div(ni, invested_capital))

        fin.roe_5y_avg = self._avg(fin.roe[:5])
        if not fin.roic_5y_avg:
            fin.roic_5y_avg = self._avg(fin.roic[:5])

    # ─────────────────────────────────────────────
    # 计算增长率（CAGR）
    # ─────────────────────────────────────────────

    def _compute_growth(self, fin: NormalizedFinancials):
        fin.revenue_cagr_5y = self._cagr(fin.revenue, 5)
        fin.revenue_cagr_3y = self._cagr(fin.revenue, 3)
        fin.net_income_cagr_5y = self._cagr(fin.net_income, 5)
        fin.eps_cagr_5y = self._cagr(fin.eps, 5)
        fin.fcf_cagr_3y = self._cagr(fin.free_cash_flow, 3)

    # ─────────────────────────────────────────────
    # 计算稳定性指标
    # ─────────────────────────────────────────────

    def _compute_stability(self, fin: NormalizedFinancials):
        # 毛利率变异系数（护城河代理指标）
        gm = [v for v in fin.gross_margin if v is not None]
        if len(gm) >= 3:
            avg = sum(gm) / len(gm)
            if avg != 0:
                std = math.sqrt(sum((x - avg) ** 2 for x in gm) / len(gm))
                fin.gross_margin_cv = std / abs(avg)

        # 股权稀释率（近3年平均）
        shares = fin.shares_outstanding
        if len(shares) >= 4:
            rates = []
            for i in range(min(3, len(shares) - 1)):
                if shares[i + 1] and shares[i + 1] > 0:
                    rates.append((shares[i] - shares[i + 1]) / shares[i + 1])
            if rates:
                fin.dilution_rate_3y = sum(rates) / len(rates)

    # ─────────────────────────────────────────────
    # 工具方法
    # ─────────────────────────────────────────────

    def _safe_float(self, value, field_name: str = "", fin: NormalizedFinancials = None) -> Optional[float]:
        if value is None:
            return None
        try:
            v = float(value)
            return None if math.isnan(v) or math.isinf(v) else v
        except (TypeError, ValueError):
            if field_name and fin:
                flag = f"parse_error:{field_name}"
                if flag not in fin.data_quality_flags:
                    fin.data_quality_flags.append(flag)
            return None

    def _div(self, numerator, denominator) -> Optional[float]:
        if numerator is None or denominator is None:
            return None
        if denominator == 0:
            return None
        return numerator / denominator

    def _avg(self, values: list) -> Optional[float]:
        valid = [v for v in values if v is not None]
        return sum(valid) / len(valid) if valid else None

    def _cagr(self, series: list[float], years: int) -> Optional[float]:
        """计算CAGR，series[0] = 最新，series[-1] = 最旧"""
        if len(series) < years + 1:
            return None
        end_val = series[0]
        start_val = series[years]
        if start_val is None or end_val is None:
            return None
        if start_val <= 0:
            return None
        ratio = end_val / start_val
        if ratio <= 0:  # 负数的分数次幂会产生复数，直接返回 None
            return None
        try:
            return ratio ** (1 / years) - 1
        except (ZeroDivisionError, ValueError):
            return None
