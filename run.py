"""Compatibility entry point: import run; run.run(question, session_id)."""

from __future__ import annotations

from functools import lru_cache

from text2sql.service import Text2SQLService


@lru_cache(maxsize=1)
def _service() -> Text2SQLService:
    return Text2SQLService()


def run(question: str, session_id: str = "default") -> str:
    return _service().ask(question, session_id).answer


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--session-id", default="cli")
    args = parser.parse_args()
    print(run(args.question, args.session_id))

