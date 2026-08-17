from __future__ import annotations

import duckdb
import pytest

from text2sql.answerer import format_answer
from text2sql.date_resolver import comparison_date, resolve_date
from text2sql.models import QueryPlan, SessionState
from text2sql.sql_compiler import compile_rule_sql


def test_point_query(service):
    response = service.ask("江苏省A市农商行在2025年6月15日，各项存款余额是多少？", "point")
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


def test_top_one_keeps_all_tied_organizations(service):
    response = service.ask("2026年4月15日，全省13家农商行里逾期贷款率最高的是哪家？", "rank-tie")
    assert response.route == "rule"
    assert [row[1] for row in response.rows] == ["江苏省D市农商行", "江苏省I市农商行"]
    assert all(row[-1] == 12 for row in response.rows)


def test_period_difference(service):
    response = service.ask(
        "江苏省A市农商行的各项存款余额截至2025-03-31，和2024年末相比变化了多少？",
        "difference",
    )
    assert response.route == "rule"
    assert "增加0.2亿元" in response.answer


def test_derived_ratio_from_catalog(service):
    response = service.ask("江苏省A市农商行在2025-01-31的存贷比是多少？", "ratio")
    assert response.route == "rule"
    assert response.answer == "江苏省A市农商行：80.68%"


def test_province_average(service):
    response = service.ask(
        "江苏省A市农商行的拨备覆盖率在2026-03-31和全省均值比，是高还是低？",
        "average",
    )
    assert response.route == "rule"
    assert "高于全省均值23.95个百分点" in response.answer


def test_rank_of_selected_org_is_among_all_orgs(service):
    response = service.ask(
        "江苏省H市农商行的不良贷款率在2026-04-30，全省13家里排第几？",
        "selected-rank",
    )
    assert response.route == "rule"
    assert "第11名" in response.answer


def test_multi_turn_pronoun_inherits_result_orgs_and_date(service):
    first = service.ask("截至2026-03-31，各项存款余额排名前三的是哪几家？", "dialog")
    second = service.ask("它们的不良率呢？", "dialog")
    assert "江苏省C市农商行" in first.answer
    assert second.route == "rule"
    assert second.plan["current_date"] == "2026-03-31"
    assert set(second.plan["organizations"]) == {"ORG003", "ORG006", "ORG007"}
    assert "0.88%" in second.answer


def test_contextual_quarter_end_uses_previous_year_and_period(service):
    state = SessionState(
        last_organizations=("ORG003",),
        last_metrics=("ZB011",),
        last_date="2025-09-30",
    )

    decision = service.rule_planner.plan(
        "那四季度末呢？相比三季度是继续上升还是回落了？",
        state,
    )

    assert decision.plan is not None
    assert decision.plan.operation == "difference"
    assert decision.plan.current_date == "2025-12-31"
    assert decision.plan.comparison_date == "2025-09-30"


def test_quarter_followup_returns_quarter_end_change_not_daily_dump(service):
    state = SessionState(
        last_organizations=("ORG003",),
        last_metrics=("ZB011",),
        last_date="2025-12-31",
    )

    decision = service.rule_planner.plan(
        "进入2026年一季度，C市净利润又怎么变了？",
        state,
    )

    assert decision.plan is not None
    assert decision.plan.operation == "difference"
    assert decision.plan.current_date == "2026-03-31"
    assert decision.plan.comparison_date == "2025-12-31"


def test_referenced_increment_rank_keeps_subject_and_ranks_all_banks(service):
    state = SessionState(
        last_organizations=("ORG003",),
        last_metrics=("ZB011",),
        last_date="2026-03-31",
        last_comparison_date="2024-12-31",
        last_operation="difference",
    )

    decision = service.rule_planner.plan("这个增量在全省13家里排第几？", state)

    assert decision.plan is not None
    assert decision.plan.operation == "period_change_rank"
    assert decision.plan.organization_scope == "selected"
    assert decision.plan.organizations == ("ORG003",)
    assert decision.plan.current_date == "2026-03-31"
    assert decision.plan.comparison_date == "2024-12-31"
    assert "rank_absolute_change" in decision.plan.assumptions
    sql = compile_rule_sql(decision.plan)
    _, result = service._validated_execute(decision.plan, sql)
    assert result.rows[0][-1] == 2
    assert round(float(result.rows[0][result.columns.index("result_value")]), 2) == 25.68


