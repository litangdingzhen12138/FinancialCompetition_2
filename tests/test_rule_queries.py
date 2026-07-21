from __future__ import annotations

import pytest


def test_point_query(service):
    response = service.ask(
        "江苏省A市农商行在2025年6月15日，各项存款余额是多少？",
        "point",
    )
    assert response.route == "rule"
    assert response.answer == "江苏省A市农商行：42.02亿元"
    assert "ZB001" in response.sql
    assert "ORG001" in response.sql


def test_lower_is_better_ranking(service):
    response = service.ask("2026年3月末，哪家农商行的不良贷款率最低？", "rank")
    assert response.route == "rule"
    assert "江苏省J市农商行" in response.answer
    assert "0.73%" in response.answer
    assert "ORDER BY v.metric_value ASC" in response.plan["sql"]


def test_period_difference(service):
    response = service.ask(
        "江苏省A市农商行的各项存款余额截至2025-03-31，和2024年末相比变化了多少？",
        "difference",
    )
    assert response.route == "rule"
    assert "增加0.2亿元" in response.answer


def test_derived_ratio(service):
    response = service.ask("江苏省A市农商行在2025-01-31的存贷比是多少？", "ratio")
    assert response.route == "rule"
    assert response.answer == "江苏省A市农商行：80.68%"


def test_province_average(service):
    response = service.ask(
        "江苏省A市农商行的拨备覆盖率在2026-03-31和全省均值比，是高还是低？差多少？",
        "average",
    )
    assert "高于全省均值23.95个百分点" in response.answer


def test_rank_of_selected_org_is_among_all_orgs(service):
    response = service.ask(
        "江苏省H市农商行的不良贷款率在2026-04-30，全省13家里排第几？",
        "selected-rank",
    )
    assert "第11名" in response.answer


def test_multi_turn_pronoun_inherits_result_orgs_and_date(service):
    first = service.ask("截至2026-03-31，各项存款余额排名前三的是哪几家？", "dialog")
    second = service.ask("它们的不良率呢？", "dialog")
    assert "江苏省C市农商行" in first.answer
    assert second.plan["current_date"] == "2026-03-31"
    assert set(second.plan["organizations"]) == {"ORG003", "ORG006", "ORG007"}
    assert "0.88%" in second.answer


def test_query_database_does_not_import_gold_answers(service):
    import duckdb

    connection = duckdb.connect(str(service.db_path), read_only=True)
    names = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
    connection.close()
    assert "问题答案清单" not in names
    assert "qa_examples" not in names
    assert service.executor.execute("SELECT COUNT(*) AS cnt FROM metric_values").rows[0][0] == 132678


def test_employee_alias_uses_rule_path(service):
    response = service.ask("江苏省I市农商行2025年底有多少员工？", "employee")
    assert response.route == "rule"
    assert response.answer == "江苏省I市农商行：211人"


def test_quarter_daily_average_uses_rule_path(service):
    response = service.ask("江苏省M市农商行2025年一季度的日均存款余额是多少？", "quarter-average")
    assert response.route == "rule"
    assert response.plan["start_date"] == "2025-01-01"
    assert response.plan["end_date"] == "2025-03-31"


def test_multi_metric_point_query_uses_rule_path(service):
    response = service.ask(
        "江苏省D市农商行在2025-10-31的不良贷款率和拨备覆盖率分别是多少？",
        "multi-metric",
    )
    assert response.route == "rule"
    assert set(response.plan["metrics"]) == {"ZB013", "ZB015"}
    assert len(response.rows) == 2


def test_missing_comparison_period_is_reported_without_crashing(service):
    response = service.ask(
        "江苏省E市农商行的各项存款余额在2025-11-30，同比（较去年同期）变动了多少？",
        "missing-comparison",
    )
    assert response.route == "rule"
    assert response.answer == "江苏省E市农商行：无法计算，缺少比较期数据"


def test_minimum_requirement_threshold_uses_rule_path(service):
    response = service.ask(
        "2026年一季度末，江苏省H市农商行的资本充足率满足10.5%的最低要求吗？",
        "minimum-requirement",
    )
    assert response.route == "rule"
    assert "满足" in response.answer
    assert "11.82%" in response.answer


def test_annual_average_top_and_bottom_uses_rule_path(service):
    response = service.ask("2025年全年不良贷款率均值前3后3是谁？", "annual-extrema")
    assert response.route == "rule"
    assert response.plan["operation"] == "annual_average_extrema"
    assert len(response.rows) == 6
    assert "前1名" in response.answer
    assert "后3名" in response.answer


def test_multi_metric_rank_change_uses_rule_path(service):
    response = service.ask(
        "江苏省K市农商行从2024年末到2026年4月末，存款、贷款、不良率、净利润的排名分别变化了多少？",
        "rank-change",
    )
    assert response.route == "rule"
    assert response.plan["operation"] == "multi_metric_rank_change"
    assert len(response.rows) == 4
    assert "排名" in response.answer


