from __future__ import annotations


def test_rank_and_threshold_are_answered_by_one_deterministic_plan(service):
    session_id = "multi-turn-rank-threshold"
    service.ask("到2026年4月末，C市净利润较2024年末增幅是多少？", session_id)

    response = service.ask(
        "它的资本充足率在全省排第几？是否达到10.5%最低要求？",
        session_id,
    )

    assert response.route == "rule"
    assert response.plan["operation"] == "rank_threshold"
    assert {"metric_rank", "threshold_met"}.issubset(response.columns)
    assert "第5名" in response.answer
    assert "达到" in response.answer


def test_pairwise_context_keeps_a_bounded_organization_set(service):
    session_id = "multi-turn-pairwise-scope"
    service.ask(
        "江苏省J市农商行2026年4月末的不良贷款率是多少？",
        session_id,
    )

    comparison = service.ask(
        "它和L市比，哪家的不良贷款率更低？",
        session_id,
    )

    assert comparison.route == "rule"
    assert comparison.plan["organization_scope"] == "selected"
    assert set(comparison.plan["organizations"]) == {"ORG010", "ORG012"}
    assert len(comparison.rows) == 2
    assert "江苏省J市农商行更低" in comparison.answer
    assert "第1名" not in comparison.answer

    balance = service.ask("哪家的不良贷款余额更多？相差多少？", session_id)

    assert balance.route == "rule"
    assert balance.plan["organization_scope"] == "selected"
    assert set(balance.plan["organizations"]) == {"ORG010", "ORG012"}
    assert len(balance.rows) == 2
    assert "江苏省L市农商行更高" in balance.answer
    assert "0.25亿元" in balance.answer
    assert "江苏省A市农商行" not in balance.answer


def test_plural_context_supports_rank_threshold_and_reconciliation(service):
    session_id = "multi-turn-plural-context"
    service.ask(
        "2026年4月末，江苏省J市农商行和江苏省L市农商行的各项存款余额相差多少？",
        session_id,
    )

    rank_threshold = service.ask(
        "两家的资本充足率排名如何？是否都达到10.5%最低要求？",
        session_id,
    )

    assert rank_threshold.route == "rule"
    assert rank_threshold.plan["operation"] == "rank_threshold"
    assert set(rank_threshold.plan["organizations"]) == {"ORG010", "ORG012"}
    assert len(rank_threshold.rows) == 2
    assert "第1名" in rank_threshold.answer
    assert "第3名" in rank_threshold.answer
    assert rank_threshold.answer.count("达到") == 2

    reconciliation = service.ask(
        "不良率是否等于各自的不良余额除以各项贷款余额？",
        session_id,
    )

    assert reconciliation.route == "rule"
    assert reconciliation.plan["operation"] == "reconcile"
    assert set(reconciliation.plan["organizations"]) == {"ORG010", "ORG012"}
    assert len(reconciliation.rows) == 2
    assert "江苏省J市农商行" in reconciliation.answer
    assert "江苏省L市农商行" in reconciliation.answer


def test_missing_slots_are_inherited_for_metric_reconciliation(service):
    session_id = "multi-turn-deposit-reconciliation"
    service.ask(
        "江苏省B市农商行在2025年6月30日的存款中，对公和个人分别占比多少？",
        session_id,
    )

    response = service.ask(
        "对公存款和个人存款相加，是否等于各项存款余额？",
        session_id,
    )

    assert response.route == "rule"
    assert response.plan["operation"] == "reconcile"
    assert response.plan["organizations"] == ("ORG002",)
    assert response.plan["current_date"] == "2025-06-30"
    assert "等于各项存款52.11亿元" in response.answer


def test_two_explicit_dates_return_values_until_change_is_requested(service):
    session_id = "multi-turn-period-values"

    values = service.ask(
        "L市2024年末和2026年4月末的不良率分别是多少？",
        session_id,
    )

    assert values.route == "rule"
    assert values.plan["operation"] == "period_values"
    assert "2024-12-31：0.91%" in values.answer
    assert "2026-04-30：0.85%" in values.answer
    assert "下降" not in values.answer

    change = service.ask("K市同期变化情况呢？", session_id)

    assert change.route == "rule"
    assert change.plan["operation"] == "difference"
    assert "下降0.06个百分点" in change.answer


def test_count_only_and_follow_up_organization_list_are_separate(service):
    session_id = "multi-turn-count-then-list"

    count = service.ask(
        "2026年4月末同时满足不良率低于全省均值且拨备覆盖率高于全省均值的共有几家？",
        session_id,
    )

    assert count.route == "rule"
    assert count.answer == "符合条件的机构共6家"
    assert "江苏省A市农商行" not in count.answer

    members = service.ask("都是哪几家？", session_id)

    assert members.route == "rule"
    assert members.answer == (
        "江苏省A市农商行、江苏省B市农商行、江苏省E市农商行、"
        "江苏省G市农商行、江苏省J市农商行、江苏省L市农商行"
    )


def test_threshold_count_can_be_projected_to_an_empty_member_list(service):
    session_id = "multi-turn-empty-count-list"
    count = service.ask(
        "全省2026年4月末资本充足率低于10.5%最低要求的有几家？",
        session_id,
    )
    assert "共有0家" in count.answer

    members = service.ask("都是哪几家？", session_id)

    assert members.route == "rule"
    assert members.plan["operation"] == "condition_members"
    assert members.answer == "没有机构满足该条件"


def test_profit_deposit_ratio_keeps_unit_conversion_after_context_inheritance(service):
    session_id = "multi-turn-profit-deposit-ratio"
    service.ask(
        "江苏省C市农商行2026年一季度末的各项存款余额是多少？",
        session_id,
    )

    response = service.ask("同期的净利润/存款比约为多少？", session_id)

    assert response.route == "rule"
    assert response.plan["derived_formula"] == "profit_deposit_ratio"
    assert response.answer == "江苏省C市农商行：0.0242%"


def test_highest_rank_follow_up_reuses_population_not_previous_result_rows(service):
    session_id = "multi-turn-lowest-then-highest"
    service.ask(
        "截至2026年4月末，全省13家农商行里不良贷款率最低的前三家是谁？",
        session_id,
    )

    response = service.ask("那不良率最高的三家呢？", session_id)

    assert response.route == "rule"
    assert response.plan["organization_scope"] == "all"
    assert response.answer == (
        "第13名 江苏省D市农商行：1.61%；"
        "第12名 江苏省I市农商行：1.49%；"
        "第11名 江苏省H市农商行：1.42%"
    )