def test_metric_rank_change_uses_metric_direction_and_previous_context_date(service):
    state = SessionState(
        last_organizations=("ORG004",),
        last_metrics=("ZB015",),
        last_date="2025-03-31",
    )
    decision = service.rule_planner.plan(
        "到2025年末，D市的不良率是改善了还是恶化了？排名变化？",
        state,
    )

    assert decision.plan is not None
    assert decision.plan.operation == "multi_rank_change"
    sql = compile_rule_sql(decision.plan)
    _, result = service._validated_execute(decision.plan, sql)
    answer = format_answer(decision.plan, result, service.catalog)
    assert "1.6%" in answer
    assert "1.62%" in answer
    assert "恶化0.02个百分点" in answer
    assert "第13名→第13名" in answer


def test_threshold_count_returns_aggregate_instead_of_each_bank(service):
    response = service.ask(
        "全省2026年4月末资本充足率低于10.5%最低要求的有几家？",
        "capital-threshold-count",
    )

    assert response.route == "rule"
    assert response.plan["operation"] == "count_condition"
    assert response.rows == ((0, 13),)
    assert response.answer == "资本充足率低于10.5%的共有0家（全省13家）"


def test_inherited_subject_is_not_expanded_to_all_for_rank_or_average(service):
    state = SessionState(
        last_organizations=("ORG012",),
        last_metrics=("ZB015",),
        last_date="2026-04-30",
    )

    rank = service.rule_planner.plan("资本充足率排名如何？是否达标？", state).plan
    average = service.rule_planner.plan(
        "成本管控效率怎么样？2026年4月末成本收入比与全省均值比？",
        state,
    ).plan

    assert rank is not None and rank.organization_scope == "selected"
    assert rank.organizations == ("ORG012",)
    assert average is not None and average.organization_scope == "selected"
    assert average.organizations == ("ORG012",)


def test_rank_question_focuses_on_the_metric_nearest_the_rank_request(service):
    decision = service.rule_planner.plan(
        "J市利润亮眼，风控如何？2025年三季度末拨备覆盖率是多少、全省第几？",
        SessionState(),
    )

    assert decision.plan is not None
    assert decision.plan.operation == "rank"
    assert decision.plan.metrics == ("ZB015",)
    assert decision.plan.organizations == ("ORG010",)


def test_profit_deposit_ratio_uses_competition_workbook_convention(service):
    response = service.ask(
        "L市2026年4月末的净利润/存款比约为多少？",
        "profit-deposit-ratio",
    )

    assert response.route == "rule"
    assert response.plan["derived_formula"] == "profit_deposit_ratio"
    assert "232.12%" in response.answer


def test_two_explicit_dates_are_remembered_for_the_next_subject(service):
    first = service.ask(
        "L市2024年末和2026年4月末的不良率分别是多少？",
        "two-date-context",
    )
    second = service.ask("K市同期变化情况呢？", "two-date-context")

    assert first.route == "rule"
    assert "1.3%" in second.answer
    assert "1.24%" in second.answer
    assert "0.06个百分点" in second.answer


def test_explicit_comparison_quarter_is_not_mistaken_for_current_quarter(service):
    state = SessionState(
        last_organizations=("ORG010",),
        last_metrics=("ZB011",),
        last_date="2026-03-31",
    )

    plan = service.rule_planner.plan(
        "2026年一季度末的净利润相比2025年四季度末又怎么变了？",
        state,
    ).plan

    assert plan is not None
    assert plan.current_date == "2026-03-31"
    assert plan.comparison_date == "2025-12-31"


def test_rank_and_province_average_are_returned_together(service):
    service.ask("江苏省C市农商行2025年三季度末的净利润是多少？", "rank-average")
    response = service.ask(
        "这个利润水平在全省同期排第几？比全省平均高多少？",
        "rank-average",
    )

    assert response.route == "rule"
    assert "全省第1名" in response.answer
    assert "高于全省均值121.41万元" in response.answer


def test_composite_derived_question_does_not_drop_the_difference(service):
    state = SessionState(
        last_organizations=("ORG003",),
        last_metrics=("ZB001",),
        last_date="2026-03-31",
    )
    decision = service.rule_planner.plan(
        "结合前面提到的季度高点，C市净利润从2025年三季度287.85万元的高点回落到2026年一季度的279.95万元，回落了多少？"
        "以同期存款115.81亿元看，净利润/存款比约为多少？",
        state,
    )

    assert decision.plan is None
    assert "派生" in decision.reason


