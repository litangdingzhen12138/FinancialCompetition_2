"""FastAPI transport for the Text2SQL service."""

from __future__ import annotations

from functools import lru_cache

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .errors import Text2SQLError
from .service import Text2SQLService


app = FastAPI(title="Bank Text2SQL", version="0.1.0")


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=128)


@lru_cache(maxsize=1)
def get_service() -> Text2SQLService:
    return Text2SQLService()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/query")
def query(request: QueryRequest) -> dict[str, object]:
    try:
        return get_service().ask(request.question, request.session_id).to_dict()
    except (Text2SQLError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/sessions/{session_id}")
def clear_session(session_id: str) -> dict[str, bool]:
    get_service().clear_session(session_id)
    return {"cleared": True}

