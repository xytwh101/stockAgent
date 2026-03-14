"""
test_fetch.py — 数据拉取测试

用法:
    cd /Users/tanweihan/code/stockAgent
    python tests/test_fetch.py              # 测试 AAPL
    python tests/test_fetch.py MSFT GOOGL   # 测试指定股票
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.fetcher import DataFetcher


def fmt(val, fmt_str=None):
    if val is None:
        return "N/A"
    if fmt_str == "pct":
        return f"{val*100:.1f}%"
    if fmt_str == "B":
        return f"${val/1e9:.2f}B"
    if fmt_str == "M":
        return f"${val/1e6:.1f}M"
    if isinstance(val, float):
        return f"{val:.4f}"
    return str(val)


def sep(title=""):
    if title:
        print(f"\n{'─'*20} {title} {'─'*20}")
    else:
        print("─" * 50)


def test_ticker(ticker: str, fetcher: DataFetcher):
    print(f"\n{'='*60}")
    print(f"  测试股票: {ticker}")
    print(f"{'='*60}")

    # ── 1. 公司基本信息 ───────────────────────────
    sep("1. 公司基本信息")
    profile = fetcher.get_profile(ticker)
    if profile:
        print(f"  公司名称   : {profile.get('companyName')}")
        print(f"  行业       : {profile.get('sector')} / {profile.get('industry')}")
        print(f"  交易所     : {profile.get('exchange')}")
        print(f"  市值       : {fmt(profile.get('marketCap', profile.get('mktCap')), 'B')}")
        print(f"  当前价格   : ${profile.get('price')}")
        print(f"  Beta       : {profile.get('beta')}")
        print(f"  IPO日期    : {profile.get('ipoDate')}")
        print(f"  员工数     : {profile.get('fullTimeEmployees')}")
    else:
        print("  [!] 无法获取 profile")

    # ── 2. 损益表（年报，近5年）─────────────────────
    sep("2. 损益表（年报，近5年）")
    income = fetcher.get_income_statement(ticker, period="annual", limit=5)
    if income:
        print(f"  {'年份':<8} {'营收':>14} {'毛利润':>14} {'营业利润':>14} {'净利润':>14} {'EPS':>8}")
        print(f"  {'─'*6:<8} {'─'*12:>14} {'─'*12:>14} {'─'*12:>14} {'─'*12:>14} {'─'*6:>8}")
        for row in income[:5]:
            print(f"  {row.get('date','')[:4]:<8}"
                  f" {fmt(row.get('revenue'), 'B'):>14}"
                  f" {fmt(row.get('grossProfit'), 'B'):>14}"
                  f" {fmt(row.get('operatingIncome'), 'B'):>14}"
                  f" {fmt(row.get('netIncome'), 'B'):>14}"
                  f" {fmt(row.get('epsdiluted')):>8}")
        print(f"  共 {len(income)} 年数据")
    else:
        print("  [!] 无法获取损益表")

    # ── 3. 资产负债表（年报，近5年）─────────────────
    sep("3. 资产负债表（年报，近5年）")
    balance = fetcher.get_balance_sheet(ticker, period="annual", limit=5)
    if balance:
        print(f"  {'年份':<8} {'总资产':>14} {'总负债':>14} {'股东权益':>14} {'现金':>12} {'流动比':<8}")
        print(f"  {'─'*6:<8} {'─'*12:>14} {'─'*12:>14} {'─'*12:>14} {'─'*10:>12} {'─'*6:<8}")
        for row in balance[:5]:
            ta = row.get('totalAssets') or 1
            tl = row.get('totalLiabilities') or 0
            ca = row.get('totalCurrentAssets') or 0
            cl = row.get('totalCurrentLiabilities') or 1
            print(f"  {row.get('date','')[:4]:<8}"
                  f" {fmt(ta, 'B'):>14}"
                  f" {fmt(tl, 'B'):>14}"
                  f" {fmt(row.get('totalStockholdersEquity'), 'B'):>14}"
                  f" {fmt(row.get('cashAndCashEquivalents'), 'B'):>12}"
                  f" {ca/cl:.2f}")
        print(f"  共 {len(balance)} 年数据")
    else:
        print("  [!] 无法获取资产负债表")

    # ── 4. 现金流量表（年报，近5年）─────────────────
    sep("4. 现金流量表（年报，近5年）")
    cashflow = fetcher.get_cash_flow_statement(ticker, period="annual", limit=5)
    if cashflow:
        print(f"  {'年份':<8} {'经营现金流':>14} {'资本支出':>12} {'自由现金流':>14} {'回购':>12} {'分红':>12}")
        print(f"  {'─'*6:<8} {'─'*12:>14} {'─'*10:>12} {'─'*12:>14} {'─'*10:>12} {'─'*10:>12}")
        for row in cashflow[:5]:
            print(f"  {row.get('date','')[:4]:<8}"
                  f" {fmt(row.get('operatingCashFlow'), 'B'):>14}"
                  f" {fmt(row.get('capitalExpenditure'), 'B'):>12}"
                  f" {fmt(row.get('freeCashFlow'), 'B'):>14}"
                  f" {fmt(row.get('commonStockRepurchased'), 'B'):>12}"
                  f" {fmt(row.get('dividendsPaid'), 'B'):>12}")
        print(f"  共 {len(cashflow)} 年数据")
    else:
        print("  [!] 无法获取现金流量表")

    # ── 5. 关键指标历史（ROE/ROIC/FCF Yield 等）────
    sep("5. 关键指标历史（近5年）")
    metrics = fetcher.get_key_metrics(ticker, period="annual", limit=5)
    if metrics:
        print(f"  {'年份':<8} {'EV/EBITDA':>10} {'ROE':>8} {'ROIC':>8} {'FCF Yield':>10} {'市值(B)':>10}")
        print(f"  {'─'*6:<8} {'─'*8:>10} {'─'*6:>8} {'─'*6:>8} {'─'*8:>10} {'─'*8:>10}")
        for row in metrics[:5]:
            print(f"  {row.get('date','')[:4]:<8}"
                  f" {fmt(row.get('evToEBITDA')):>10}"
                  f" {fmt(row.get('returnOnEquity'), 'pct'):>8}"
                  f" {fmt(row.get('returnOnInvestedCapital'), 'pct'):>8}"
                  f" {fmt(row.get('freeCashFlowYield'), 'pct'):>10}"
                  f" {fmt(row.get('marketCap'), 'B'):>10}")
    else:
        print("  [!] 无法获取关键指标")

    # ── 6. 财务比率（盈利性，近3年）─────────────────
    sep("6. 盈利性比率（近3年）")
    ratios = fetcher.get_financial_ratios(ticker, period="annual", limit=3)
    if ratios:
        for row in ratios[:3]:
            # 新API字段: grossProfitMargin, operatingProfitMargin, netProfitMargin, returnOnAssets, returnOnEquity
            print(f"  [{row.get('date','')[:4]}]"
                  f"  毛利率={fmt(row.get('grossProfitMargin'), 'pct')}"
                  f"  营业利润率={fmt(row.get('operatingProfitMargin'), 'pct')}"
                  f"  净利润率={fmt(row.get('netProfitMargin'), 'pct')}"
                  f"  PE={fmt(row.get('priceToEarningsRatio'))}"
                  f"  PB={fmt(row.get('priceToBookRatio'))}"
                  f"  PEG={fmt(row.get('priceToEarningsGrowthRatio'))}")
    else:
        print("  [!] 无法获取财务比率")

    # ── 7. 当前 TTM 估值 ──────────────────────────
    sep("7. 当前 TTM 估值")
    ttm = fetcher.get_ratios_ttm(ticker)
    km_ttm = fetcher.get_key_metrics_ttm(ticker)
    if ttm or km_ttm:
        # ratios-ttm 字段（新API后缀TTM）
        print(f"  PE (TTM)          : {fmt(ttm.get('priceToEarningsRatioTTM'))}")
        print(f"  PB (TTM)          : {fmt(ttm.get('priceToBookRatioTTM'))}")
        print(f"  PS (TTM)          : {fmt(ttm.get('priceToSalesRatioTTM'))}")
        print(f"  PEG (TTM)         : {fmt(ttm.get('priceToEarningsGrowthRatioTTM'))}")
        print(f"  FCF/Price (TTM)   : {fmt(ttm.get('priceToFreeCashFlowRatioTTM'))}")
        print(f"  Current Ratio     : {fmt(ttm.get('currentRatioTTM'))}")
        print(f"  Debt/Equity       : {fmt(ttm.get('debtToEquityRatioTTM'))}")
        print(f"  Interest Coverage : {fmt(ttm.get('interestCoverageRatioTTM'))}")
        print(f"  Dividend Yield    : {fmt(ttm.get('dividendYieldTTM'), 'pct')}")
        print(f"  Dividend/Share    : {fmt(ttm.get('dividendPerShareTTM'))}")
        # key-metrics-ttm 字段
        print(f"  EV/EBITDA (TTM)   : {fmt(km_ttm.get('evToEBITDATTM'))}")
        print(f"  FCF Yield (TTM)   : {fmt(km_ttm.get('freeCashFlowYieldTTM'), 'pct')}")
        print(f"  ROIC (TTM)        : {fmt(km_ttm.get('returnOnInvestedCapitalTTM'), 'pct')}")
        print(f"  ROE (TTM)         : {fmt(km_ttm.get('returnOnEquityTTM'), 'pct')}")
        print(f"  ROA (TTM)         : {fmt(km_ttm.get('returnOnAssetsTTM'), 'pct')}")
        print(f"  Enterprise Value  : {fmt(km_ttm.get('enterpriseValueTTM'), 'B')}")
        print(f"  Graham Number     : {fmt(km_ttm.get('grahamNumberTTM'))}")
    else:
        print("  [!] 无法获取 TTM 估值")

    # ── 8. 增长率历史 ─────────────────────────────
    sep("8. 增长率历史（近5年）")
    growth = fetcher.get_income_growth(ticker, limit=5)
    if growth:
        print(f"  {'年份':<8} {'营收增速':>10} {'毛利增速':>10} {'净利增速':>10} {'EPS增速':>10} {'FCF增速':>10}")
        print(f"  {'─'*6:<8} {'─'*8:>10} {'─'*8:>10} {'─'*8:>10} {'─'*8:>10} {'─'*8:>10}")
        for row in growth[:5]:
            print(f"  {row.get('date','')[:4]:<8}"
                  f" {fmt(row.get('growthRevenue'), 'pct'):>10}"
                  f" {fmt(row.get('growthGrossProfit'), 'pct'):>10}"
                  f" {fmt(row.get('growthNetIncome'), 'pct'):>10}"
                  f" {fmt(row.get('growthEPSDiluted'), 'pct'):>10}"
                  f" {fmt(row.get('growthFreeCashFlow'), 'pct'):>10}")
    else:
        print("  [!] 无法获取增长率数据")

    # ── 9. 企业价值历史 ────────────────────────────
    sep("9. 企业价值历史（近5年）")
    ev_data = fetcher.get_enterprise_values(ticker, period="annual", limit=5)
    if ev_data:
        for row in ev_data[:5]:
            print(f"  [{row.get('date','')[:4]}]"
                  f"  市值={fmt(row.get('marketCapitalization'), 'B')}"
                  f"  EV={fmt(row.get('enterpriseValue'), 'B')}"
                  f"  股价=${row.get('stockPrice')}"
                  f"  股数={fmt(row.get('numberOfShares'), 'B')}")
    else:
        print("  [!] 无法获取企业价值数据")

    # ── 10. 历史财报 EPS 超预期 ──────────────────
    sep("10. 财报 EPS 实际 vs 预期（近8季）")
    earnings = fetcher.get_earnings_history(ticker, limit=8)
    if earnings:
        print(f"  {'日期':<12} {'EPS实际':>10} {'EPS预期':>10} {'超预期':>10} {'营收实际':>14} {'营收预期':>14}")
        print(f"  {'─'*10:<12} {'─'*8:>10} {'─'*8:>10} {'─'*8:>10} {'─'*12:>14} {'─'*12:>14}")
        for row in earnings[:8]:
            eps_a = row.get('eps') or 0
            eps_e = row.get('epsEstimated') or 0
            surprise = eps_a - eps_e if eps_e else None
            print(f"  {row.get('date',''):<12}"
                  f" {fmt(eps_a):>10}"
                  f" {fmt(eps_e):>10}"
                  f" {fmt(surprise):>10}"
                  f" {fmt(row.get('revenue'), 'B'):>14}"
                  f" {fmt(row.get('revenueEstimated'), 'B'):>14}")
    else:
        print("  [!] 无法获取财报历史")

    # ── 11. 内部人交易（近10条）─────────────────
    sep("11. 内部人交易（近10条）")
    insider = fetcher.get_insider_trading(ticker, limit=10)
    if insider:
        for row in insider[:10]:
            t_type = row.get('transactionType', '')
            marker = "🟢买入" if "P-Purchase" in t_type else "🔴卖出" if "S-Sale" in t_type else t_type
            print(f"  {row.get('transactionDate',''):<12}"
                  f" {marker:<8}"
                  f" {row.get('reportingName',''):<25}"
                  f" {row.get('securitiesTransacted', 0):>10,.0f}股"
                  f" @${row.get('price', 0):.2f}")
    else:
        print("  [!] 无法获取内部人交易数据")

    # ── 12. 分析师预期 ────────────────────────────
    sep("12. 分析师预期")
    analyst = fetcher.get_analyst_estimates(ticker, limit=4)
    if analyst:
        for row in analyst[:4]:
            print(f"  [{row.get('date','')[:7]}]"
                  f"  EPS预期均值={fmt(row.get('estimatedEpsAvg'))}"
                  f"  营收预期均值={fmt(row.get('estimatedRevenueAvg'), 'B')}"
                  f"  分析师数量={row.get('numberAnalystsEstimatedEps','N/A')}")
    else:
        print("  [!] 无法获取分析师预期")

    # ── 13. 股息历史（近5年）───────────────────
    sep("13. 股息历史（近10次）")
    div = fetcher.get_dividend_history(ticker)
    if div:
        print(f"  近10次分红记录：")
        for row in div[:10]:
            print(f"  {row.get('date',''):<12}"
                  f" 每股分红=${row.get('dividend', 0):.4f}"
                  f" 除权价=${row.get('adjDividend', 0):.4f}")
    else:
        print("  [!] 无分红记录（或不分红）")

    # ── 14. 历史价格摘要 ──────────────────────────
    sep("14. 历史价格摘要（近20年）")
    prices = fetcher.get_price_history_daily(ticker, years=20)
    if prices:
        total_days = len(prices)
        # stable API /historical-price-eod/light: 字段为 date/price/volume（降序）
        latest = prices[0]
        oldest = prices[-1]
        latest_price = latest.get('price') or latest.get('adjClose') or latest.get('close')
        oldest_price = oldest.get('price') or oldest.get('adjClose') or oldest.get('close')
        total_return = (latest_price / oldest_price - 1) * 100 if latest_price and oldest_price else None
        print(f"  数据范围   : {oldest.get('date')} → {latest.get('date')}")
        print(f"  交易日数量 : {total_days} 天")
        print(f"  最新价格   : ${latest_price}")
        print(f"  最新成交量 : {latest.get('volume', 0):,.0f}")
        print(f"  近5天      :", " | ".join(
            f"{r.get('date','')[:10]} ${r.get('price') or r.get('close')}"
            for r in prices[:5]
        ))
        if total_return:
            print(f"  期间总涨幅 : {total_return:.1f}%")
    else:
        print("  [!] 无法获取价格历史")

    # ── 季报（近4季）───────────────────────────────
    sep("15. 季报损益（近4季）")
    income_q = fetcher.get_income_statement(ticker, period="quarter", limit=4)
    if income_q:
        print(f"  {'季度':<10} {'营收':>12} {'净利润':>12} {'净利率':>10} {'EPS':>8}")
        print(f"  {'─'*8:<10} {'─'*10:>12} {'─'*10:>12} {'─'*8:>10} {'─'*6:>8}")
        for row in income_q[:4]:
            rev = row.get('revenue') or 1
            ni = row.get('netIncome') or 0
            print(f"  {row.get('date','')[:7]:<10}"
                  f" {fmt(row.get('revenue'), 'B'):>12}"
                  f" {fmt(row.get('netIncome'), 'B'):>12}"
                  f" {fmt(ni/rev, 'pct'):>10}"
                  f" {fmt(row.get('epsdiluted')):>8}")
    else:
        print("  [!] 无法获取季报数据")

    print(f"\n{'='*60}")
    print(f"  {ticker} 数据拉取完成")
    print(f"{'='*60}\n")


def main():
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["AAPL"]

    print("\n初始化数据获取器...")
    fetcher = DataFetcher()

    try:
        for ticker in tickers:
            test_ticker(ticker.upper(), fetcher)
    finally:
        fetcher.close()
        print("数据库连接已关闭")


if __name__ == "__main__":
    main()