def test_explicit_comparison_wins_over_background_trend_word(service):
    state = SessionState(
        last_organizations=("ORG003",),
        last_metrics=("ZB011",),
        last_date="2026-03-31",
    )
    plan = service.rule_planner.plan(
        "尽管逐季有所回落，但和2024年末比，2026年一季度净增长了多少？",
        state,
    ).plan

    assert plan is not None
    assert plan.operation == "difference"
    assert plan.comparison_date == "2024-12-31"


def test_selected_rank_without_top_n_does_not_filter_out_the_subject(service):
    state = SessionState(
        last_organizations=("ORG012",),
        last_metrics=("ZB013",),
        last_date="2026-04-30",
    )
    decision = service.rule_planner.plan("拨备覆盖率呢？全省排名？", state)

    assert decision.plan is not None
    assert decision.plan.limit is None
    sql = compile_rule_sql(decision.plan)
    _, result = service._validated_execute(decision.plan, sql)
    assert result.rows[0][-1] == 3


def test_date_resolution_is_not_tied_to_the_benchmark_year():
    question = "2028年二季度末的数值相比2027年末变化了多少？"

    current = resolve_date(question)
    comparison, kind = comparison_date(question, current or "")

    assert current == "2028-06-30"
    assert comparison == "2027-12-31"
    assert kind == "explicit_comparison"


def test_year_beginning_uses_fixed_workbook_baseline():
    comparison, kind = comparison_date("较年初增长了多少？", "2029-08-31")

    assert comparison == "2024-12-31"
    assert kind == "year_beginning"


def test_query_database_does_not_import_gold_answers(service):
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


def test_simple_quarter_daily_average_uses_rule_path(service):
    response = service.ask("江苏省M市农商行2025年一季度的日均存款余额是多少？", "quarter-average")
    assert response.route == "rule"
    assert response.plan["start_date"] == "2025-01-01"
    assert response.plan["end_date"] == "2025-03-31"


def test_multi_metric_point_query_is_not_a_calculation(service):
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


def test_known_unit_conversion_is_a_catalog_formula(service):
    response = service.ask(
        "江苏省E市农商行在2026-02-28的网点平均存款规模（万元/网点）是多少？",
        "deposit-per-branch",
    )
    assert response.route == "rule"
    assert response.plan["derived_formula"] == "deposit_per_branch"
    assert response.answer == "江苏省E市农商行：56071.43万元/网点"


def test_simple_quarterly_series_uses_rule_path(service):
    response = service.ask(
        "江苏省A市农商行的各项存款余额从2025年一季度末到2026年一季度末逐季是多少？",
        "quarter-range",
    )
    assert response.route == "rule"
    assert response.plan["operation"] == "quarterly_trend"
    assert len(response.rows) == 5


def test_quarterly_series_can_summarize_extrema_without_llm(service):
    response = service.ask(
        "请分析江苏省A市农商行的各项存款余额从2025年一季度末到2026年一季度末的逐季变化，最高和最低分别在哪个季度？",
        "quarter-extrema",
    )
    assert response.route == "rule"
    assert "最高为2026-03-31：42.32亿元" in response.answer
    assert "最低为2025-09-30：41.35亿元" in response.answer


def test_pairwise_comparison_answers_both_winner_and_gap(service):
    response = service.ask(
        "2025年3月31日，江苏省C市农商行和江苏省H市农商行谁的各项存款余额更高？相差多少？",
        "pairwise-gap",
    )
    assert response.route == "rule"
    assert "江苏省C市农商行更高" in response.answer
    assert "两家相差78.03亿元" in response.answer


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("江苏省H市农商行在2025年4月30日的拨备覆盖率是否达到150%的监管要求？", "满足"),
        ("江苏省A市农商行在2026年4月30日的不良贷款率是否控制在1%以内？", "满足"),
    ],
)
def test_regulatory_threshold_variants_return_a_judgement(service, question, expected):
    response = service.ask(question, f"threshold-{expected}")
    assert response.route == "rule"
    assert expected in response.answer


