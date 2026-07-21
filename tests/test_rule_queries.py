from __future__ import annotations

import pytest


def test_point_query(service):
    response = service.ask(
        "江苏省A市农商行在2025年6月15日，各项存款余额是多少？",
        "point",
    )
    assert response.route == "rule"
    assert response.answer == "江苏省A市农商行：42.02亿元"
    assert "ZB001" in response.sql
    assert "ORG001" in response.sql


def test_lower_is_better_ranking(service):
    response = service.ask("2026年3月末，哪家农商行的不良贷款率最低？", "rank")
    assert response.route == "rule"
    assert "江苏省J市农商行" in response.answer
    assert "0.73%" in response.answer
    assert "ORDER BY v.metric_value ASC" in response.plan["sql"]


def test_period_difference(service):
    response = service.ask(
        "江苏省A市农商行的各项存款余额截至2025-03-31，和2024年末相比变化了多少？",
        "difference",
    )
    assert response.route == "rule"
    assert "增加0.2亿元" in response.answer


def test_derived_ratio(service):
    response = service.ask("江苏省A市农商行在2025-01-31的存贷比是多少？", "ratio")
    assert response.route == "rule"
    assert response.answer == "江苏省A市农商行：80.68%"


def test_province_average(service):
    response = service.ask(
        "江苏省A市农商行的拨备覆盖率在2026-03-31和全省均值比，是高还是低？差多少？",
        "average",
    )
    assert "高于全省均值23.95个百分点" in response.answer


def test_rank_of_selected_org_is_among_all_orgs(service):
    response = service.ask(
        "江苏省H市农商行的不良贷款率在2026-04-30，全省13家里排第几？",
        "selected-rank",
    )
    assert "第11名" in response.answer


def test_multi_turn_pronoun_inherits_result_orgs_and_date(service):
    first = service.ask("截至2026-03-31，各项存款余额排名前三的是哪几家？", "dialog")
    second = service.ask("它们的不良率呢？", "dialog")
    assert "江苏省C市农商行" in first.answer
    assert second.plan["current_date"] == "2026-03-31"
    assert set(second.plan["organizations"]) == {"ORG003", "ORG006", "ORG007"}
    assert "0.88%" in second.answer


def test_query_database_does_not_import_gold_answers(service):
    import duckdb

    connection = duckdb.connect(str(service.db_path), read_only=True)
    names = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
    connection.close()
    assert "问题答案清单" not in names
    assert "qa_examples" not in names
    assert service.executor.execute("SELECT COUNT(*) AS cnt FROM metric_values").rows[0][0] == 132678

