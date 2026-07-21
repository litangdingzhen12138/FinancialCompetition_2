"""Typed failures used to decide whether a request may fall back to the LLM."""

from __future__ import annotations


class Text2SQLError(Exception):
    """Base class for expected request failures."""


class ConfigurationError(Text2SQLError):
    pass


class DataBuildError(Text2SQLError):
    pass


class PlanningError(Text2SQLError):
    pass


class ValidationError(Text2SQLError):
    pass


class SQLSafetyError(ValidationError):
    pass


class QueryExecutionError(Text2SQLError):
    def __init__(self, public_message: str, retry_feedback: str | None = None) -> None:
        super().__init__(public_message)
        self.retry_feedback = retry_feedback


class ResultValidationError(Text2SQLError):
    pass


def retry_feedback(exc: Exception) -> str:
    if isinstance(exc, QueryExecutionError) and exc.retry_feedback:
        return exc.retry_feedback
    return str(exc)[:500]