def test_province_average_with_gap_stays_on_generic_average_rule(service):
    response = service.ask(
        "江苏省F市农商行2025年2月28日的各项存款余额跟全省平均比是高还是低？差多少？",
        "average-gap",
    )
    assert response.route == "rule"
    assert "高于全省均值31.8亿元" in response.answer


def test_additional_derived_metric_aliases(service):
    per_employee = service.ask("江苏省K市农商行在2026年3月31日的人均净利润是多少？", "per-employee")
    npl_share = service.ask("江苏省D市农商行在2026年4月30日的按余额计算的不良贷款占比是多少？", "npl-share")
    assert per_employee.route == "rule"
    assert per_employee.answer.endswith("万元/人")
    assert "101.49万元" not in per_employee.answer
    assert npl_share.route == "rule"
    assert npl_share.answer == "江苏省D市农商行：1.6%"


@pytest.mark.parametrize(
    "question",
    [
        "截至2025年5月31日，按各项贷款余额看，表现相对靠后的三家分别是谁？",
        "截至2025年12月31日，按成本收入比看，表现相对靠后的三家分别是谁？",
        "截至2026年2月28日，按拨备覆盖率看，表现相对靠后的三家分别是谁？",
        "截至2026年4月30日，按个人客户数看，表现相对靠后的三家分别是谁？",
    ],
)
def test_relative_bottom_three_is_a_basic_rank_primitive(service, question):
    response = service.ask(question, f"bottom-{hash(question)}")
    assert response.route == "rule"
    assert response.plan["operation"] == "rank"
    assert len(response.rows) == 3


def test_last_three_wording_returns_three_rows(service):
    response = service.ask("截至2025-04-30，不良贷款率排名最后的三家是哪些？", "last-three")
    assert response.route == "rule"
    assert [row[1] for row in response.rows] == [
        "江苏省D市农商行", "江苏省H市农商行", "江苏省I市农商行"
    ]


def test_deposit_composition_is_calculated_from_three_metrics(service):
    response = service.ask(
        "江苏省B市农商行在2025-06-30的存款中，对公和个人分别占比多少？",
        "deposit-composition",
    )
    assert response.route == "rule"
    assert response.answer == "对公存款占比35.52%，个人存款占比64.48%"


def test_multi_metric_sum_returns_components_and_total(service):
    response = service.ask(
        "江苏省E市农商行2025年底的对公客户数和个人客户数分别是多少？合计多少？",
        "metric-sum",
    )
    assert response.route == "rule"
    assert "对公客户数1461户" in response.answer
    assert "个人客户数146477户" in response.answer
    assert "合计147938户" in response.answer


def test_daily_average_with_extrema_is_one_deterministic_query(service):
    response = service.ask(
        "江苏省A市农商行2025年全年的各项贷款余额日均值是多少？最高日和最低日分别出现在什么水平？",
        "daily-statistics",
    )
    assert response.route == "rule"
    assert "日均33.88亿元" in response.answer
    assert "最高日" in response.answer and "34.58亿元" in response.answer
    assert "最低日" in response.answer and "32.96亿元" in response.answer


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "分析江苏省A市农商行在2026-04-30的各项存款余额环比（较上月）和同比（较去年同期）的变化情况。",
            ("环比下降1.47%", "同比下降0.83%"),
        ),
        (
            "江苏省K市农商行在2026-04-30的净利润环比和同比分别变动了多少？",
            ("环比增长20.63%", "同比增长24.51%"),
        ),
    ],
)
def test_mom_and_yoy_are_calculated_together(service, question, expected):
    response = service.ask(question, f"mom-yoy-{hash(question)}")
    assert response.route == "rule"
    assert all(value in response.answer for value in expected)


def test_period_count_vs_province_average_uses_all_orgs_before_filter(service):
    response = service.ask(
        "2025年全年，江苏省G市农商行的成本收入比有多少天高于全省均值？",
        "count-vs-average",
    )
    assert response.route == "rule"
    assert response.answer == "江苏省G市农商行：11天高于全省均值（共365天，占比3.01%）"


def test_profitability_profile_includes_income_structure(service):
    response = service.ask(
        "评估江苏省K市农商行在2026-04-30的盈利能力，包含净利润、成本收入比、收入结构和较年初变化。",
        "profitability-profile",
    )
    assert response.route == "rule"
    assert "净利息收入51.72万元" in response.answer
    assert "中间业务收入172.05万元" in response.answer
    assert "较年初净利润增长21.25万元" in response.answer