def test_province_average_count_returns_a_count(service):
    response = service.ask(
        "2025年12月31日，有多少家农商行的贷款余额超过了全省平均值？",
        "average-count",
    )
    assert response.route == "rule"
    assert response.plan["operation"] == "province_average_count"
    assert response.answer.startswith("共有7家")


def test_organization_sum_returns_total(service):
    response = service.ask(
        "江苏省A市农商行和江苏省B市农商行两家加起来，2025年底的存款总额有多少？",
        "organization-sum",
    )
    assert response.route == "rule"
    assert response.plan["operation"] == "organization_sum"
    assert "合计：94.64亿元" in response.answer


def test_cross_organization_difference_returns_gap(service):
    response = service.ask(
        "2025年6月末，江苏省C市农商行比江苏省G市农商行的存款多多少？",
        "cross-org-difference",
    )
    assert response.route == "rule"
    assert response.plan["operation"] == "cross_organization_difference"
    assert "4.72亿元" in response.answer


def test_component_shares_return_both_percentages(service):
    response = service.ask(
        "江苏省B市农商行在2025-06-30的存款中，对公和个人分别占比多少？",
        "component-shares",
    )
    assert response.route == "rule"
    assert response.plan["operation"] == "component_shares"
    assert "对公存款余额占比" in response.answer
    assert "个人存款余额占比" in response.answer


def test_component_sum_compare_answers_equality_and_difference(service):
    response = service.ask(
        "2025年12月末，江苏省C市农商行的对公存款加个人存款是不是等于各项存款？差额多少？",
        "component-sum-compare",
    )
    assert response.route == "rule"
    assert response.plan["operation"] == "component_sum_compare"
    assert "等于" in response.answer
    assert "差额0亿元" in response.answer


def test_derived_deposit_per_branch_converts_to_ten_thousand_yuan(service):
    response = service.ask(
        "江苏省E市农商行在2026-02-28的网点平均存款规模（万元/网点）是多少？",
        "deposit-per-branch",
    )
    assert response.route == "rule"
    assert response.plan["derived_formula"] == "deposit_per_branch"
    assert response.answer == "江苏省E市农商行：56071.43万元/网点"


def test_annual_growth_ranking_returns_only_top_three(service):
    response = service.ask(
        "从2024年末到2026-03-31，全省各项存款余额增幅排名前三的是哪几家？增幅各是多少？",
        "growth-rank",
    )
    assert response.route == "rule"
    assert response.plan["operation"] == "period_growth_rank"
    assert len(response.rows) == 3
    assert "第3名" in response.answer


def test_period_average_summary_includes_highest_and_lowest_days(service):
    response = service.ask(
        "江苏省J市农商行2025年全年的各项存款余额日均值是多少？最高日和最低日分别出现在什么水平？",
        "average-summary",
    )
    assert response.route == "rule"
    assert response.plan["operation"] == "period_average_summary"
    assert "最高日" in response.answer
    assert "最低日" in response.answer


def test_cross_year_chinese_quarter_trend_has_five_quarters_and_maximum(service):
    response = service.ask(
        "请分析江苏省A市农商行的各项存款余额从2025年一季度末到2026年一季度末的逐季变化，各季度末数值是多少？哪个季度数值最高？",
        "chinese-quarter-range",
    )
    assert response.route == "rule"
    assert len(response.rows) == 5
    assert response.plan["start_date"] == "2025-03-31"
    assert response.plan["end_date"] == "2026-03-31"
    assert "最高季度为" in response.answer


def test_same_date_metric_difference_is_explained(service):
    response = service.ask(
        "江苏省F市农商行在2026-04-30的逾期贷款率比不良贷款率高多少？",
        "metric-difference",
    )
    assert response.route == "rule"
    assert response.plan["operation"] == "metric_difference"
    assert "低于不良贷款率0.2个百分点" in response.answer


def test_multi_metric_province_average_comparison_uses_rule_path(service):
    response = service.ask(
        "对江苏省D市农商行在2025-07-31的存款、贷款、不良率、净利润四项关键指标与全省均值逐一对比。",
        "multi-province-average",
    )
    assert response.route == "rule"
    assert response.plan["operation"] == "province_average_compare"
    assert len(response.rows) == 4
    assert response.answer.count("全省均值") >= 4


def test_days_above_daily_province_average_uses_rule_path(service):
    response = service.ask(
        "2025年全年，江苏省G市农商行的成本收入比有多少天高于全省均值？",
        "days-vs-average",
    )
    assert response.route == "rule"
    assert response.plan["operation"] == "days_vs_average"
    assert "全年共11天高于全省均值" in response.answer


def test_multi_metric_period_direction_uses_rule_path(service):
    response = service.ask(
        "江苏省A市农商行从2025年上半年末到年末，存款、贷款、不良率和净利润的变动方向分别是什么？",
        "multi-metric-direction",
    )
    assert response.route == "rule"
    assert response.plan["operation"] == "multi_metric_difference"
    assert "各项存款余额" in response.answer
    assert "不良贷款率" in response.answer
