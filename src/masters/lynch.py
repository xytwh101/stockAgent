"""
lynch.py — 彼得·林奇大师配置

核心哲学：PEG成长 + 被忽视的好公司 + 普通人选股优势
权重侧重：成长性(40%) >> 生意质量(20%) > 财务健康(15%) = 估值(15%) > 管理层(10%)

关键评分规则（来源：彼得·林奇《彼得·林奇的成功投资》）：
- PEG < 0.5 → 严重低估，强烈关注
- PEG < 1.0 → 低估，值得买入
- 机构持仓比例 < 30% → 被忽视信号，加分
- 过去5年EPS增速 > 15% 且稳定 → 成长型核心
- 内部人近期增持 → 额外加分信号
- 优先关注：快速增长型 + 困境反转型
"""
from config import MASTER_WEIGHTS
from src.normalizer import NormalizedFinancials


NAME = "lynch"
WEIGHTS = MASTER_WEIGHTS["lynch"]

# 林奇额外否决项
EXTRA_VETO_RULES = {
    "min_eps_growth_years": 3,    # EPS连续增长至少3年
    "max_pe_for_growth": 50,      # 成长股PE不超过50
}


def extra_vetoes(fin: NormalizedFinancials) -> list[str]:
    """林奇特定的额外否决检查"""
    triggers = []

    # EPS连续增长检查
    eps = fin.eps
    if len(eps) >= 3:
        growth_years = sum(
            1 for i in range(min(3, len(eps) - 1))
            if (eps[i] or 0) > (eps[i + 1] or 0)
        )
        if growth_years < EXTRA_VETO_RULES["min_eps_growth_years"]:
            triggers.append(f"[林奇] EPS连续增长不足{EXTRA_VETO_RULES['min_eps_growth_years']}年")

    # PE 过高且非高成长
    if fin.pe_ratio and fin.pe_ratio > EXTRA_VETO_RULES["max_pe_for_growth"]:
        if fin.eps_cagr_5y is None or fin.eps_cagr_5y < 0.20:
            triggers.append(f"[林奇] PE={fin.pe_ratio:.1f} 过高但EPS增速不足20%")

    return triggers


def apply_weights(dim_scores: dict[str, float]) -> float:
    """按林奇权重矩阵加权计算总分（成长性权重最高40%）"""
    return round(sum(WEIGHTS[dim] * dim_scores.get(dim, 0) for dim in WEIGHTS), 4)
