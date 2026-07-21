from __future__ import annotations

import json

import pytest

from text2sql.errors import PlanningError
from text2sql.llm_planner import parse_llm_plan


def _plan_json(assumptions: object) -> str:
    return json.dumps(
        {
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
    )


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
