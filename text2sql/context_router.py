"""High-precision routing for conversational context usage.

The router answers one question only: does the current request need dialogue
history to become self-contained?  It deliberately does not generate SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import re

from .date_resolver import comparison_date, resolve_date
from .models import SessionState
from .semantic_catalog import SemanticCatalog


ContextDependency = Literal["self_contained", "context_required", "uncertain"]


LONG_DISTANCE_REFERENCE = re.compile(
    r"最早|一开始|最开始|很早之前|之前讨论过|回到(?:前面|最初|一开始)|前面一直"
)
EXPLICIT_DIALOGUE_REFERENCE = re.compile(
    r"它们|这些机构|这几家|上述机构|两家|双方|各自|其中|它的|它在|这个(?:指标|利润|增量|水平|数值|排名|不良率)|"
    r"对应的|上述|前面|刚才|先前|之前|一开始|最开始|回到|^那|那家|那几家"
)
PLURAL_REFERENCE = re.compile(r"它们|这些机构|这几家|上述机构|两家|双方|各自|其中|那几家")
ORGANIZATION_LIST_FOLLOWUP = re.compile(
    r"^(?:(?:都|具体|分别)?是?)?(?:哪|那)几家(?:机构|银行|农商行)?[?？]?$|"
    r"^(?:具体|分别)?(?:是)?哪些机构[?？]?$"
)
CONTINUATION_MARKER = re.compile(r"呢[?？]?$|同期|又怎么变|继续.*(?:上升|下降|回落)|排名变化")
ALL_ORGANIZATION_SCOPE = re.compile(
    r"全省|江苏省(?:全行|全市(?:农商行|银行|机构)?|13\s*个市(?:的)?(?:农商行|银行|机构)?)|"
    r"13\s*家(?:农商行|银行|机构)?"
)
POPULATION_INTENT = re.compile(
    r"13家|哪家|哪些|多少家|有几家|前\d|后\d|前三|后三|最高|最低|最好|最差"
)
EXPLICIT_OPERATION = re.compile(
    r"多少|第几|排名|变化|变动|增长|增幅|差|相比|比|均值|平均|达标|满足|"
    r"最高|最低|趋势|日均|合计|画像|评价|表现|怎么变|上升|下降|回落|改善|恶化|"
    r"等于|相加|除以|占比|比例|哪几家|哪些机构"
)
PROFILE_INTENT = re.compile(r"综合|整体(?:画像|风控|风险)|画像|评价|一句话总结|最优.*之一")


@dataclass(frozen=True, slots=True)
class ContextDecision:
    dependency: ContextDependency
    allow_context_rule: bool
    history_limit: int
    reason: str

    @property
    def needs_history(self) -> bool:
        return self.dependency != "self_contained"


class ContextRouter:
    """Separate history dependency from deterministic query coverage."""

    def __init__(self, catalog: SemanticCatalog) -> None:
        self.catalog = catalog

    def classify(self, raw_question: str, state: SessionState) -> ContextDecision:
        question = re.sub(r"\s+", "", raw_question.strip())
        long_distance = bool(LONG_DISTANCE_REFERENCE.search(question))
        history_limit = 20 if long_distance else 10
        has_history = bool(
            state.recent_turns
            or state.last_organizations
            or state.last_metrics
            or state.last_date
        )

        explicit_orgs = self.catalog.resolve_organizations(question)
        explicit_metrics = self._explicit_metrics(question)
        explicit_date = resolve_date(question)
        has_period = bool(
            re.search(
                r"20\d{2}年全年|20\d{2}年(?:第)?[一二三四1234]季度|"
                r"这个月|本月|当月|上个月|本季度|这个季度|本年|今年",
                question,
            )
        )
        population_intent = bool(
            POPULATION_INTENT.search(question)
            or (
                ALL_ORGANIZATION_SCOPE.search(question)
                and not re.search(r"全省(?:均值|平均)", question)
            )
        )
        dialogue_reference = bool(EXPLICIT_DIALOGUE_REFERENCE.search(question))
        list_followup = bool(ORGANIZATION_LIST_FOLLOWUP.fullmatch(question))

        # “同期” and change-from-focus expressions carry history only when the
        # current request does not itself provide the needed comparison anchor.
        comparison_needs_focus = False
        if explicit_date:
            resolved_comparison, _ = comparison_date(question, explicit_date)
            comparison_needs_focus = bool(
                re.search(r"又怎么变|继续.*(?:上升|下降|回落)|排名变化", question)
                and not resolved_comparison
            )
        elif "同期" in question:
            comparison_needs_focus = True

        missing_anchor_with_continuation = bool(
            has_history
            and CONTINUATION_MARKER.search(question)
            and (
                not explicit_metrics
                or (not explicit_orgs and not population_intent)
                or (not explicit_date and not has_period)
            )
        )
        implicit_ellipsis = bool(
            has_history
            and EXPLICIT_OPERATION.search(question)
            and (
                not explicit_metrics
                or
                (not explicit_orgs and not population_intent)
                or (not explicit_date and not has_period)
            )
        )
        # A recognizable data operation that omits selected-scope anchors is a
        # contextual continuation even when it contains no conventional pronoun.
        # This covers questions such as “两项相加是否等于总额”.
        missing_required_anchor = bool(
            has_history
            and explicit_metrics
            and (
                (not explicit_orgs and not population_intent)
                or (not explicit_date and not has_period)
            )
        )
        needs_context = bool(
            dialogue_reference
            or list_followup
            or comparison_needs_focus
            or missing_anchor_with_continuation
            or implicit_ellipsis
            or missing_required_anchor
        )

        # A discourse word that contributes no required slot must not force
        # history.  “继续查询2026-...B市不良率” is already self-contained.
        anchors_complete = bool(
            explicit_metrics
            and (explicit_orgs or population_intent)
            and (explicit_date or has_period)
        )
        redundant_history_phrase = bool(
            dialogue_reference
            and anchors_complete
            and not comparison_needs_focus
            and not PROFILE_INTENT.search(question)
        )
        nonsemantic_continue = bool(
            re.match(r"^继续(?:查询|看|分析)", question)
            and anchors_complete
            and not dialogue_reference
            and not comparison_needs_focus
        )
        if nonsemantic_continue or redundant_history_phrase or not needs_context:
            return ContextDecision(
                dependency="self_contained",
                allow_context_rule=False,
                history_limit=0,
                reason="当前问题的业务对象、时间和操作不依赖历史对话",
            )

        if not has_history:
            return ContextDecision(
                dependency="uncertain",
                allow_context_rule=False,
                history_limit=history_limit,
                reason="问题包含历史引用，但当前线程没有可用历史",
            )

        allow_rule = self._can_inherit_uniquely(
            question=question,
            state=state,
            explicit_orgs=explicit_orgs,
            explicit_metrics=explicit_metrics,
            explicit_date=explicit_date,
            has_period=has_period,
            population_intent=population_intent,
            long_distance=long_distance,
            list_followup=list_followup,
        )
        return ContextDecision(
            dependency="context_required" if allow_rule else "uncertain",
            allow_context_rule=allow_rule,
            history_limit=history_limit,
            reason=(
                "缺失槽位可从唯一的当前对话焦点安全继承"
                if allow_rule
                else "问题依赖历史，但规则无法证明所有引用对象唯一"
            ),
        )

    def _explicit_metrics(self, question: str) -> tuple[str, ...]:
        metrics = self.catalog.resolve_metrics(question)
        if metrics:
            return metrics
        formula = self.catalog.resolve_derived_metric(question)
        composition = self.catalog.resolve_composition(question)
        group = self.catalog.resolve_sum_metric_group(question)
        if formula or composition or group:
            # Only presence matters here; RulePlanner expands the actual IDs.
            return (formula or composition or "metric_group",)
        if re.search(r"盈利能力|主要经营指标|规模.*资产质量.*盈利能力|表现较好|风险画像|风控画像", question):
            return ("profile",)
        return ()

    @staticmethod
    def _can_inherit_uniquely(
        *,
        question: str,
        state: SessionState,
        explicit_orgs: tuple[str, ...],
        explicit_metrics: tuple[str, ...],
        explicit_date: str | None,
        has_period: bool,
        population_intent: bool,
        long_distance: bool,
        list_followup: bool,
    ) -> bool:
        # Long-distance references must use the wider history window; the last
        # focus state alone cannot prove what “一开始” referred to.
        if long_distance:
            return False

        if list_followup:
            return bool(
                state.recent_turns
                and state.recent_turns[-1].plan.operation in {
                    "count_condition",
                    "condition_members",
                    "multi_metric_province_compare",
                }
            )

        if not explicit_metrics and len(state.last_metrics) != 1:
            return False

        if not explicit_orgs and not population_intent:
            if PLURAL_REFERENCE.search(question):
                inherited_orgs = state.last_result_organizations or state.last_organizations
                if not inherited_orgs:
                    return False
                if "两家" in question and len(inherited_orgs) != 2:
                    return False
            else:
                inherited_orgs = state.last_organizations or state.last_result_organizations
                if len(inherited_orgs) != 1:
                    return False

        if not explicit_date and not has_period and not state.last_date:
            return False

        if re.search(r"这个增量|同期变化", question) and not state.last_comparison_date:
            return False

        # A bare “B市呢” is safe only when the previous operation was a simple
        # value lookup.  Other operations must be stated again or resolved by LLM.
        if not EXPLICIT_OPERATION.search(question) and state.last_operation not in {None, "value"}:
            return False

        return True
