"""
config.py — 全局配置，所有常量、权重、阈值的唯一来源
无业务逻辑，只有数据
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# API 配置
# ─────────────────────────────────────────────
API_CONFIG = {
    "fmp_api_key": os.getenv("FMP_API_KEY", ""),
    "fmp_base_url": "https://financialmodelingprep.com/api/v3",
    "fmp_rate_limit_per_minute": 250,   # FMP 基础版限速
    "request_timeout_seconds": 30,
    "max_retries": 3,
    "retry_backoff_seconds": 2,
}

# ─────────────────────────────────────────────
# 本地缓存配置
# ─────────────────────────────────────────────
CACHE_CONFIG = {
    "db_path": "data/stock_cache.db",
    "financial_data_ttl_days": 90,   # 财务报表：季报才更新
    "valuation_data_ttl_days": 7,    # 估值数据：价格每天变
    "universe_ttl_days": 30,         # 股票列表：月度刷新
}

# ─────────────────────────────────────────────
# 三道过滤漏斗参数
# ─────────────────────────────────────────────
FUNNEL_CONFIG = {
    # 第一道：基础过滤（市值/流动性/交易所）
    "stage1_min_market_cap_usd": 300_000_000,    # 最低市值 $3亿
    "stage1_min_avg_daily_volume_usd": 500_000,  # 最低日均成交额 $50万
    "stage1_min_listing_years": 3,               # 上市年限至少3年
    "stage1_exchanges": ["NYSE", "NASDAQ", "AMEX"],

    # 第二道：财务质量筛选
    "stage2_min_profitable_years": 2,    # 过去3年至少2年盈利
    "stage2_max_debt_to_assets": 0.85,   # 总负债/总资产 < 85%
    "stage2_lookback_years": 3,

    # 第三道：全量打分（无额外过滤，仅一票否决）
}

# ─────────────────────────────────────────────
# 一票否决项（任一触发 → 标红，不参与排名）
# ─────────────────────────────────────────────
VETO_CONDITIONS = {
    "fcf_negative_years": 3,           # 连续N年自由现金流为负
    "net_loss_consecutive_years": 2,   # 净利润率<0 且连续N年
    "debt_to_fcf_max": 10.0,           # 长期负债/FCF > 10（偿债需10年以上）
    "insider_selling_threshold": 0.30, # 近1年内部人减持 > 30%
    # 审计非标准意见：数据标记字段 audit_opinion != "Unqualified"
}

# ─────────────────────────────────────────────
# 五大评分维度默认权重
# ─────────────────────────────────────────────
DIMENSION_WEIGHTS = {
    "business_quality": 0.30,
    "financial_health": 0.20,
    "growth":           0.20,
    "management":       0.15,
    "valuation":        0.15,
}

# ─────────────────────────────────────────────
# 四位大师权重矩阵
# ─────────────────────────────────────────────
MASTER_WEIGHTS = {
    "buffett": {
        "business_quality": 0.40,
        "financial_health": 0.20,
        "growth":           0.10,
        "management":       0.20,
        "valuation":        0.10,
        # 核心哲学：护城河 + 现金流
    },
    "munger": {
        "business_quality": 0.35,
        "financial_health": 0.20,
        "growth":           0.10,
        "management":       0.30,
        "valuation":        0.05,
        # 核心哲学：激励机制 + 逆向思考
    },
    "duan": {
        "business_quality": 0.35,
        "financial_health": 0.15,
        "growth":           0.25,
        "management":       0.25,
        "valuation":        0.00,
        # 核心哲学：本分文化 + 10年后（不依赖估值选股）
    },
    "lynch": {
        "business_quality": 0.20,
        "financial_health": 0.15,
        "growth":           0.40,
        "management":       0.10,
        "valuation":        0.15,
        # 核心哲学：PEG成长 + 被忽视
    },
}

# ─────────────────────────────────────────────
# 打分阈值：生意质量
# ─────────────────────────────────────────────
BUSINESS_QUALITY_THRESHOLDS = {
    # ROE（过去5年均值）
    "roe": [
        (0.25, 10), (0.20, 8), (0.15, 6), (0.10, 4), (0.0, 2), (float("-inf"), 0)
    ],
    # 毛利率（过去5年均值）
    "gross_margin": [
        (0.60, 10), (0.45, 8), (0.30, 6), (0.20, 4), (0.10, 2), (float("-inf"), 0)
    ],
    # 毛利率稳定性（变异系数 CV，越低越稳定）
    "gross_margin_cv": [
        (0.05, 10), (0.10, 8), (0.20, 6), (0.30, 4), (0.50, 2), (float("inf"), 0)
    ],
    # 资本支出/净利润（越低越轻资产）
    "capex_to_net_income": [
        (0.10, 10), (0.25, 8), (0.50, 6), (0.75, 4), (1.0, 2), (float("inf"), 0)
    ],
    # FCF/净利润（利润质量）
    "fcf_to_net_income": [
        (1.0, 10), (0.8, 8), (0.6, 6), (0.4, 4), (0.2, 2), (float("-inf"), 0)
    ],
}

# ─────────────────────────────────────────────
# 打分阈值：财务健康
# ─────────────────────────────────────────────
FINANCIAL_HEALTH_THRESHOLDS = {
    # 流动比率
    "current_ratio": [
        (3.0, 10), (2.0, 8), (1.5, 6), (1.0, 4), (0.8, 2), (float("-inf"), 0)
    ],
    # 负债/权益
    "debt_to_equity": [
        (0.3, 10), (0.5, 8), (1.0, 6), (2.0, 4), (3.0, 2), (float("inf"), 0)
    ],
    # 利息覆盖倍数
    "interest_coverage": [
        (10.0, 10), (5.0, 8), (3.0, 6), (1.5, 4), (1.0, 2), (float("-inf"), 0)
    ],
    # 经营现金流/总负债
    "ocf_to_debt": [
        (0.5, 10), (0.3, 8), (0.2, 6), (0.1, 4), (0.05, 2), (float("-inf"), 0)
    ],
}

# ─────────────────────────────────────────────
# 打分阈值：成长性
# ─────────────────────────────────────────────
GROWTH_THRESHOLDS = {
    # 营收5年CAGR
    "revenue_cagr_5y": [
        (0.20, 10), (0.15, 8), (0.10, 6), (0.05, 4), (0.0, 2), (float("-inf"), 0)
    ],
    # 净利润5年CAGR
    "net_income_cagr_5y": [
        (0.20, 10), (0.15, 8), (0.10, 6), (0.05, 4), (0.0, 2), (float("-inf"), 0)
    ],
    # FCF 3年CAGR
    "fcf_cagr_3y": [
        (0.20, 10), (0.15, 8), (0.10, 6), (0.05, 4), (0.0, 2), (float("-inf"), 0)
    ],
    # EPS 5年CAGR
    "eps_cagr_5y": [
        (0.20, 10), (0.15, 8), (0.10, 6), (0.05, 4), (0.0, 2), (float("-inf"), 0)
    ],
}

# ─────────────────────────────────────────────
# 打分阈值：管理层
# ─────────────────────────────────────────────
MANAGEMENT_THRESHOLDS = {
    # ROIC（过去5年均值）
    "roic": [
        (0.20, 10), (0.15, 8), (0.10, 6), (0.08, 4), (0.05, 2), (float("-inf"), 0)
    ],
    # 股权稀释率（近3年年均，越低越好）
    "dilution_rate": [
        (0.00, 10), (0.01, 8), (0.02, 6), (0.03, 4), (0.05, 2), (float("inf"), 0)
    ],
    # 回购/市值（近3年年均）
    "buyback_yield": [
        (0.05, 10), (0.03, 8), (0.01, 6), (0.0, 4), (float("-inf"), 2)
    ],
    # FCF转化率（经营现金流/净利润）
    "fcf_conversion": [
        (1.2, 10), (1.0, 8), (0.8, 6), (0.6, 4), (0.4, 2), (float("-inf"), 0)
    ],
}

# ─────────────────────────────────────────────
# 打分阈值：估值
# ─────────────────────────────────────────────
VALUATION_THRESHOLDS = {
    # PEG比率（彼得·林奇核心指标）
    "peg_ratio": [
        (0.5, 10), (1.0, 8), (1.5, 6), (2.0, 4), (3.0, 2), (float("inf"), 0)
    ],
    # PE比率（行业相对）
    "pe_ratio": [
        (10, 10), (15, 8), (20, 6), (25, 4), (35, 2), (float("inf"), 0)
    ],
    # EV/EBITDA
    "ev_to_ebitda": [
        (8, 10), (12, 8), (16, 6), (20, 4), (30, 2), (float("inf"), 0)
    ],
    # PB比率
    "pb_ratio": [
        (1.0, 10), (2.0, 8), (3.0, 6), (5.0, 4), (10.0, 2), (float("inf"), 0)
    ],
}

# ─────────────────────────────────────────────
# 输出配置
# ─────────────────────────────────────────────
OUTPUT_CONFIG = {
    "scores_dir": "scores",
    "raw_data_dir": "data/raw",
    "summary_filename": "_summary.csv",
    "run_log_filename": "_run_log.txt",
    "sleep_between_tickers": 2.0,   # 限速保护，约10 req/s
}
