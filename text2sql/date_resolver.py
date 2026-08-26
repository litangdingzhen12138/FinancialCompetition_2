"""Deterministic Chinese date and comparison-period normalization."""

from __future__ import annotations

from calendar import monthrange
from datetime import date
import re

from .business_rules import YEAR_BEGINNING_BASELINE


def _iso(year: int, month: int, day: int) -> str:
    return date(year, month, day).isoformat()


_QUARTERS = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}


def _explicit_dates(question: str) -> list[tuple[int, int, str]]:
    """Extract arbitrary explicit dates without assuming a benchmark year."""
    found: list[tuple[int, int, str]] = []
    patterns = (
        (r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日|号)?", "day"),
        (r"(?<!\d)(2\d)[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日|号)?", "short_day"),
        (r"今年(\d{1,2})月(\d{1,2})(?:日|号)?", "current_day"),
        (r"(20\d{2})年(\d{1,2})月(?:底|末|月底|月末)", "month"),
        (r"(20\d{2})年(?:第)?([一二三四1234])季度(?:末)?", "quarter"),
        (r"(20\d{2})[- ]?Q([1-4])(?:末)?", "quarter"),
        (r"(20\d{2})年(?:底|末|年底|年末)", "year"),
    )
    occupied: list[tuple[int, int]] = []
    for pattern, kind in patterns:
        for match in re.finditer(pattern, question, re.IGNORECASE):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            if kind == "current_day":
                year = date.today().year
                value = _iso(year, int(match.group(1)), int(match.group(2)))
            elif kind == "short_day":
                year = 2000 + int(match.group(1))
                value = _iso(year, int(match.group(2)), int(match.group(3)))
            elif kind == "day":
                year = int(match.group(1))
                value = _iso(year, int(match.group(2)), int(match.group(3)))
            elif kind == "month":
                year = int(match.group(1))
                month = int(match.group(2))
                value = _iso(year, month, monthrange(year, month)[1])
            elif kind == "quarter":
                year = int(match.group(1))
                month = _QUARTERS[match.group(2)] * 3
                value = _iso(year, month, monthrange(year, month)[1])
            else:
                year = int(match.group(1))
                value = f"{year}-12-31"
            occupied.append((match.start(), match.end()))
            found.append((match.start(), match.end(), value))
    return sorted(found)


def _is_explicit_comparison(question: str, start: int, end: int) -> bool:
    prefix = question[max(0, start - 3):start]
    suffix = question[end:min(len(question), end + 3)]
    return bool(
        re.search(r"(?:相比|比|较)$", prefix)
        or re.match(r"(?:相比|作比较)", suffix)
        or (re.search(r"(?:和|与)$", prefix) and re.match(r"比", suffix))
    )


def year_beginning(current: str) -> str:
    """Return the fixed workbook baseline for the ``较年初`` dimension."""
    date.fromisoformat(current)  # Keep invalid current dates from being silently accepted.
    return YEAR_BEGINNING_BASELINE


def resolve_date(question: str, reference_date: str | None = None) -> str | None:
    match = re.search(r"(20\d{2})年.*上半年末.*(?:年末|年底)", question)
    if match:
        return f"{match.group(1)}-12-31"
    explicit_dates = _explicit_dates(question)
    if explicit_dates:
        current_candidates = [
            item for item in explicit_dates if not _is_explicit_comparison(question, item[0], item[1])
        ]
        return (current_candidates or explicit_dates)[-1][2]
    if reference_date:
        reference_year = date.fromisoformat(reference_date).year
        for match in re.finditer(r"(?:第)?([一二三四1234])季度末", question):
            prefix = question[max(0, match.start() - 2):match.start()]
            if re.search(r"(?:相比|比|较)$", prefix):
                continue
            quarter = _QUARTERS[match.group(1)]
            month = quarter * 3
            return _iso(reference_year, month, monthrange(reference_year, month)[1])
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


def comparison_date(
    question: str,
    current: str,
    reference_date: str | None = None,
) -> tuple[str | None, str | None]:
    quarter = re.search(
        r"(?:相比|比|较)(?:(20\d{2})年)?(?:第)?([一二三四1234])季度(?:末)?",
        question,
    )
    if quarter:
        year = int(quarter.group(1)) if quarter.group(1) else date.fromisoformat(current).year
        number = _QUARTERS[quarter.group(2)]
        month = number * 3
        return _iso(year, month, monthrange(year, month)[1]), "explicit_quarter"
    if re.search(r"上半年末|半年末", question):
        return f"{date.fromisoformat(current).year}-06-30", "half_year_end"
    explicit_dates = _explicit_dates(question)
    marked_comparisons = [
        value
        for start, end, value in explicit_dates
        if value != current and _is_explicit_comparison(question, start, end)
    ]
    if marked_comparisons:
        return marked_comparisons[-1], "explicit_comparison"
    if len(explicit_dates) >= 2 and re.search(r"和|与|从|到|至|->|→|分别", question):
        other_dates = [value for _, _, value in explicit_dates if value != current]
        if other_dates:
            return other_dates[0], "explicit_range"
    if re.search(r"较?年初|从年初", question):
        return year_beginning(current), "year_beginning"
    if re.search(r"较?上月|环比|比上个月|与上个月", question):
        return previous_month_end(current), "previous_month"
    if re.search(r"较?上季|上季度|与上季度", question):
        return previous_quarter_end(current), "previous_quarter"
    if re.search(r"同比|较同期|同期比|较去年同期|去年同期", question):
        return same_period_last_year(current), "same_period_last_year"
    if reference_date and re.search(r"又怎么变|继续.*(?:上升|下降|回落)|改善.*恶化|恶化.*改善|排名变化", question):
        return reference_date, "previous_focus_period"
    return None, None
