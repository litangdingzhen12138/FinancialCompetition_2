"""Deterministic Chinese date and comparison-period normalization."""

from __future__ import annotations

from calendar import monthrange
from datetime import date
import re


def _iso(year: int, month: int, day: int) -> str:
    return date(year, month, day).isoformat()


def resolve_date(question: str) -> str | None:
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日|号)?", question)
    if match:
        return _iso(*map(int, match.groups()))
    match = re.search(r"(20\d{2})年(\d{1,2})月(?:底|末|月底|月末)", question)
    if match:
        year, month = map(int, match.groups())
        return _iso(year, month, monthrange(year, month)[1])
    match = re.search(r"(20\d{2})年(?:第)?([一二三四1234])季度末", question)
    if match:
        year = int(match.group(1))
        quarter = {"一": 1, "二": 2, "三": 3, "四": 4}.get(match.group(2), int(match.group(2)) if match.group(2).isdigit() else 1)
        month = quarter * 3
        return _iso(year, month, monthrange(year, month)[1])
    match = re.search(r"(20\d{2})[- ]?Q([1-4])", question, re.IGNORECASE)
    if match:
        year, quarter = map(int, match.groups())
        month = quarter * 3
        return _iso(year, month, monthrange(year, month)[1])
    match = re.search(r"(20\d{2})年(?:底|末|年底|年末)", question)
    if match:
        return f"{match.group(1)}-12-31"
    return None


def previous_month_end(current: str) -> str:
    current_date = date.fromisoformat(current)
    year, month = current_date.year, current_date.month - 1
    if month == 0:
        year, month = year - 1, 12
    return _iso(year, month, monthrange(year, month)[1])


def previous_quarter_end(current: str) -> str:
    current_date = date.fromisoformat(current)
    current_quarter = (current_date.month - 1) // 3 + 1
    if current_quarter == 1:
        return f"{current_date.year - 1}-12-31"
    month = (current_quarter - 1) * 3
    return _iso(current_date.year, month, monthrange(current_date.year, month)[1])


def same_period_last_year(current: str) -> str:
    current_date = date.fromisoformat(current)
    day = min(current_date.day, monthrange(current_date.year - 1, current_date.month)[1])
    return _iso(current_date.year - 1, current_date.month, day)


def comparison_date(question: str, current: str) -> tuple[str | None, str | None]:
    if re.search(r"较?年初|从年初|2024年末", question):
        return "2024-12-31", "year_beginning"
    if re.search(r"较?上月|环比|比上个月|与上个月", question):
        return previous_month_end(current), "previous_month"
    if re.search(r"较?上季|上季度|与上季度", question):
        return previous_quarter_end(current), "previous_quarter"
    if re.search(r"同比|较去年同期|去年同期", question):
        return same_period_last_year(current), "same_period_last_year"
    return None, None
