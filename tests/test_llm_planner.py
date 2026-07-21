from __future__ import annotations

import json

import pytest

from text2sql.errors import PlanningError
from text2sql.llm_planner import parse_llm_plan


def _plan_json(assumptions: object, **updates: object) -> str:
    value = {
            "query_type": "point_query",
            "operation": "value",
            "organizations": ["ORG001"],
            "organization_scope": "selected",
            "metrics": ["ZB001"],
            "current_date": "2025-06-15",
            "dimensions": ["organization"],
            "filters": [],
            "sort_direction": None,
            "limit": None,
            "expected_shape": "single_row",
            "allow_empty": False,
            "derived_formula": None,
            "confidence": 0.9,
            "assumptions": assumptions,
            "sql": "SELECT 1",
    }
    value.update(updates)
    return json.dumps(value)


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("inherit organizations from the previous turn", ("inherit organizations from the previous turn",)),
        (["first", "second"], ("first", "second")),
        (None, ()),
        ("   ", ()),
    ],
)
def test_parse_llm_plan_normalizes_assumptions(raw_value, expected):
    assert parse_llm_plan(_plan_json(raw_value)).assumptions == expected


def test_parse_llm_plan_rejects_invalid_assumptions_object():
    with pytest.raises(PlanningError):
        parse_llm_plan(_plan_json({"unexpected": "object"}))


def test_parse_llm_plan_accepts_null_confidence_and_normalizes_operation_alias():
    plan = parse_llm_plan(_plan_json([], confidence=None, operation="query"))
    assert plan.confidence == 0.0
    assert plan.operation == "value"


def test_parse_llm_plan_defaults_non_executable_structure_fields():
    plan = parse_llm_plan(
        _plan_json([], query_type=None, organization_scope=None, expected_shape=None)
    )
    assert plan.query_type == "llm_query"
    assert plan.organization_scope == "selected"
    assert plan.expected_shape == "multi_row"


def test_parse_llm_plan_normalizes_list_and_dict_structure_fields():
    plan = parse_llm_plan(
        _plan_json(
            [],
            organization_scope=["ORG001"],
            expected_shape={"rows": 1, "columns": 2},
        )
    )
    assert plan.organization_scope == "selected"
    assert plan.expected_shape == "multi_row"


def test_parse_llm_plan_treats_custom_same_date_arithmetic_as_generic_calculation():
    plan = parse_llm_plan(
        _plan_json(
            [],
            operation="difference",
            metrics=["ZB017", "ZB013"],
            comparison_date=None,
            derived_formula="ZB017 - ZB013",
        )
    )
    assert plan.operation == "multi_condition"
    assert plan.derived_formula is None
