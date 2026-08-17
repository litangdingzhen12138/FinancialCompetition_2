"""High-precision state machine for incrementally completing missing query slots."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal
import re

from .date_resolver import resolve_date
from .models import PendingQuery, SessionState
from .rule_planner import RuleDecision, RulePlanner
from .semantic_catalog import METRIC_ALIASES, SemanticCatalog


PendingAction = Literal["new_question", "clarify", "ready", "cancelled"]

_QUERY_INTENT = re.compile(
    r"多少|查询|查一下|看一下|第几|排名|变化|变动|增长|增幅|相比|比|差|"
    r"均值|平均|最高|最低|哪些|哪家|是否|达标|满足|趋势|画像|评价|表现"
)
_SLOT_REPLY = re.compile(
    r"^(?:补充(?:一下|信息)?[：:]?)?(?:查询)?(?:机构|银行|农商行|指标|日期|时间|"
    r"当前日期|比较日期|比较期)(?:是|为|改成|换成|用)[：:]?"
)
_CORRECTION = re.compile(r"我说的是|不是.+是|改成|换成|更正")
_CANCEL = re.compile(r"^(?:算了|取消|不用了|忽略(?:这个|上个)?问题)[。.!！]?$")

_SLOT_LABELS = {
    "organization": "查询机构",
    "metric": "查询指标",
    "date": "查询日期",
}


@dataclass(frozen=True, slots=True)
class PendingResolution:
    action: PendingAction
    pending: PendingQuery | None = None
    planning_state: SessionState | None = None
    resolved_question: str | None = None
    prompt: str | None = None


def missing_required_slots(candidates: dict[str, object]) -> tuple[str, ...]:
    """Return only universal query slots, not operation-specific optional fields."""
    missing: list[str] = []
    scope = candidates.get("organization_scope", "selected")
    organizations = candidates.get("organizations")
    metrics = candidates.get("metrics")
    current_date = candidates.get("current_date")
    start_date = candidates.get("start_date")
    end_date = candidates.get("end_date")
    if scope == "selected" and not organizations:
        missing.append("organization")
    if not metrics:
        missing.append("metric")
    if not current_date and not (start_date and end_date):
        missing.append("date")
    return tuple(missing)


def should_start_pending(question: str, decision: RuleDecision) -> bool:
    """Only capture recognizable data queries; leave open requests to the LLM."""
    missing = missing_required_slots(decision.candidates)
    if not missing or not _QUERY_INTENT.search(question) or _SLOT_REPLY.search(question):
        return False
    candidates = decision.candidates
    has_anchor = bool(
        candidates.get("organizations")
        or candidates.get("metrics")
        or candidates.get("current_date")
        or candidates.get("start_date")
        or candidates.get("end_date")
    )
    return has_anchor


def is_slot_only_reply(question: str, catalog: SemanticCatalog) -> bool:
    """Identify a bare slot value that cannot form a standalone data query."""
    if _QUERY_INTENT.search(question):
        return False
    return bool(
        catalog.resolve_organizations(question)
        or catalog.resolve_metrics(question)
        or resolve_date(question)
        or _SLOT_REPLY.search(re.sub(r"\s+", "", question.strip()))
    )


class PendingQueryResolver:
    """Merge explicit slot replies without consulting or mutating successful history."""

    def __init__(self, catalog: SemanticCatalog, rule_planner: RulePlanner) -> None:
        self.catalog = catalog
        self.rule_planner = rule_planner

    def start(self, question: str, decision: RuleDecision) -> PendingQuery:
        candidates = decision.candidates
        return PendingQuery(
            original_question=question,
            working_question=question,
            organizations=self._tuple(candidates.get("organizations")),
            metrics=self._tuple(candidates.get("metrics")),
            current_date=self._string(candidates.get("current_date")),
            comparison_date=self._string(candidates.get("comparison_date")),
            start_date=self._string(candidates.get("start_date")),
            end_date=self._string(candidates.get("end_date")),
            organization_scope=(
                "all" if candidates.get("organization_scope") == "all" else "selected"
            ),
            missing_slots=missing_required_slots(candidates),
        )

    def resolve(self, question: str, pending: PendingQuery) -> PendingResolution:
        normalized = re.sub(r"\s+", "", question.strip())
        if _CANCEL.fullmatch(normalized):
            return PendingResolution(
                action="cancelled",
                prompt="已取消上一条未完成的查询。",
            )

        standalone = self.rule_planner.plan(question, SessionState(), use_context=False)
        is_slot_reply = bool(_SLOT_REPLY.search(normalized) or _CORRECTION.search(normalized))
        if standalone.plan is not None and not is_slot_reply:
            return PendingResolution(action="new_question")

        explicit_orgs = self.catalog.resolve_organizations(question)
        explicit_metrics = self.catalog.resolve_metrics(question)
        explicit_date = resolve_date(question)
        supplied_slots = {
            slot
            for slot, supplied in (
                ("organization", bool(explicit_orgs)),
                ("metric", bool(explicit_metrics)),
                ("date", bool(explicit_date)),
            )
            if supplied
        }

        # A question-like request with several anchors is more likely a new query
        # than an answer to the outstanding clarification.
        if (
            not is_slot_reply
            and _QUERY_INTENT.search(question)
            and len(supplied_slots) >= 2
            and not supplied_slots.intersection(pending.missing_slots)
        ):
            return PendingResolution(action="new_question")

        if not supplied_slots:
            updated = replace(pending, clarification_count=pending.clarification_count + 1)
            if updated.clarification_count >= 3:
                return PendingResolution(
                    action="cancelled",
                    prompt="仍无法识别补充信息，已取消待补查询；请在一条消息中明确机构、指标和日期后重新提问。",
                )
            return PendingResolution(
                action="clarify",
                pending=updated,
                prompt=self.clarification_prompt(updated.missing_slots),
            )

        working_question = pending.working_question
        if explicit_orgs and pending.organizations and explicit_orgs != pending.organizations:
            working_question = self._replace_organizations(
                working_question,
                pending.organizations,
                explicit_orgs,
            )
        if explicit_metrics and pending.metrics and explicit_metrics != pending.metrics:
            working_question = self._replace_metrics(
                working_question,
                pending.metrics,
                explicit_metrics,
            )

        merged = replace(
            pending,
            working_question=working_question,
            organizations=explicit_orgs or pending.organizations,
            metrics=explicit_metrics or pending.metrics,
            current_date=explicit_date or pending.current_date,
            supplements=(*pending.supplements, question),
            clarification_count=0,
        )
        planning_state = self._planning_state(merged)
        decision = self.rule_planner.plan(
            merged.working_question,
            planning_state,
            use_context=True,
        )
        missing = missing_required_slots(decision.candidates)
        merged = replace(
            merged,
            organizations=(
                self._tuple(decision.candidates.get("organizations"))
                or merged.organizations
            ),
            metrics=self._tuple(decision.candidates.get("metrics")) or merged.metrics,
            current_date=(
                self._string(decision.candidates.get("current_date"))
                or merged.current_date
            ),
            comparison_date=(
                self._string(decision.candidates.get("comparison_date"))
                or merged.comparison_date
            ),
            start_date=self._string(decision.candidates.get("start_date")) or merged.start_date,
            end_date=self._string(decision.candidates.get("end_date")) or merged.end_date,
            missing_slots=missing,
        )
        if missing:
            return PendingResolution(
                action="clarify",
                pending=merged,
                prompt=self.clarification_prompt(missing),
            )
        return PendingResolution(
            action="ready",
            pending=merged,
            planning_state=self._planning_state(merged),
            resolved_question=self.resolved_question(merged),
        )

    def clarification_prompt(self, missing_slots: tuple[str, ...]) -> str:
        labels = "、".join(_SLOT_LABELS[slot] for slot in missing_slots)
        examples: list[str] = []
        if "organization" in missing_slots:
            examples.append("机构是江苏省J市农商行")
        if "metric" in missing_slots:
            examples.append("指标是不良贷款率")
        if "date" in missing_slots:
            examples.append("日期是2026-04-30")
        return f"当前查询还缺少{labels}。请补充，例如：{'；'.join(examples)}。"

    def resolved_question(self, pending: PendingQuery) -> str:
        details: list[str] = []
        if pending.organization_scope == "selected" and pending.organizations:
            names = "、".join(self.catalog.organizations[item] for item in pending.organizations)
            details.append(f"查询机构为{names}")
        if pending.metrics:
            names = "、".join(self.catalog.metrics[item].name for item in pending.metrics)
            details.append(f"查询指标为{names}")
        if pending.current_date:
            details.append(f"查询日期为{pending.current_date}")
        suffix = "；".join(details)
        return f"{pending.working_question.rstrip('？?。')}（补充信息：{suffix}）"

    def _replace_organizations(
        self,
        question: str,
        old_ids: tuple[str, ...],
        new_ids: tuple[str, ...],
    ) -> str:
        aliases: set[str] = set()
        for org_id in old_ids:
            name = self.catalog.organizations[org_id]
            aliases.update(
                {
                    org_id,
                    name,
                    name.removeprefix("江苏省"),
                    name.replace("江苏省", "").replace("农商行", "行"),
                    name.replace("江苏省", "").replace("农商行", ""),
                }
            )
        replacement = "、".join(self.catalog.organizations[item] for item in new_ids)
        return self._replace_aliases(question, aliases, replacement)

    def _replace_metrics(
        self,
        question: str,
        old_ids: tuple[str, ...],
        new_ids: tuple[str, ...],
    ) -> str:
        aliases = {
            alias
            for metric_id in old_ids
            for alias in (*METRIC_ALIASES.get(metric_id, ()), metric_id)
        }
        replacement = "、".join(self.catalog.metrics[item].name for item in new_ids)
        return self._replace_aliases(question, aliases, replacement)

    @staticmethod
    def _replace_aliases(question: str, aliases: set[str], replacement: str) -> str:
        usable = sorted((alias for alias in aliases if alias), key=len, reverse=True)
        if not usable:
            return question
        pattern = re.compile("|".join(re.escape(alias) for alias in usable), re.IGNORECASE)
        return pattern.sub(replacement, question)

    @staticmethod
    def _planning_state(pending: PendingQuery) -> SessionState:
        return SessionState(
            last_organizations=pending.organizations,
            last_metrics=pending.metrics,
            last_date=pending.current_date,
            last_comparison_date=pending.comparison_date,
        )

    @staticmethod
    def _tuple(value: object) -> tuple[str, ...]:
        if isinstance(value, (tuple, list)):
            return tuple(item for item in value if isinstance(item, str))
        return ()

    @staticmethod
    def _string(value: object) -> str | None:
        return value if isinstance(value, str) else None
