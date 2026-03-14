"""
munger.py — 芒格大师配置

核心哲学：激励机制 + 逆向思考 + 拒绝EBITDA
权重侧重：管理层(30%) > 生意质量(35%) > 财务健康(20%)，估值权重极低(5%)

关键评分规则（来源：穷查理宝典）：
- CEO薪酬与长期股价挂钩 → 激励健康
- 股权稀释率 < 1%/年 → 管理层不掠夺股东
- 历史ROE稳定 → 资本配置能力
- 使用EBITDA替代净利润的公司直接降分
- 反向思考：这家公司怎么会失败？
"""
from config import MASTER_WEIGHTS
from src.normalizer import NormalizedFinancials


NAME = "munger"
WEIGHTS = MASTER_WEIGHTS["munger"]

# 芒格额外否决项
EXTRA_VETO_RULES = {
    "max_dilution_rate": 0.03,   # 年均股权稀释率不超过3%
    "min_roe_consistency": 3,    # 过去5年至少3年ROE > 10%
}


def extra_vetoes(fin: NormalizedFinancials) -> list[str]:
    """芒格特定的额外否决检查"""
    triggers = []

    # 股权稀释过重
    if fin.dilution_rate_3y is not None and fin.dilution_rate_3y > EXTRA_VETO_RULES["max_dilution_rate"]:
        triggers.append(f"[芒格] 股权年均稀释率 {fin.dilution_rate_3y:.1%} > {EXTRA_VETO_RULES['max_dilution_rate']:.0%}")

    # ROE 一致性：过去5年至少N年 > 10%
    roe_above_threshold = sum(1 for r in fin.roe[:5] if (r or 0) > 0.10)
    if len(fin.roe) >= 3 and roe_above_threshold < EXTRA_VETO_RULES["min_roe_consistency"]:
        triggers.append(f"[芒格] 过去5年ROE > 10% 不足 {EXTRA_VETO_RULES['min_roe_consistency']} 年")

    return triggers


def apply_weights(dim_scores: dict[str, float]) -> float:
    """按芒格权重矩阵加权计算总分"""
    return round(sum(WEIGHTS[dim] * dim_scores.get(dim, 0) for dim in WEIGHTS), 4)
