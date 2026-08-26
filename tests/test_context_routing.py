from __future__ import annotations

from dataclasses import replace

import pytest

from text2sql.config import Settings
from text2sql.context_router import ContextRouter
from text2sql.errors import ClarificationError, PlanningError, ValidationError
from text2sql.llm_planner import ContextRewrite, LLMPlanner, parse_context_rewrite
from text2sql.models import QueryPlan, QueryResult, SessionState, TurnMemory
from text2sql.service import Text2SQLService
from text2sql.validators import answer_obligations


COMPOSITE_SELF_CONTAINED = (
    "江苏省F市农商行的不良贷款率在2026-01-31和全省均值比，是高还是低？差多少？"
    "截至2026-03-31，各项存款余额排名前三的是哪几家？各多少？"
)


def test_business_relative_time_is_not_conversation_history(service):
    state = SessionState(
        last_organizations=("ORG013",),
        last_metrics=("ZB011",),
        last_date="2026-03-31",
    )

    decision = service.context_router.classify(
        "这个月A市的不良率有多少？跟上个月比怎么样？跟B市比呢？",
        state,
    )

    assert decision.dependency == "self_contained"
    assert decision.history_limit == 0


def test_province_wide_scope_does_not_inherit_a_previous_selected_organization(service):
    state = SessionState(
        last_organizations=("ORG010",),
        last_metrics=("ZB013",),
        last_date="2026-04-30",
    )

    decision = service.context_router.classify(
        "江苏省全市农商行在2026年3月31日，各项存款余额总额是多少？",
        state,
    )

    assert decision.dependency == "self_contained"
    assert decision.history_limit == 0


def test_redundant_history_phrase_does_not_force_context(service):
    state = SessionState(
        last_organizations=("ORG003",),
        last_metrics=("ZB001",),
        last_date="2026-03-31",
    )
    decision = service.context_router.classify(
        "结合前面提到的季度高点，C市净利润从2025年三季度287.85万元回落到2026年一季度279.95万元，回落了多少？"
        "以同期存款115.81亿元看，净利润/存款比约为多少？",
        state,
    )

    assert decision.dependency == "self_contained"


def test_profile_reference_still_requires_history(service):
    state = SessionState(
        last_organizations=("ORG004",),
        last_metrics=("ZB013",),
        last_date="2025-03-31",
    )
    decision = service.context_router.classify(
        "回到一开始D市2025年一季度末1.60%的不良率，它是否低于5%的监管上限？整体风控画像如何？",
        state,
    )

    assert decision.needs_history is True


def test_background_trend_word_is_not_an_answer_obligation():
    obligations = answer_obligations(
        "尽管逐季有所回落，但和2024年末比，2026年一季度净增长了多少？"
    )
    assert "trend" not in obligations


def test_context_rule_requires_unique_inherited_slots(service):
    unique_state = SessionState(
        last_organizations=("ORG012",),
        last_metrics=("ZB015",),
        last_date="2026-04-30",
    )
    ambiguous_state = SessionState(
        last_organizations=("ORG012",),
        last_metrics=("ZB008", "ZB009"),
        last_date="2026-04-30",
    )

    safe = service.context_router.classify(
        "资本充足率排名如何？是否达标？",
        unique_state,
    )
    ambiguous = service.context_router.classify(
        "这个指标全省排第几？",
        ambiguous_state,
    )

    assert safe.dependency == "context_required"
    assert safe.allow_context_rule is True
    assert ambiguous.dependency == "uncertain"
    assert ambiguous.allow_context_rule is False


def test_composite_self_contained_question_fails_partial_rule_coverage(service):
    decision = service.rule_planner.plan(
        COMPOSITE_SELF_CONTAINED,
        SessionState(),
        use_context=False,
    )

    assert decision.plan is not None
    required, missing = service.answer_coverage.analyze(
        COMPOSITE_SELF_CONTAINED,
        decision.plan,
    )
    assert {"rank", "province_average", "difference"}.issubset(required)
    assert missing
    with pytest.raises(ValidationError):
        service.answer_coverage.validate(COMPOSITE_SELF_CONTAINED, decision.plan)


class CapturingFailPlanner:
    def __init__(self) -> None:
        self.plan_calls: list[dict[str, object]] = []

    def plan(self, **kwargs):
        self.plan_calls.append(kwargs)
        raise PlanningError("stop after payload capture")


def test_self_contained_llm_fallback_does_not_receive_history():
    planner = CapturingFailPlanner()
    settings = replace(Settings.from_env(), llm_retries=0)
    service = Text2SQLService(settings=settings, llm_planner=planner)
    service.ask(
        "江苏省A市农商行在2025年6月15日，各项存款余额是多少？",
        "no-history-llm",
    )

    with pytest.raises(PlanningError, match="payload capture"):
        service.ask(COMPOSITE_SELF_CONTAINED, "no-history-llm")

    call = planner.plan_calls[0]
    assert call["include_history"] is False
    assert call["state"] == SessionState()