def test_performance_profile_has_valid_metric_plan(service):
    response = service.ask(
        "江苏省D市农商行在2026-01-31的表现较好和较差的指标分别有哪些？",
        "performance-profile",
    )
    assert response.route == "rule"
    assert "表现较好：资本充足率（第1名）" in response.answer
    assert "不良贷款率（第13名）" in response.answer
    assert "净利润（第11名）" in response.answer

    later = service.ask(
        "江苏省F市农商行在2025-11-30的指标中哪些表现较好？哪些表现较差？",
        "performance-profile-bottom-four",
    )
    assert "表现较差：拨备覆盖率（第10名）" in later.answer


@pytest.mark.parametrize(
    ("question", "session_id"),
    [
        (
            "江苏省D市农商行在2026-01-31的表现较好和较差的指标分别有哪些？",
            "performance-contract",
        ),
        (
            "请列出江苏省G市农商行在2025-11-30的主要经营指标及排名，哪些指标表现较好，哪些表现较差？",
            "major-profile-contract",
        ),
    ],
)
def test_profile_labels_follow_top_three_and_bottom_four(service, question, session_id):
    response = service.ask(question, session_id)
    rank_index = response.columns.index("metric_rank")
    label_index = response.columns.index("performance_label")

    for row in response.rows:
        rank = int(row[rank_index])
        expected = "较好" if rank <= 3 else "较差" if rank >= 10 else "中性"
        assert row[label_index] == expected


def test_joint_metric_province_average_conditions_are_local_and_readable(service):
    response = service.ask(
        "2026年3月末，哪些农商行同时满足不良率低于全省均值且拨备覆盖率高于全省均值？",
        "joint-province-average",
    )
    assert response.route == "rule"
    assert response.plan["operation"] == "multi_metric_province_compare"
    assert "江苏省A市农商行：不良贷款率0.92%（低于全省均值1.14%）" in response.answer
    assert "拨备覆盖率202.47%（高于全省均值178.52%）" in response.answer
    assert response.answer.count("农商行：") == 6


def test_joint_metric_condition_count_is_answered_before_organization_details(service):
    response = service.ask(
        "2026年4月末同时满足不良率低于全省均值且拨备覆盖率高于全省均值的共有几家？",
        "joint-province-average-count-first",
    )

    assert response.route == "rule"
    assert response.answer.startswith("共有6家，分别为：江苏省A市农商行")
    assert response.answer.count("农商行：") == 6
    assert not response.answer.endswith("共6家")


def test_value_then_change_question_is_answered_in_the_same_order(service):
    session_id = "value-before-change"
    service.ask("江苏省J市农商行2024年末的净利润是多少？", session_id)

    response = service.ask(
        "2025年三季度末净利润是多少？比二季度末增长了多少？",
        session_id,
    )

    assert response.answer == (
        "江苏省J市农商行：净利润175.6万元；"
        "增加25.34万元（比较期150.26万元）"
    )


def test_multi_metric_values_keep_metric_names(service):
    response = service.ask(
        "列出江苏省F市农商行在2026-04-30的风险指标数据，含不良率、拨备覆盖率、逾期率和资本充足率。",
        "named-risk-values",
    )
    assert "不良贷款率：1.35%" in response.answer
    assert "拨备覆盖率：167.48%" in response.answer
    assert "资本充足率：11.99%" in response.answer
    assert "逾期贷款率：1.15%" in response.answer


def test_tiny_province_average_gap_is_not_rounded_to_zero(service):
    response = service.ask(
        "江苏省M市农商行在2026-04-30的不良率和全省均值比怎么样？",
        "tiny-average-gap",
    )
    assert "低于全省均值0.0031个百分点" in response.answer


def test_major_operating_profile_is_local_and_complete(service):
    response = service.ask(
        "请列出江苏省G市农商行在2025-11-30的主要经营指标及排名，哪些指标表现较好，哪些表现较差？",
        "major-profile",
    )
    assert response.route == "rule"
    assert "存贷比81.21%" in response.answer
    assert "不良贷款率0.96%" in response.answer
    assert "拨备覆盖率188.94%" in response.answer
    assert "净利润239.28万元" in response.answer
    assert "较年初" in response.answer


