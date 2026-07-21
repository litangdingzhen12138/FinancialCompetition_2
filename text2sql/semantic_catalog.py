"""Shared bank metric semantics used by rules, prompts and validators."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import duckdb


LOWER_IS_BETTER = {"ZB012", "ZB013", "ZB017"}
RATIO_METRICS = {"ZB012", "ZB013", "ZB015", "ZB016", "ZB017"}
APPROVED_TABLES = {"organizations", "metrics", "derived_rules", "metric_values"}

METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "ZB001": ("各项存款余额", "存款余额", "存款规模", "总存款", "存款"),
    "ZB002": ("各项贷款余额", "贷款余额", "贷款规模", "总贷款", "贷款"),
    "ZB003": ("对公存款余额", "对公存款"),
    "ZB004": ("个人存款余额", "个人存款"),
    "ZB005": ("对公贷款余额", "对公贷款"),
    "ZB006": ("个人贷款余额", "个人贷款"),
    "ZB007": ("中间业务收入", "中收"),
    "ZB008": ("净利息收入",),
    "ZB009": ("营业收入", "营收"),
    "ZB010": ("营业支出",),
    "ZB011": ("净利润", "利润"),
    "ZB012": ("成本收入比",),
    "ZB013": ("不良贷款率", "不良率"),
    "ZB014": ("不良贷款余额", "不良余额"),
    "ZB015": ("拨备覆盖率", "拨备"),
    "ZB016": ("资本充足率",),
    "ZB017": ("逾期贷款率", "逾期率"),
    "ZB018": ("员工人数", "员工数", "员工"),
    "ZB019": ("网点数量", "网点数", "网点"),
    "ZB020": ("个人客户数", "个人客户数量"),
    "ZB021": ("对公客户数", "对公客户数量"),
}

DERIVED_METRICS: dict[str, dict[str, str | tuple[str, ...]]] = {
    "deposit_loan_ratio": {
        "aliases": ("存贷比",), "numerator": "ZB002", "denominator": "ZB001", "unit": "%"
    },
    "profit_margin": {
        "aliases": ("净利润率", "净利润占营业收入"), "numerator": "ZB011", "denominator": "ZB009", "unit": "%"
    },
    "corporate_loan_share": {
        "aliases": ("对公贷款占各项贷款", "对公贷款占比"), "numerator": "ZB005", "denominator": "ZB002", "unit": "%"
    },
    "personal_loan_share": {
        "aliases": ("个人贷款占各项贷款", "个人贷款占比"), "numerator": "ZB006", "denominator": "ZB002", "unit": "%"
    },
    "corporate_deposit_share": {
        "aliases": ("对公存款占各项存款", "对公存款占比"), "numerator": "ZB003", "denominator": "ZB001", "unit": "%"
    },
    "personal_deposit_share": {
        "aliases": ("个人存款占各项存款", "个人存款占比"), "numerator": "ZB004", "denominator": "ZB001", "unit": "%"
    },
    "intermediate_income_share": {
        "aliases": ("中间业务收入占营业收入", "中收占比"), "numerator": "ZB007", "denominator": "ZB009", "unit": "%"
    },
    "net_interest_income_share": {
        "aliases": ("净利息收入占营业收入", "净利息收入比重"), "numerator": "ZB008", "denominator": "ZB009", "unit": "%"
    },
    "profit_per_employee": {
        "aliases": ("人均利润",), "numerator": "ZB011", "denominator": "ZB018", "unit": "万元/人"
    },
    "npl_balance_share": {
        "aliases": ("不良贷款余额占贷款总额", "不良余额占贷款"),
        "numerator": "ZB014", "denominator": "ZB002", "unit": "%"
    },
    "deposit_per_branch": {
        "aliases": ("网点平均存款规模", "平均存款规模（万元/网点）", "平均存款规模(万元/网点)"),
        "numerator": "ZB001", "denominator": "ZB019", "unit": "万元/网点"
    },
    "combined_npl_overdue_rate": {
        "aliases": ("不良+逾期合计占贷款比", "不良率和逾期率合计"),
        "numerator": "ZB013", "denominator": "ZB017", "unit": "%"
    },
}


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    metric_id: str
    name: str
    description: str
    unit: str

    @property
    def sort_direction(self) -> str:
        return "asc" if self.metric_id in LOWER_IS_BETTER else "desc"


class SemanticCatalog:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        connection = duckdb.connect(str(db_path), read_only=True)
        try:
            self.organizations = {
                str(org_id): str(org_name)
                for org_id, org_name in connection.execute(
                    "SELECT org_id, org_name FROM organizations ORDER BY org_id"
                ).fetchall()
            }
            self.metrics = {
                str(metric_id): MetricDefinition(
                    str(metric_id), str(name), str(description), str(unit)
                )
                for metric_id, name, description, unit in connection.execute(
                    "SELECT metric_id, metric_name, description, unit FROM metrics ORDER BY metric_id"
                ).fetchall()
            }
            self.rules = {
                str(name): str(description)
                for name, description in connection.execute(
                    "SELECT rule_name, description FROM derived_rules ORDER BY rule_name"
                ).fetchall()
            }
        finally:
            connection.close()

    def resolve_organizations(self, question: str) -> tuple[str, ...]:
        found: list[tuple[int, str]] = []
        upper = question.upper()
        for org_id, name in self.organizations.items():
            aliases = {
                org_id,
                name,
                name.removeprefix("江苏省"),
                name.replace("江苏省", "").replace("农商行", "行"),
                name.replace("江苏省", "").replace("农商行", ""),
            }
            positions = [upper.find(alias.upper()) for alias in aliases if alias and alias.upper() in upper]
            if positions:
                found.append((min(positions), org_id))
        return tuple(org_id for _, org_id in sorted(found))

    def resolve_metrics(self, question: str) -> tuple[str, ...]:
        matches: list[tuple[int, int, str]] = []
        for metric_id, aliases in METRIC_ALIASES.items():
            for alias in aliases:
                for match in re.finditer(re.escape(alias), question):
                    matches.append((match.start(), match.end(), metric_id))
        accepted: list[tuple[int, int, str]] = []
        for start, end, metric_id in sorted(matches, key=lambda item: (-(item[1] - item[0]), item[0])):
            if any(not (end <= other_start or start >= other_end) for other_start, other_end, _ in accepted):
                continue
            accepted.append((start, end, metric_id))
        ordered: list[str] = []
        for _, _, metric_id in sorted(accepted):
            if metric_id not in ordered:
                ordered.append(metric_id)
        return tuple(ordered)

    def resolve_derived_metric(self, question: str) -> str | None:
        for name, definition in DERIVED_METRICS.items():
            aliases = definition["aliases"]
            if any(alias in question for alias in aliases):
                return name
        return None

    def unit_for_plan(self, metrics: tuple[str, ...], derived_formula: str | None) -> str:
        if derived_formula in DERIVED_METRICS:
            return str(DERIVED_METRICS[derived_formula]["unit"])
        if len(metrics) == 1:
            return self.metrics[metrics[0]].unit
        return ""

    def schema_context(self, question: str) -> str:
        matched_metrics = self.resolve_metrics(question)
        metrics = matched_metrics or tuple(self.metrics)
        metric_lines = [
            f"- {metric_id}: {self.metrics[metric_id].name}; 单位={self.metrics[metric_id].unit}; "
            f"排序={'升序' if metric_id in LOWER_IS_BETTER else '降序'}"
            for metric_id in metrics[:21]
        ]
        org_matches = self.resolve_organizations(question)
        org_lines = [f"- {org_id}: {self.organizations[org_id]}" for org_id in (org_matches or tuple(self.organizations))]
        return "\n".join(
            [
                "只读表：",
                "- organizations(org_id, org_name)",
                "- metrics(metric_id, metric_name, description, unit)",
                "- metric_values(data_date, metric_id, org_id, metric_value)，唯一粒度为日期+指标+机构",
                "指标：",
                *metric_lines,
                "机构：",
                *org_lines,
                "规则：不良贷款率、逾期贷款率、成本收入比越低越好；其他指标默认越高越好。",
                "较年初基准为2024-12-31；比率指标不计算增幅，使用百分点差。",
            ]
        )[:12_000]


def contains_pronoun_reference(question: str) -> bool:
    return bool(re.search(r"它们|这些机构|这几家|上述机构|他们|它的|其", question))
