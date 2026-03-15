"""
buffett.py — 巴菲特大师配置

核心哲学：护城河 + Owner's Earnings + 定价权
权重侧重：生意质量(40%) > 管理层(20%) = 财务健康(20%) > 成长性(10%) = 估值(10%)

关键评分规则（来源：巴菲特致股东信 1965-2024）：
- ROE > 15% 且连续10年稳定 → 护城河信号
- 毛利率在通胀期不下滑 → 定价权
- 资本支出/净利润 < 25% → 轻资产
- FCF/净利润 > 0.8 → 利润质量高
"""
from config import MASTER_WEIGHTS, SCORING_VERSION
from src.normalizer import NormalizedFinancials


NAME = "buffett"
WEIGHTS = MASTER_WEIGHTS["buffett"]

def _get_weights():
    if SCORING_VERSION == "v2":
        from config import V2_MASTER_WEIGHTS
        return V2_MASTER_WEIGHTS["buffett"]
    return WEIGHTS

# 巴菲特额外否决项（在全局否决之上）
EXTRA_VETO_RULES = {
    "min_roe_5y_avg": 0.12,          # 5年均值 ROE 不得低于12%
    "min_gross_margin": 0.25,        # 毛利率不得低于25%（定价权门槛）
    "max_capex_to_net_income": 0.50, # 资本密集型企业不符合巴菲特偏好
}


def extra_vetoes(fin: NormalizedFinancials) -> list[str]:
    """巴菲特特定的额外否决检查"""
    triggers = []

    if fin.roe_5y_avg is not None and fin.roe_5y_avg < EXTRA_VETO_RULES["min_roe_5y_avg"]:
        triggers.append(f"[巴菲特] ROE 5年均值 {fin.roe_5y_avg:.1%} < {EXTRA_VETO_RULES['min_roe_5y_avg']:.0%}")

    gm = fin.gross_margin[0] if fin.gross_margin else None
    if gm is not None and gm < EXTRA_VETO_RULES["min_gross_margin"]:
        triggers.append(f"[巴菲特] 毛利率 {gm:.1%} < {EXTRA_VETO_RULES['min_gross_margin']:.0%}")

    return triggers


def apply_weights(dim_scores: dict[str, float]) -> float:
    """按巴菲特权重矩阵加权计算总分"""
    w = _get_weights()
    return round(sum(w[dim] * dim_scores.get(dim, 0) for dim in w), 4)