def test_combined_risk_rate_sums_only_the_two_requested_rates(service):
    response = service.ask(
        "江苏省M市农商行在2025年12月底的不良+逾期合计占贷款比？",
        "combined-risk",
    )
    assert response.route == "rule"
    assert "不良贷款率1.21%" in response.answer
    assert "逾期贷款率1.04%" in response.answer
    assert "合计2.25%" in response.answer


def test_period_mean_front_and_back_ranking_is_deterministic(service):
    response = service.ask("2025年全年不良贷款率均值前3后3是谁？", "mean-ranks")
    assert response.route == "rule"
    assert "前3名：江苏省J市农商行" in response.answer
    assert "后3名：江苏省H市农商行" in response.answer


def test_cross_entity_difference_avoids_llm(service):
    response = service.ask(
        "2025年6月末，江苏省C市农商行比江苏省G市农商行的存款多多少？",
        "cross-difference",
    )
    assert response.route == "rule"
    assert "相差4.72亿元" in response.answer


def test_point_count_vs_average_is_local(service):
    response = service.ask(
        "2025年12月31日，有多少家农商行的贷款余额超过了全省平均值？",
        "point-count-average",
    )
    assert response.route == "rule"
    assert "家机构高于全省均值" in response.answer
    assert "共13家" in response.answer


def test_period_global_extrema_is_local(service):
    response = service.ask(
        "2025年全年，各项贷款余额的单日最高值出现在哪家？单日最低值在哪家？",
        "period-extrema",
    )
    assert response.route == "rule"
    assert "最高值：江苏省C市农商行" in response.answer
    assert "最低值：江苏省H市农商行" in response.answer


def test_period_growth_ranking_is_local(service):
    response = service.ask(
        "从2024年末到2026-03-31，全省各项存款余额增幅排名前三的是哪几家？增幅各是多少？",
        "period-growth-rank",
    )
    assert response.route == "rule"
    assert "第1名 江苏省H市农商行：增长2.58%" in response.answer
    assert len(response.rows) == 3


def test_multi_metric_period_direction_is_local(service):
    response = service.ask(
        "江苏省A市农商行从2025年上半年末到年末，存款、贷款、不良率和净利润的变动方向分别是什么？",
        "multi-period-direction",
    )
    assert response.route == "rule"
    assert "各项存款余额上升" in response.answer
    assert "各项贷款余额下降" in response.answer
    assert "不良贷款率下降" in response.answer


def test_three_dimension_profile_is_local(service):
    response = service.ask(
        "从规模、资产质量、盈利能力三个维度，分别列出江苏省L市农商行在2026-04-30的各项指标及排名。",
        "three-dimension",
    )
    assert response.route == "rule"
    assert "规模：存款88.17亿元（第4名）" in response.answer
    assert "资产质量：不良贷款率0.85%（第2名）" in response.answer
    assert "盈利能力：净利润204.66万元（第4名）" in response.answer


def test_multi_metric_rank_and_rank_change_are_local(service):
    snapshot = service.ask(
        "2025年底，江苏省L市农商行在规模（贷款）、质量（不良率）、效益（净利润）三方面排名各是多少？",
        "multi-rank",
    )
    changes = service.ask(
        "江苏省K市农商行从2024年末到2026年4月末，存款、贷款、不良率、净利润的排名分别变化了多少？",
        "multi-rank-change",
    )
    assert snapshot.route == "rule"
    assert "各项贷款余额：71.37亿元，全省第4名" in snapshot.answer
    assert changes.route == "rule"
    assert "净利润：第11名→第9名，排名提升2名" in changes.answer


def test_component_reconciliation_is_local(service):
    response = service.ask(
        "2025年12月末，江苏省C市农商行的对公存款加个人存款是不是等于各项存款？差额多少？",
        "reconcile",
    )
    assert response.route == "rule"
    assert "等于各项存款" in response.answer
    assert "差额0亿元" in response.answer


@pytest.mark.parametrize(
    "question",
    [
        "评估江苏省A市农商行2025年末的盈利能力。",
    ],
)
def test_complex_queries_are_deliberately_not_encoded_as_rules(service, question):
    decision = service.rule_planner.plan(question, SessionState())
    assert decision.plan is None
    assert "LLM" in decision.reason
