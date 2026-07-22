"""Authoritative derived-dimension rules from the competition workbook.

These definitions are a data contract, not heuristics.  Natural-language
planning may generalize to unseen questions, but it must never reinterpret the
calculation semantics declared by the ``衍生维度说明`` worksheet.
"""

from __future__ import annotations


YEAR_BEGINNING_BASELINE = "2024-12-31"
PROVINCE_ORGANIZATION_COUNT = 13
PERFORMANCE_GOOD_MAX_RANK = 3
PERFORMANCE_BAD_COUNT = 4

LOWER_IS_BETTER = frozenset({"ZB012", "ZB013", "ZB017"})
RATIO_METRICS = frozenset({"ZB012", "ZB013", "ZB015", "ZB016", "ZB017"})

# Keep the worksheet order so the same contract can be injected into LLM
# context without the model having to reconstruct or reinterpret it.
AUTHORITATIVE_DERIVED_RULES: tuple[tuple[str, str], ...] = (
    ("较年初", "当日值 - 2024年12月31日值"),
    ("较上季", "当日值 - 上季度末值"),
    ("较上月", "当日值 - 上月月末值"),
    ("较同期", "当日值 - 去年同期值"),
    ("全省均值", "13家机构当日值的算数平均值"),
    (
        "排名",
        "不良贷款率/逾期贷款率/成本收入比按从低到高排名（越低越好），其余按从高到低排名，使用rank算法",
    ),
    ("增量", "当日值 - 比较日值"),
    (
        "增幅",
        "(当日值-比较日值)/比较日值×100%，比率类指标（ZB012/013/015/016/017）不做增幅计算",
    ),
    ("表现较好", "前三"),
    ("表现较差", "后四"),
)

AUTHORITATIVE_DERIVED_RULE_MAP = dict(AUTHORITATIVE_DERIVED_RULES)


def derived_rule_prompt(rules: dict[str, str] | None = None) -> str:
    """Render all rules in canonical worksheet order for planner context."""
    source = rules or AUTHORITATIVE_DERIVED_RULE_MAP
    return "\n".join(f"- {name}: {source[name]}" for name, _ in AUTHORITATIVE_DERIVED_RULES)
