"""
fetcher.py — 数据获取层（唯一网络边界）

覆盖数据：
  价格        历史日线（最多20年）
  财务报表    损益表、资产负债表、现金流量表（年报20年 + 季报20季）
  估值指标    PE/PB/PS/EV/PEG 等历史序列
  关键指标    ROE/ROA/ROIC/FCF/每股指标 历史序列
  财报摘要    EPS实际 vs 预期（历史Earnings，40季）
  内部人交易  管理层增减持记录
  分析师预期  EPS/营收一致预期
  企业价值    EV历史序列
  增长指标    营收/利润/FCF同比增速（FMP预计算）
  股息历史    历史分红记录

数据源优先级：FMP API（主）→ yfinance（fallback）→ 返回空
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from typing import Any, Optional

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except Exception:
    _YF_AVAILABLE = False
    yf = None

from config import API_CONFIG, CACHE_CONFIG

# FMP stable API 端点映射（新版 2025-08-31 后）
# 旧 v3: GET /api/v3/profile/{ticker}
# 新 stable: GET /stable/profile?symbol={ticker}
_FMP_STABLE_BASE = "https://financialmodelingprep.com/stable"


class DataFetcher:
    def __init__(self):
        self.api_key = API_CONFIG["fmp_api_key"]
        self.base_url = _FMP_STABLE_BASE  # 新 stable API（2025-08-31 后）
        self.timeout = API_CONFIG["request_timeout_seconds"]
        self.max_retries = API_CONFIG["max_retries"]
        self.retry_backoff = API_CONFIG["retry_backoff_seconds"]

        self._request_times: list[float] = []
        self._rate_limit = API_CONFIG["fmp_rate_limit_per_minute"]
        self._rate_lock = __import__('threading').Lock()

        # HTTP 连接池（keep-alive 复用连接）
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=40,
            pool_maxsize=40,
            max_retries=0,  # 我们自己管理重试
        )
        self._session.mount("https://", adapter)

        self._offline = False           # 离线模式标志（由 apply_offline_mode 设置）
        self._db_lock = __import__('threading').Lock()
        self._dirty_count = 0           # 未 commit 的缓存写入计数
        self._checkpoint_dirty = 0      # 未 commit 的断点写入计数
        self._last_commit = time.time()
        db_path = CACHE_CONFIG["db_path"]
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")       # 并发读写性能
        self.conn.execute("PRAGMA synchronous=NORMAL")     # 降低 fsync 频率
        self._init_db()

    # ─────────────────────────────────────────────
    # DB 初始化
    # ─────────────────────────────────────────────

    def _init_db(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS cache (
                cache_key   TEXT PRIMARY KEY,
                data_json   TEXT NOT NULL,
                fetched_at  INTEGER NOT NULL,
                ttl_days    INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_checkpoints (
                quarter     TEXT NOT NULL,
                ticker      TEXT NOT NULL,
                scored_at   INTEGER NOT NULL,
                PRIMARY KEY (quarter, ticker)
            );
            CREATE INDEX IF NOT EXISTS idx_cache_fetched ON cache(fetched_at);
        """)
        self.conn.commit()

    # ─────────────────────────────────────────────
    # 一、股票列表 & 基本信息
    # ─────────────────────────────────────────────

    def get_ticker_universe(self) -> list[str]:
        """
        全量美股列表（NYSE + NASDAQ + AMEX，仅普通股）

        策略：
          1. 优先尝试 /stable/stock-list（单次请求，~15000 条，需要 stable plan）
          2. 若返回 None / 空，则用 /stable/search-symbol 全字母扫描兜底
             遍历 A-Z × A-Z 共 702 次请求，能覆盖 1-5 字母的所有 ticker
        """
        cache_key = "universe:all"
        cached = self._cache_get(cache_key, CACHE_CONFIG["universe_ttl_days"])
        if cached is not None:
            return cached

        TARGET_EXCHANGES = {"NYSE", "NASDAQ", "AMEX"}
        all_tickers: dict[str, None] = {}

        # ── 策略 1: /stable/stock-list（单次全量，最快）
        data = self._fmp_request("/stock-list", {})
        if isinstance(data, list) and len(data) > 100:
            for item in data:
                sym = item.get("symbol", "")
                exch = item.get("exchange", "")
                typ = item.get("type", "")
                # 只保留三大交易所的普通股，排除含 . 和数字的特殊代码
                if (
                    exch in TARGET_EXCHANGES
                    and typ in ("stock", "")          # type 字段部分版本为空字符串
                    and sym.isalpha()
                    and len(sym) <= 5
                ):
                    all_tickers[sym] = None
            print(f"  [universe] stock-list 获取 {len(all_tickers)} 只普通股")

        # ── 策略 2: search-symbol 全字母扫描（兜底）
        if not all_tickers:
            print("  [universe] stock-list 不可用，改用 search-symbol 全字母扫描（约 702 次请求）...")
            import string
            letters = string.ascii_uppercase

            # 单字母 ticker（A, B, C ... Z）
            for first in letters:
                data = self._fmp_request("/search-symbol", {"query": first, "limit": 100})
                if isinstance(data, list):
                    for item in data:
                        sym = item.get("symbol", "")
                        exch = item.get("exchange", "")
                        if sym == first and exch in TARGET_EXCHANGES:
                            all_tickers[sym] = None

            # 两字母前缀扫描（AA-ZZ），每次最多返回 100 条
            total_prefixes = len(letters) * len(letters)
            for i, first in enumerate(letters):
                for second in letters:
                    prefix = first + second
                    data = self._fmp_request("/search-symbol", {"query": prefix, "limit": 100})
                    if isinstance(data, list):
                        for item in data:
                            sym = item.get("symbol", "")
                            exch = item.get("exchange", "")
                            if (
                                sym.startswith(prefix)
                                and exch in TARGET_EXCHANGES
                                and sym.isalpha()
                                and len(sym) <= 5
                            ):
                                all_tickers[sym] = None
                    done = i * len(letters) + letters.index(second) + 1
                    if done % 100 == 0:
                        print(f"  [universe] 扫描进度: {done}/{total_prefixes}，已找到 {len(all_tickers)} 只")

            print(f"  [universe] search-symbol 扫描完成，共 {len(all_tickers)} 只")

        result = sorted(all_tickers.keys())
        if result:
            self._cache_set(cache_key, result, CACHE_CONFIG["universe_ttl_days"])
        return result

    def get_profile(self, ticker: str) -> dict:
        """
        公司基本信息
        端点: /profile/{ticker}
        字段: companyName, sector, industry, exchange, mktCap, price,
              beta, volAvg, lastDiv, ipoDate, description, ceo,
              website, country, fullTimeEmployees, phone, address
        """
        cache_key = f"profile:{ticker}"
        cached = self._cache_get(cache_key, CACHE_CONFIG["universe_ttl_days"])
        if cached is not None:
            return cached

        data = self._fmp_request("/profile", {"symbol": ticker})
        result = data[0] if isinstance(data, list) and data else {}
        if result:
            self._cache_set(cache_key, result, CACHE_CONFIG["universe_ttl_days"])
        return result

    def is_delisted(self, ticker: str) -> bool:
        """
        判断股票是否已退市或实质上已停止交易。
        三项检查，全部依赖 profile 缓存，零额外 API 调用：
          1. isActivelyTrading = False  — FMP 官方退市标记
          2. price = 0 或缺失          — 没有当前报价，基本等于停止交易
          3. price 缺失时不误杀        — 保守处理，避免数据缺失被误判
        数据过期（年报日期）由调用方在拿到完整 raw 数据后再判断，
        见 score_ticker() 中的 _is_stale_financials() 检查。
        """
        profile = self.get_profile(ticker)
        if not profile:
            return True  # 拉不到 profile，视为无效/已退市

        # 1. 官方退市标记
        is_active = profile.get("isActivelyTrading")
        if is_active is not None and not bool(is_active):
            return True

        # 2. 报价为 0：没有当前市场价格，实质上已停止交易
        price = profile.get("price")
        if price is not None and float(price) == 0:
            return True

        return False

    # ─────────────────────────────────────────────
    # 二、历史价格（最多 20 年日线）
    # ─────────────────────────────────────────────

    def get_price_history_daily(self, ticker: str, years: int = 20) -> list[dict]:
        """
        日线 OHLCV + 复权价
        端点: /historical-price-full/{ticker}
        字段: date, open, high, low, close, adjClose, volume,
              vwap, change, changePercent, changeOverTime
        """
        cache_key = f"price_daily:{ticker}"
        ttl = CACHE_CONFIG["valuation_data_ttl_days"]
        cached = self._cache_get(cache_key, ttl)
        if cached is not None:
            return cached

        from_date = f"{datetime.now().year - years}-01-01"
        # stable API: /historical-price-eod/light 返回轻量日线（date/price/volume）
        data = self._fmp_request(
            "/historical-price-eod/light",
            {"symbol": ticker, "from": from_date}
        )
        result = data if isinstance(data, list) else []
        if result:
            self._cache_set(cache_key, result, ttl)
        return result

    # ─────────────────────────────────────────────
    # 三、财务报表（年报20年 + 季报20季）
    # ─────────────────────────────────────────────

    def get_income_statement(
        self, ticker: str, period: str = "annual", limit: int = 20
    ) -> list[dict]:
        """
        损益表
        端点: /income-statement/{ticker}
        关键字段: date, revenue, costOfRevenue, grossProfit, grossProfitRatio,
                  researchAndDevelopmentExpenses, generalAndAdministrativeExpenses,
                  operatingExpenses, operatingIncome, operatingIncomeRatio,
                  interestExpense, incomeBeforeTax, incomeTaxExpense,
                  netIncome, netIncomeRatio, eps, epsdiluted,
                  weightedAverageShsOut, weightedAverageShsOutDil
        period: "annual" | "quarter"
        """
        cache_key = f"income_{period}:{ticker}"
        ttl = CACHE_CONFIG["financial_data_ttl_days"]
        cached = self._cache_get(cache_key, ttl)
        if cached is not None:
            return cached

        data = self._fmp_request(
            "/income-statement",
            {"symbol": ticker, "period": period, "limit": limit}
        )
        if not data:
            data = self._yf_income_statement(ticker)

        result = data if isinstance(data, list) else []
        if result:
            self._cache_set(cache_key, result, ttl)
        return result

    def get_balance_sheet(
        self, ticker: str, period: str = "annual", limit: int = 20
    ) -> list[dict]:
        """
        资产负债表
        端点: /balance-sheet-statement/{ticker}
        关键字段: date, cashAndCashEquivalents, shortTermInvestments,
                  netReceivables, inventory, totalCurrentAssets,
                  propertyPlantEquipmentNet, goodwill, intangibleAssets,
                  totalAssets, accountPayables, shortTermDebt,
                  totalCurrentLiabilities, longTermDebt, totalDebt,
                  totalLiabilities, retainedEarnings,
                  totalStockholdersEquity, commonStockSharesOutstanding
        """
        cache_key = f"balance_{period}:{ticker}"
        ttl = CACHE_CONFIG["financial_data_ttl_days"]
        cached = self._cache_get(cache_key, ttl)
        if cached is not None:
            return cached

        data = self._fmp_request(
            "/balance-sheet-statement",
            {"symbol": ticker, "period": period, "limit": limit}
        )
        if not data:
            data = self._yf_balance_sheet(ticker)

        result = data if isinstance(data, list) else []
        if result:
            self._cache_set(cache_key, result, ttl)
        return result

    def get_cash_flow_statement(
        self, ticker: str, period: str = "annual", limit: int = 20
    ) -> list[dict]:
        """
        现金流量表
        端点: /cash-flow-statement/{ticker}
        关键字段: date, netIncome, depreciationAndAmortization,
                  changeInWorkingCapital, operatingCashFlow,
                  capitalExpenditure, acquisitionsNet,
                  freeCashFlow, commonStockIssued, commonStockRepurchased,
                  dividendsPaid, stockBasedCompensation
        """
        cache_key = f"cashflow_{period}:{ticker}"
        ttl = CACHE_CONFIG["financial_data_ttl_days"]
        cached = self._cache_get(cache_key, ttl)
        if cached is not None:
            return cached

        data = self._fmp_request(
            "/cash-flow-statement",
            {"symbol": ticker, "period": period, "limit": limit}
        )
        if not data:
            data = self._yf_cash_flow(ticker)

        result = data if isinstance(data, list) else []
        if result:
            self._cache_set(cache_key, result, ttl)
        return result

    # ─────────────────────────────────────────────
    # 四、关键指标历史（ROIC / FCF / 每股指标）
    # ─────────────────────────────────────────────

    def get_key_metrics(
        self, ticker: str, period: str = "annual", limit: int = 20
    ) -> list[dict]:
        """
        FMP 预计算关键指标历史序列
        端点: /key-metrics/{ticker}
        关键字段: date, revenuePerShare, netIncomePerShare,
                  operatingCashFlowPerShare, freeCashFlowPerShare,
                  cashPerShare, bookValuePerShare, tangibleBookValuePerShare,
                  marketCap, enterpriseValue,
                  peRatio, priceToSalesRatio, pocfratio, pfcfRatio,
                  pbRatio, evToSales, enterpriseValueOverEBITDA,
                  evToOperatingCashFlow, evToFreeCashFlow,
                  earningsYield, freeCashFlowYield,
                  debtToEquity, debtToAssets, netDebtToEBITDA,
                  currentRatio, interestCoverage,
                  dividendYield, payoutRatio,
                  capexToOperatingCashFlow, capexToRevenue,
                  roic, returnOnTangibleAssets, roe,
                  investedCapital, grahamNumber,
                  daysOfSalesOutstanding, daysPayablesOutstanding,
                  inventoryTurnover, receivablesTurnover
        """
        cache_key = f"key_metrics_{period}:{ticker}"
        ttl = CACHE_CONFIG["financial_data_ttl_days"]
        cached = self._cache_get(cache_key, ttl)
        if cached is not None:
            return cached

        data = self._fmp_request(
            "/key-metrics",
            {"symbol": ticker, "period": period, "limit": limit}
        )
        result = data if isinstance(data, list) else []
        if result:
            self._cache_set(cache_key, result, ttl)
        return result

    def get_financial_ratios(
        self, ticker: str, period: str = "annual", limit: int = 20
    ) -> list[dict]:
        """
        80+ 财务比率历史序列
        端点: /ratios/{ticker}
        分类字段:
          盈利性: grossProfitMargin, ebitdaratio, operatingProfitMargin,
                  pretaxProfitMargin, netProfitMargin, returnOnAssets,
                  returnOnEquity, returnOnCapitalEmployed
          流动性: currentRatio, quickRatio, cashRatio,
                  daysOfSalesOutstanding, daysOfInventoryOutstanding,
                  cashConversionCycle
          偿债性: debtRatio, debtEquityRatio, longtermDebtToCapitalization,
                  interestCoverage, cashFlowToDebtRatio
          效率性: assetTurnover, fixedAssetTurnover,
                  accountsReceivableTurnover, inventoryTurnover
          现金流: operatingCashFlowPerShare, freeCashFlowPerShare,
                  operatingCashFlowSalesRatio,
                  freeCashFlowOperatingCashFlowRatio,
                  capitalExpenditureCoverageRatio, dividendPayoutRatio
          估值:   priceBookValueRatio, priceToSalesRatio, priceEarningsRatio,
                  priceToFreeCashFlowsRatio, priceCashFlowRatio,
                  priceEarningsToGrowthRatio, dividendYield,
                  enterpriseValueMultiple, priceFairValue
        """
        cache_key = f"ratios_{period}:{ticker}"
        ttl = CACHE_CONFIG["financial_data_ttl_days"]
        cached = self._cache_get(cache_key, ttl)
        if cached is not None:
            return cached

        data = self._fmp_request(
            "/ratios",
            {"symbol": ticker, "period": period, "limit": limit}
        )
        result = data if isinstance(data, list) else []
        if result:
            self._cache_set(cache_key, result, ttl)
        return result

    # ─────────────────────────────────────────────
    # 五、当前 TTM 估值（7天缓存）
    # ─────────────────────────────────────────────

    def get_key_metrics_ttm(self, ticker: str) -> dict:
        """
        当前 TTM 关键指标（字段同 get_key_metrics，后缀TTM）
        端点: /key-metrics-ttm/{ticker}
        """
        cache_key = f"key_metrics_ttm:{ticker}"
        ttl = CACHE_CONFIG["valuation_data_ttl_days"]
        cached = self._cache_get(cache_key, ttl)
        if cached is not None:
            return cached

        data = self._fmp_request("/key-metrics-ttm", {"symbol": ticker})
        result = data[0] if isinstance(data, list) and data else {}
        if result:
            self._cache_set(cache_key, result, ttl)
        return result

    def get_ratios_ttm(self, ticker: str) -> dict:
        """
        当前 TTM 财务比率（字段同 get_financial_ratios，后缀TTM）
        端点: /ratios-ttm/{ticker}
        包含: peRatioTTM, pbRatioTTM, psRatioTTM, pegRatioTTM,
              evToEbitdaTTM, freeCashFlowYieldTTM 等
        """
        cache_key = f"ratios_ttm:{ticker}"
        ttl = CACHE_CONFIG["valuation_data_ttl_days"]
        cached = self._cache_get(cache_key, ttl)
        if cached is not None:
            return cached

        data = self._fmp_request("/ratios-ttm", {"symbol": ticker})
        result = data[0] if isinstance(data, list) and data else {}
        if result:
            self._cache_set(cache_key, result, ttl)
        return result

    # ─────────────────────────────────────────────
    # 六、企业价值历史
    # ─────────────────────────────────────────────

    def get_enterprise_values(
        self, ticker: str, period: str = "annual", limit: int = 20
    ) -> list[dict]:
        """
        企业价值历史序列
        端点: /enterprise-values/{ticker}
        字段: date, stockPrice, numberOfShares, marketCapitalization,
              minusCashAndCashEquivalents, addTotalDebt, enterpriseValue
        """
        cache_key = f"ev_{period}:{ticker}"
        ttl = CACHE_CONFIG["financial_data_ttl_days"]
        cached = self._cache_get(cache_key, ttl)
        if cached is not None:
            return cached

        data = self._fmp_request(
            "/enterprise-values",
            {"symbol": ticker, "period": period, "limit": limit}
        )
        result = data if isinstance(data, list) else []
        if result:
            self._cache_set(cache_key, result, ttl)
        return result

    # ─────────────────────────────────────────────
    # 七、增长率（FMP 预计算同比增速）
    # ─────────────────────────────────────────────

    def get_income_growth(self, ticker: str, limit: int = 20) -> list[dict]:
        """
        损益表增速
        端点: /income-statement-growth/{ticker}
        字段: date, growthRevenue, growthGrossProfit, growthEBITDA,
              growthOperatingIncome, growthNetIncome, growthEPS,
              growthEPSDiluted, growthOperatingCashFlow,
              growthFreeCashFlow, growthNetProfitMargin
        """
        cache_key = f"income_growth:{ticker}"
        ttl = CACHE_CONFIG["financial_data_ttl_days"]
        cached = self._cache_get(cache_key, ttl)
        if cached is not None:
            return cached

        data = self._fmp_request(
            "/income-statement-growth",
            {"symbol": ticker, "limit": limit}
        )
        result = data if isinstance(data, list) else []
        if result:
            self._cache_set(cache_key, result, ttl)
        return result

    def get_cashflow_growth(self, ticker: str, limit: int = 20) -> list[dict]:
        """
        现金流增速
        端点: /cash-flow-statement-growth/{ticker}
        字段: date, growthOperatingCashFlow, growthFreeCashFlow,
              growthCapitalExpenditure, growthNetCashProvidedByOperatingActivites
        """
        cache_key = f"cashflow_growth:{ticker}"
        ttl = CACHE_CONFIG["financial_data_ttl_days"]
        cached = self._cache_get(cache_key, ttl)
        if cached is not None:
            return cached

        data = self._fmp_request(
            "/cash-flow-statement-growth",
            {"symbol": ticker, "limit": limit}
        )
        result = data if isinstance(data, list) else []
        if result:
            self._cache_set(cache_key, result, ttl)
        return result

    # ─────────────────────────────────────────────
    # 八、财报 EPS 实际 vs 预期
    # ─────────────────────────────────────────────

    def get_earnings_history(self, ticker: str, limit: int = 40) -> list[dict]:
        """
        历史财报 EPS 超预期记录（近40季 ≈ 10年）
        端点: /historical/earning_calendar/{ticker}
        字段: date, eps, epsEstimated, revenue, revenueEstimated,
              fiscalDateEnding
        用途: 彼得·林奇重视的 EPS 超预期信号
        """
        cache_key = f"earnings:{ticker}"
        ttl = CACHE_CONFIG["financial_data_ttl_days"]
        cached = self._cache_get(cache_key, ttl)
        if cached is not None:
            return cached

        data = self._fmp_request(
            "/earnings",
            {"symbol": ticker, "limit": limit}
        )
        result = data if isinstance(data, list) else []
        if result:
            self._cache_set(cache_key, result, ttl)
        return result

    # ─────────────────────────────────────────────
    # 九、内部人交易
    # ─────────────────────────────────────────────

    def get_insider_trading(self, ticker: str, limit: int = 100) -> list[dict]:
        """
        管理层增减持记录
        端点: /insider-trading
        字段: transactionDate, transactionType (P-Purchase/S-Sale),
              securitiesTransacted, price, securityName,
              reportingName, typeOfOwner
        用途: 巴菲特/芒格重视管理层持股信号
        """
        cache_key = f"insider:{ticker}"
        ttl = CACHE_CONFIG["universe_ttl_days"]
        cached = self._cache_get(cache_key, ttl)
        if cached is not None:
            return cached

        data = self._fmp_request(
            "/insider-trading",
            {"symbol": ticker, "limit": limit, "transactionType": "P-Purchase,S-Sale"}
        )
        result = data if isinstance(data, list) else []
        if result:
            self._cache_set(cache_key, result, ttl)
        return result

    # ─────────────────────────────────────────────
    # 十、分析师预期
    # ─────────────────────────────────────────────

    def get_analyst_estimates(self, ticker: str, limit: int = 8) -> list[dict]:
        """
        分析师一致预期
        端点: /analyst-estimates/{ticker}
        字段: date, estimatedRevenueLow/High/Avg,
              estimatedEbitdaLow/High/Avg, estimatedEpsLow/High/Avg,
              estimatedNetIncomeLow/High/Avg,
              numberAnalystEstimatedRevenue, numberAnalystsEstimatedEps
        """
        cache_key = f"analyst:{ticker}"
        ttl = CACHE_CONFIG["valuation_data_ttl_days"]
        cached = self._cache_get(cache_key, ttl)
        if cached is not None:
            return cached

        data = self._fmp_request(
            "/analyst-estimates",
            {"symbol": ticker, "limit": limit}
        )
        result = data if isinstance(data, list) else []
        if result:
            self._cache_set(cache_key, result, ttl)
        return result

    # ─────────────────────────────────────────────
    # 十一、股息历史
    # ─────────────────────────────────────────────

    def get_dividend_history(self, ticker: str) -> list[dict]:
        """
        历史分红记录
        端点: /historical-price-full/stock_dividend/{ticker}
        字段: date, adjDividend, dividend, recordDate,
              paymentDate, declarationDate
        """
        cache_key = f"dividend:{ticker}"
        ttl = CACHE_CONFIG["financial_data_ttl_days"]
        cached = self._cache_get(cache_key, ttl)
        if cached is not None:
            return cached

        data = self._fmp_request("/dividends", {"symbol": ticker})
        result = data if isinstance(data, list) else []
        if result:
            self._cache_set(cache_key, result, ttl)
        return result

    # ─────────────────────────────────────────────
    # 十二、主力汇总（供 normalizer 使用）
    # ─────────────────────────────────────────────

    # normalizer.normalize() 实际消费的 8 个字段：result_key → cache_key_prefix
    _SCORING_CACHE_KEYS: dict = {
        "profile":            "profile",
        "income_annual":      "income_annual",
        "balance_annual":     "balance_annual",
        "cashflow_annual":    "cashflow_annual",
        "key_metrics_annual": "key_metrics_annual",
        "ratios_annual":      "ratios_annual",
        "key_metrics_ttm":    "key_metrics_ttm",
        "ratios_ttm":         "ratios_ttm",
    }

    def _batch_cache_read(self, ticker: str) -> dict:
        """
        离线模式专用：一次 SQL 批量读取打分所需的所有缓存键，
        替代原先 19 次串行 _cache_get 调用。
        返回 {result_key: parsed_data}，缺失或过期的键返回空默认值。
        """
        now = int(time.time())
        key_map = {rk: f"{prefix}:{ticker}" for rk, prefix in self._SCORING_CACHE_KEYS.items()}
        cache_keys = list(key_map.values())

        placeholders = ",".join("?" * len(cache_keys))
        with self._db_lock:
            rows = self.conn.execute(
                f"SELECT cache_key, data_json, fetched_at, ttl_days FROM cache"
                f" WHERE cache_key IN ({placeholders})",
                cache_keys,
            ).fetchall()

        # 过滤过期行，解析 JSON
        fresh: dict = {}
        for cache_key, data_json, fetched_at, ttl_days in rows:
            if now - fetched_at <= ttl_days * 86400:
                fresh[cache_key] = json.loads(data_json)

        # 拼装结果，缺失键给默认空值
        result: dict = {"ticker": ticker}
        for rk, ck in key_map.items():
            data = fresh.get(ck)
            if data is None:
                result[rk] = {} if ("ttm" in rk or rk == "profile") else []
            else:
                result[rk] = data
        return result

    def get_all_financial_data(self, ticker: str) -> dict:
        """汇总所有数据，normalizer 的直接输入。
        - 离线模式：一次批量 SQL 读取打分所需的 8 个缓存键（跳过价格/季报等无关数据）
        - 在线模式：并发拉取全部 19 个端点
        """
        if self._offline:
            return self._batch_cache_read(ticker)

        # 在线模式：并发拉取所有端点
        tasks = {
            "profile":            lambda: self.get_profile(ticker),
            "income_annual":      lambda: self.get_income_statement(ticker, "annual", 20),
            "balance_annual":     lambda: self.get_balance_sheet(ticker, "annual", 20),
            "cashflow_annual":    lambda: self.get_cash_flow_statement(ticker, "annual", 20),
            "income_quarterly":   lambda: self.get_income_statement(ticker, "quarter", 20),
            "balance_quarterly":  lambda: self.get_balance_sheet(ticker, "quarter", 20),
            "cashflow_quarterly": lambda: self.get_cash_flow_statement(ticker, "quarter", 20),
            "key_metrics_annual": lambda: self.get_key_metrics(ticker, "annual", 20),
            "ratios_annual":      lambda: self.get_financial_ratios(ticker, "annual", 20),
            "enterprise_values":  lambda: self.get_enterprise_values(ticker, "annual", 20),
            "income_growth":      lambda: self.get_income_growth(ticker, 20),
            "cashflow_growth":    lambda: self.get_cashflow_growth(ticker, 20),
            "key_metrics_ttm":    lambda: self.get_key_metrics_ttm(ticker),
            "ratios_ttm":         lambda: self.get_ratios_ttm(ticker),
            "earnings_history":   lambda: self.get_earnings_history(ticker, 40),
            "insider_trading":    lambda: self.get_insider_trading(ticker, 100),
            "analyst_estimates":  lambda: self.get_analyst_estimates(ticker, 8),
            "dividend_history":   lambda: self.get_dividend_history(ticker),
            "price_daily":        lambda: self.get_price_history_daily(ticker, 20),
        }

        result = {"ticker": ticker}
        with ThreadPoolExecutor(max_workers=19) as pool:
            futures = {pool.submit(fn): key for key, fn in tasks.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    result[key] = future.result()
                except Exception as e:
                    print(f"  [并发错误] {ticker}/{key}: {e}")
                    result[key] = {} if "ttm" in key or key == "profile" else []
        return result

    # ─────────────────────────────────────────────
    # yfinance Fallback
    # ─────────────────────────────────────────────

    def _yf_income_statement(self, ticker: str) -> list[dict]:
        try:
            stock = yf.Ticker(ticker)
            df = stock.financials.T
            records = []
            for date, row in df.iterrows():
                records.append({
                    "date": str(date)[:10],
                    "revenue": _to_float(row.get("Total Revenue")),
                    "grossProfit": _to_float(row.get("Gross Profit")),
                    "operatingIncome": _to_float(row.get("Operating Income")),
                    "netIncome": _to_float(row.get("Net Income")),
                    "ebitda": _to_float(row.get("EBITDA")),
                    "epsdiluted": _to_float(row.get("Diluted EPS")),
                    "researchAndDevelopmentExpenses": _to_float(row.get("Research And Development")),
                    "_source": "yfinance",
                })
            return records
        except Exception:
            return []

    def _yf_balance_sheet(self, ticker: str) -> list[dict]:
        try:
            stock = yf.Ticker(ticker)
            df = stock.balance_sheet.T
            records = []
            for date, row in df.iterrows():
                records.append({
                    "date": str(date)[:10],
                    "totalAssets": _to_float(row.get("Total Assets")),
                    "totalDebt": _to_float(row.get("Total Debt")),
                    "totalStockholdersEquity": _to_float(row.get("Stockholders Equity")),
                    "cashAndCashEquivalents": _to_float(row.get("Cash And Cash Equivalents")),
                    "totalCurrentAssets": _to_float(row.get("Current Assets")),
                    "totalCurrentLiabilities": _to_float(row.get("Current Liabilities")),
                    "commonStockSharesOutstanding": _to_float(row.get("Ordinary Shares Number")),
                    "_source": "yfinance",
                })
            return records
        except Exception:
            return []

    def _yf_cash_flow(self, ticker: str) -> list[dict]:
        try:
            stock = yf.Ticker(ticker)
            df = stock.cashflow.T
            records = []
            for date, row in df.iterrows():
                ocf = _to_float(row.get("Operating Cash Flow")) or 0
                capex = _to_float(row.get("Capital Expenditure")) or 0
                records.append({
                    "date": str(date)[:10],
                    "operatingCashFlow": ocf,
                    "capitalExpenditure": capex,
                    "freeCashFlow": ocf + capex,
                    "dividendsPaid": _to_float(row.get("Payment Of Dividends")),
                    "commonStockRepurchased": _to_float(row.get("Repurchase Of Capital Stock")),
                    "depreciationAndAmortization": _to_float(row.get("Depreciation And Amortization")),
                    "_source": "yfinance",
                })
            return records
        except Exception:
            return []

    # ─────────────────────────────────────────────
    # 限速 + HTTP
    # ─────────────────────────────────────────────

    def _rate_limit_wait(self):
        with self._rate_lock:
            now = time.time()
            self._request_times = [t for t in self._request_times if now - t < 60]
            if len(self._request_times) >= self._rate_limit:
                sleep_time = 60 - (now - self._request_times[0]) + 0.1
                if sleep_time > 0:
                    time.sleep(sleep_time)
            self._request_times.append(time.time())

    def _fmp_request(self, endpoint: str, params: dict | None = None) -> Any:
        if not self.api_key:
            raise ValueError("FMP_API_KEY 未设置，请在 .env 文件中配置")

        url = f"{self.base_url}{endpoint}"
        req_params = {"apikey": self.api_key}
        if params:
            req_params.update(params)

        for attempt in range(self.max_retries):
            try:
                self._rate_limit_wait()
                resp = self._session.get(url, params=req_params, timeout=(5, self.timeout))

                if resp.status_code == 429:
                    wait = self.retry_backoff * (2 ** attempt)
                    print(f"  [限速] 429，等待 {wait}s")
                    time.sleep(wait)
                    continue

                if resp.status_code != 200:
                    print(f"  [HTTP {resp.status_code}] {endpoint}")
                    return None

                data = resp.json()
                if isinstance(data, dict) and "Error Message" in data:
                    print(f"  [FMP Error] {data['Error Message']}")
                    return None

                return data

            except requests.exceptions.Timeout:
                print(f"  [超时] {endpoint}，第{attempt+1}次")
                time.sleep(self.retry_backoff)
            except requests.exceptions.RequestException as e:
                print(f"  [网络错误] {endpoint}: {e}")
                time.sleep(self.retry_backoff)

        return None

    # ─────────────────────────────────────────────
    # 缓存读写
    # ─────────────────────────────────────────────

    def _cache_get(self, cache_key: str, ttl_days: int) -> Any | None:
        now = int(time.time())
        with self._db_lock:
            row = self.conn.execute(
                "SELECT data_json, fetched_at, ttl_days FROM cache WHERE cache_key = ?",
                (cache_key,)
            ).fetchone()
        if not row:
            return None
        data_json, fetched_at, stored_ttl = row
        if now - fetched_at > stored_ttl * 86400:
            with self._db_lock:
                self.conn.execute("DELETE FROM cache WHERE cache_key = ?", (cache_key,))
                self.conn.commit()
            return None
        return json.loads(data_json)

    def _cache_set(self, cache_key: str, data: Any, ttl_days: int):
        now = int(time.time())
        with self._db_lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO cache (cache_key, data_json, fetched_at, ttl_days) VALUES (?, ?, ?, ?)",
                (cache_key, json.dumps(data, ensure_ascii=False), now, ttl_days)
            )
            self._dirty_count += 1
            # 批量 commit：每 20 次写入或每 2 秒刷一次盘
            if self._dirty_count >= 20 or (time.time() - self._last_commit) > 2:
                self.conn.commit()
                self._dirty_count = 0
                self._last_commit = time.time()

    def flush_cache(self):
        """强制刷盘，确保所有写入持久化"""
        with self._db_lock:
            if self._dirty_count > 0 or self._checkpoint_dirty > 0:
                self.conn.commit()
                self._dirty_count = 0
                self._checkpoint_dirty = 0
                self._last_commit = time.time()

    # ─────────────────────────────────────────────
    # 断点续跑
    # ─────────────────────────────────────────────

    def is_scored(self, ticker: str, quarter: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM run_checkpoints WHERE quarter = ? AND ticker = ?",
            (quarter, ticker)
        ).fetchone() is not None

    def mark_scored(self, ticker: str, quarter: str):
        with self._db_lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO run_checkpoints (quarter, ticker, scored_at) VALUES (?, ?, ?)",
                (quarter, ticker, int(time.time()))
            )
            self._checkpoint_dirty += 1
            # 每 50 条批量提交一次，减少 fsync 次数
            if self._checkpoint_dirty >= 50:
                self.conn.commit()
                self._checkpoint_dirty = 0

    def get_remaining_tickers(self, tickers: list[str], quarter: str) -> list[str]:
        scored = {
            row[0] for row in self.conn.execute(
                "SELECT ticker FROM run_checkpoints WHERE quarter = ?", (quarter,)
            ).fetchall()
        }
        return [t for t in tickers if t not in scored]

    def progress_report(self, total: int, quarter: str) -> dict:
        done = self.conn.execute(
            "SELECT COUNT(*) FROM run_checkpoints WHERE quarter = ?", (quarter,)
        ).fetchone()[0]
        return {"total": total, "done": done, "remaining": total - done}

    def close(self):
        self.flush_cache()
        self._session.close()
        self.conn.close()


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _to_float(value) -> Optional[float]:
    import math
    if value is None:
        return None
    try:
        v = float(value)
        return None if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return None
