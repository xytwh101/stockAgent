"""
duan.py — 段永平大师配置

核心哲学：本分文化 + 10年后看公司 + 不依赖估值选股
权重侧重：生意质量(35%) + 成长性(25%) + 管理层(25%)，估值权重为 0%

关键评分规则（来源：段永平雪球帖子与投资问答录）：
- 过去10年毛利率趋势向上或稳定 → 本分经营的财务表现
- 研发投入持续稳定 → 长期主义信号
- 无重大财务欺诈记录 → 诚信底线
- 10年后行业是否仍存在且公司仍领先
- 估值权重为 0%（段永平明确表示不依赖估值指标选股）
"""
from config import MASTER_WEIGHTS
from src.normalizer import NormalizedFinancials


NAME = "duan"
WEIGHTS = MASTER_WEIGHTS["duan"]

# 段永平额外否决项
EXTRA_VETO_RULES = {
    "min_gross_margin_trend_years": 5,  # 至少5年毛利率数据验证稳定性
    "max_gross_margin_decline": -0.10,  # 毛利率5年内不能下滑超过10pct
}


def extra_vetoes(fin: NormalizedFinancials) -> list[str]:
    """段永平特定的额外否决检查"""
    triggers = []

    # 毛利率长期趋势：不能大幅下滑（本分经营的财务反映）
    gm = [v for v in fin.gross_margin[:7] if v is not None]
    if len(gm) >= 5:
        recent_avg = sum(gm[:3]) / 3
        older = gm[3:]   # 年份 4 之后的所有数据（至少 2 个点）
        older_avg = sum(older) / len(older)
        if older_avg > 0 and (recent_avg - older_avg) < EXTRA_VETO_RULES["max_gross_margin_decline"]:
            triggers.append(
                f"[段永平] 毛利率长期下滑 {(recent_avg - older_avg):.1%}"
            )

    return triggers


def apply_weights(dim_scores: dict[str, float]) -> float:
    """
    按段永平权重矩阵加权计算总分
    注意：估值权重为 0，完全不考虑当前估值
    """
    return round(sum(WEIGHTS[dim] * dim_scores.get(dim, 0) for dim in WEIGHTS), 4)