class RewriteOnlyPlanner:
    def __init__(self, rewritten_question: str, unresolved: tuple[str, ...] = ()) -> None:
        self.rewritten_question = rewritten_question
        self.unresolved = unresolved
        self.rewrite_calls: list[dict[str, object]] = []

    def rewrite(self, question, state, **kwargs):
        self.rewrite_calls.append(
            {
                "question": question,
                "state": state,
                **kwargs,
            }
        )
        return ContextRewrite(
            rewritten_question=self.rewritten_question,
            used_turn_indexes=(1,),
            unresolved_references=self.unresolved,
        )

    def plan(self, **kwargs):
        raise AssertionError("完整改写应重新命中普通规则，不应再次调用SQL规划LLM")


def _seed_multi_metric_state(service: Text2SQLService, session_id: str) -> None:
    response = service.ask(
        "江苏省L市农商行在2026-04-30的员工人数和网点数量分别是多少？",
        session_id,
    )
    assert response.route == "rule"


def test_ambiguous_context_uses_llm_rewrite_then_reuses_normal_rule():
    planner = RewriteOnlyPlanner(
        "江苏省L市农商行在2026-04-30的员工人数全省排第几？"
    )
    settings = replace(Settings.from_env(), llm_retries=0)
    service = Text2SQLService(settings=settings, llm_planner=planner)
    _seed_multi_metric_state(service, "rewrite-context")

    response = service.ask("这个指标全省排第几？", "rewrite-context")

    assert response.route == "rule"
    assert response.plan["metrics"] == ("ZB018",)
    assert planner.rewrite_calls[0]["history_limit"] == 10


def test_long_distance_reference_uses_twenty_turn_window():
    planner = RewriteOnlyPlanner(
        "江苏省L市农商行在2026-04-30的网点数量全省排第几？"
    )
    settings = replace(Settings.from_env(), llm_retries=0)
    service = Text2SQLService(settings=settings, llm_planner=planner)
    _seed_multi_metric_state(service, "long-context")

    response = service.ask("回到一开始，这个指标全省排第几？", "long-context")

    assert response.route == "rule"
    assert planner.rewrite_calls[0]["history_limit"] == 20


def test_unresolved_context_requests_clarification_instead_of_guessing():
    planner = RewriteOnlyPlanner("", unresolved=("这个指标具体指什么",))
    settings = replace(Settings.from_env(), llm_retries=0)
    service = Text2SQLService(settings=settings, llm_planner=planner)
    _seed_multi_metric_state(service, "clarify-context")

    with pytest.raises(ClarificationError, match="这个指标具体指什么"):
        service.ask("这个指标全省排第几？", "clarify-context")


def test_history_payload_is_bounded_to_ten_or_twenty_turns():
    plan = QueryPlan(
        source="rule",
        query_type="point_query",
        operation="value",
        organizations=("ORG001",),
        organization_scope="selected",
        metrics=("ZB001",),
        current_date="2025-06-15",
    )
    result = QueryResult(("metric_value",), ((42.02,),))
    turns = tuple(
        TurnMemory(f"question-{index}", plan, result, f"answer-{index}")
        for index in range(15)
    )
    state = SessionState(recent_turns=turns)

    recent_ten = LLMPlanner._history_context(state, 10)
    recent_twenty = LLMPlanner._history_context(state, 20)

    assert len(recent_ten["recent_turns"]) == 10
    assert recent_ten["recent_turns"][0]["question"] == "question-5"
    assert len(recent_twenty["recent_turns"]) == 15


def test_parse_context_rewrite_is_strict():
    parsed = parse_context_rewrite(
        '{"rewritten_question":"A市2026-03-31不良率是多少？",'
        '"used_turn_indexes":[1],"unresolved_references":[]}'
    )
    assert parsed.rewritten_question.startswith("A市")

    with pytest.raises(PlanningError):
        parse_context_rewrite(
            '{"rewritten_question":"A市不良率",'
            '"used_turn_indexes":[true],"unresolved_references":[]}'
        )


def test_rewrite_validation_distinguishes_background_from_target_metric(service):
    service._validate_context_rewrite(
        "前面提到不良率改善最多的L市，它2026年4月末的拨备覆盖率是否达到150%监管要求？",
        "江苏省L市农商行2026年4月末的拨备覆盖率是否达到150%监管要求？",
    )


def test_rewrite_validation_preserves_all_joint_condition_metrics(service):
    service._validate_context_rewrite(
        "L市是否同时满足不良率低于全省均值且拨备高于全省均值且资本充足率不低于10.5%且成本收入比低于全省均值？",
        "2026年4月末，江苏省L市农商行是否同时满足以下条件：不良贷款率低于全省均值、拨备覆盖率高于全省均值、资本充足率不低于10.5%、成本收入比低于全省均值？",
    )


def test_profile_obligation_accepts_semantically_equivalent_wording():
    assert "profile" in answer_obligations("是否属于风控表现最优的机构之一？")
