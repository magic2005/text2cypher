"""Offline tests for the isolated benchmark data, tools and evidence rules.

These tests must not import ``agent.core`` or require Neo4j / model
credentials.  The subprocess test at the top guards that boundary directly.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from evaluation import benchmark
from tools import compare_mock_teacher, evaluate, mock_teacher_eval

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# isolation and dataset integrity
# ---------------------------------------------------------------------------


def test_modules_import_without_neo4j_or_agent_core():
    code = (
        "import sys\n"
        "import tools.evaluate, tools.mock_teacher_eval, tools.compare_mock_teacher\n"
        "assert 'neo4j' not in sys.modules, 'neo4j was imported eagerly'\n"
        "assert 'agent.core' not in sys.modules, 'agent.core was imported eagerly'\n"
        "assert 'streamlit' not in sys.modules, 'streamlit was imported eagerly'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_question_set_is_complete_and_consistent():
    objects = evaluate.QUESTIONS
    assert len(objects) == 30
    assert [item["id"] for item in objects] == list(benchmark.EXPECTED_IDS)
    assert benchmark.validate_questions(objects) == []
    assert Counter(item["level"] for item in objects) == {
        "简单": 10,
        "中等": 12,
        "困难": 8,
    }
    assert sum(len(item["reference_rows"]) for item in objects) == 218
    fingerprints = {item["graph_fingerprint"] for item in objects}
    assert fingerprints == {
        "277d4b4c0a1dc9013efa44df289da0ca74cf2a2750d787a0855e2866066f6615"
    }
    assert benchmark.dataset_sha256() == evaluate.DATASET_SHA256
    assert len(evaluate.DATASET_SHA256) == 64


def test_reference_queries_are_read_only_and_bounded():
    for item in evaluate.QUESTIONS:
        cypher = item["reference_cypher"]
        queries = cypher if isinstance(cypher, list) else [cypher]
        for index, query in enumerate(queries, start=1):
            problems = benchmark.read_only_problems(query)
            assert problems == [], f"{item['id']} 第 {index} 条：{problems}"


def test_combined_questions_have_evidence_and_two_columns():
    combined = [
        item
        for item in evaluate.QUESTIONS
        if isinstance(item["reference_cypher"], list)
    ]
    assert {item["id"] for item in combined} == set(benchmark.COMBINED_IDS)
    for item in combined:
        evidence = item["reference_evidence_counts"]
        assert set(evidence) == set(benchmark.COMBINED_EVIDENCE_KEYS)
        assert evidence["mq_targets"]
        assert all(len(row) == 2 for row in item["reference_rows"])


def test_static_checker_ignores_keywords_inside_literals():
    query = (
        "MATCH (n:微服务) WHERE n.说明 = 'DROP; CALL // 注释' "
        "RETURN n.服务名称 AS 服务 LIMIT 10"
    )
    assert benchmark.read_only_problems(query) == []


@pytest.mark.parametrize(
    "query, expected",
    [
        ("MATCH (n) DETACH DELETE n LIMIT 1", "写操作"),
        ("MATCH (n) RETURN n", "LIMIT"),
        ("MATCH (p)-[:调用*]->(q) RETURN p LIMIT 1", "变长路径"),
        (
            "MATCH (n) RETURN n LIMIT 1; MATCH (m) RETURN m LIMIT 1",
            "一条语句",
        ),
        ("MATCH (n) RETURN n LIMIT 1 // trailing", "注释"),
        ("MATCH (n) WHERE n.x = 'unclosed RETURN n LIMIT 1", "未闭合"),
    ],
)
def test_static_checker_rejects_unsafe_queries(query, expected):
    problems = benchmark.read_only_problems(query)
    assert any(expected in problem for problem in problems), problems


def test_static_checker_rejects_unbounded_mock_gold():
    problems = benchmark.read_only_problems(
        "MATCH (p)-[:调用*]->(q) RETURN p", require_limit=False
    )
    assert any("变长路径" in problem for problem in problems)


# ---------------------------------------------------------------------------
# dataset validation catches drift
# ---------------------------------------------------------------------------


def test_validation_rejects_unsafe_reference():
    dataset = deepcopy(evaluate.QUESTIONS)
    dataset[0]["reference_cypher"] = "MATCH (n) DETACH DELETE n LIMIT 1"
    problems = benchmark.validate_questions(dataset)
    assert any("写操作" in problem for problem in problems)


def test_validation_rejects_duplicate_id():
    dataset = deepcopy(evaluate.QUESTIONS)
    dataset[1]["id"] = dataset[0]["id"]
    problems = benchmark.validate_questions(dataset)
    assert any("题号必须是" in problem for problem in problems)


def test_validation_rejects_wrong_row_count():
    dataset = deepcopy(evaluate.QUESTIONS)
    dataset[0]["reference_result_count"] = 0
    problems = benchmark.validate_questions(dataset)
    assert any("reference_result_count" in problem for problem in problems)


def test_validation_rejects_fingerprint_drift():
    dataset = deepcopy(evaluate.QUESTIONS)
    dataset[0]["graph_version_counts"]["new-version"] = 1
    problems = benchmark.validate_questions(dataset)
    assert any("graph_fingerprint" in problem for problem in problems)


def test_validation_rejects_missing_combined_evidence():
    dataset = deepcopy(evaluate.QUESTIONS)
    item = next(entry for entry in dataset if entry["id"] == "H01")
    item["reference_evidence_counts"] = None
    problems = benchmark.validate_questions(dataset)
    assert any("reference_evidence_counts" in problem for problem in problems)


# ---------------------------------------------------------------------------
# judging and report bookkeeping
# ---------------------------------------------------------------------------


def test_canonical_ignores_alias_order_and_duplicates():
    assert benchmark.canonical([{"a": 1, "b": 2}]) == benchmark.canonical(
        [{"x": 2, "y": 1}]
    )
    assert benchmark.canonical([{"a": 1}, {"a": 1}]) == benchmark.canonical(
        [{"a": 1}]
    )
    assert benchmark.duplicate_diagnostics([{"a": 1}, {"a": 1}, {"a": 2}]) == {
        "actual_row_count": 3,
        "actual_unique_row_count": 2,
        "duplicate_rows_collapsed": 1,
    }


def test_judge_three_states():
    reference = [{"服务": "ts-order-service"}]
    passed = benchmark.judge(
        reference,
        {
            "error": None,
            "answer_complete": True,
            "results": [{"目标服务": "ts-order-service"}],
        },
    )
    assert passed["correct"] is True
    assert passed["automatic_match"] is True

    review = benchmark.judge(
        reference,
        {"error": None, "answer_complete": True, "results": [{"服务": "other"}]},
    )
    assert review["correct"] is None
    assert "人工复核" in review["failure_reason"]

    incomplete = benchmark.judge(
        reference, {"error": None, "answer_complete": False, "results": []}
    )
    assert incomplete["correct"] is False
    assert "截断" in incomplete["failure_reason"]

    failed = benchmark.judge(
        reference,
        {"error": "boom", "answer_complete": False, "results": []},
    )
    assert failed["correct"] is False
    assert failed["failure_reason"] == "boom"
    assert failed["automatic_match"] is False


def test_build_record_does_not_copy_reference_rows():
    item = deepcopy(evaluate.QUESTIONS[0])
    actual = {
        "queries": ["MATCH (n) RETURN n LIMIT 1"],
        "results": deepcopy(item["reference_rows"]),
        "summary": "",
        "stages": [],
        "answer_complete": True,
        "error": None,
    }
    record = benchmark.build_record(item, actual)
    assert "reference_rows" not in record
    assert "reference_cypher" not in record
    assert record["attempted"] is True
    assert record["actual_row_count"] == len(item["reference_rows"])


def test_evaluate_one_passes_only_the_question_to_the_model():
    item = deepcopy(evaluate.QUESTIONS[0])
    captured = {}

    def fake_ask(question, graph, cfg, level=None):
        captured.update(id=id(graph), question=question, level=level)
        return {
            "queries": [],
            "results": deepcopy(item["reference_rows"]),
            "summary": "",
            "stages": [],
            "answer_complete": True,
            "error": None,
        }

    marker = object()
    record = evaluate.evaluate_one(
        item, marker, {"provider": "test"}, ask_fn=fake_ask
    )
    assert captured["question"] == item["question"]
    assert captured["level"] == item["level"]
    assert captured["id"] == id(marker)
    assert record["correct"] is True


def small_questions():
    fingerprint = "f" * 64
    return [
        {
            "id": "S01",
            "level": "简单",
            "question": "问题一",
            "graph_fingerprint": fingerprint,
        },
        {
            "id": "S02",
            "level": "简单",
            "question": "问题二",
            "graph_fingerprint": fingerprint,
        },
    ]


def small_record(question, correct, item_id):
    return {
        "id": item_id,
        "level": "简单",
        "question": question["question"],
        "attempted": True,
        "correct": correct,
        "automatic_match": correct is True,
    }


def test_build_report_scores_only_complete_reviewed_runs():
    questions = small_questions()
    records = {
        "S01": small_record(questions[0], True, "S01"),
        "S02": small_record(questions[1], True, "S02"),
    }
    cfg = {"provider": "test", "model": "stub"}
    report = evaluate.build_report(
        records, cfg, graph_before="f" * 64, graph_after="f" * 64, questions=questions
    )
    assert report["score"] == 2
    assert report["pending"] == 0
    assert report["needs_review"] == 0

    records["S02"]["correct"] = None
    report = evaluate.build_report(
        records, cfg, graph_before="f" * 64, graph_after="f" * 64, questions=questions
    )
    assert report["score"] is None
    assert report["needs_review"] == 1

    records["S02"] = small_record(questions[1], True, "S02")
    del records["S01"]
    report = evaluate.build_report(
        records, cfg, graph_before="f" * 64, graph_after="f" * 64, questions=questions
    )
    assert report["score"] is None
    assert report["pending"] == 1

    report = evaluate.build_report(
        records, cfg, graph_before="0" * 64, graph_after="f" * 64, questions=questions
    )
    assert report["graph_drift"] is True
    assert report["score"] is None


def test_save_report_writes_atomically(tmp_path):
    questions = small_questions()
    records = {"S01": small_record(questions[0], True, "S01")}
    destination = tmp_path / "report.json"
    report = evaluate.save_report(
        records,
        {"provider": "test", "model": "stub"},
        metadata={"status": "finished"},
        questions=questions,
        destination=destination,
    )
    stored = json.loads(destination.read_text(encoding="utf-8"))
    assert stored["items"][0]["id"] == "S01"
    assert stored["run"]["status"] == "finished"
    assert stored["score"] is None
    assert not (tmp_path / "report.json.tmp").exists()
    assert report["dataset_sha256"] == evaluate.DATASET_SHA256


def test_resume_rejects_a_changed_dataset(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps({"dataset_sha256": "0" * 64, "items": []}), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="题目集已变化"):
        evaluate.load_resume_records(path)


def test_resume_keeps_only_attempted_known_items(tmp_path):
    path = tmp_path / "report.json"
    payload = {
        "dataset_sha256": evaluate.DATASET_SHA256,
        "items": [
            {"id": "S01", "attempted": True},
            {"id": "S02", "attempted": False},
            {"id": "UNKNOWN", "attempted": True},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert evaluate.load_resume_records(path) == {"S01": {"id": "S01", "attempted": True}}


@pytest.mark.parametrize(
    "name", ["questions.json", "../questions.json", "sub/results.json", "results.txt"]
)
def test_output_path_rejects_escapes(name):
    with pytest.raises(ValueError):
        benchmark.safe_output_path(benchmark.MOCK_TEACHER_DIR, name)


def test_output_path_accepts_plain_json():
    path = benchmark.safe_output_path(
        benchmark.MOCK_TEACHER_DIR, "results_2026-11-13.json"
    )
    assert path.name == "results_2026-11-13.json"
    assert path.parent == benchmark.MOCK_TEACHER_DIR


# ---------------------------------------------------------------------------
# mock teacher set and comparison tool
# ---------------------------------------------------------------------------


def test_mock_cases_match_committed_questions():
    assert len(mock_teacher_eval.CASES) == 30
    assert mock_teacher_eval.validate()["problems"] == []
    committed = json.loads(
        benchmark.MOCK_QUESTIONS_PATH.read_text(encoding="utf-8")
    )
    assert committed == mock_teacher_eval.mock_questions_payload(
        mock_teacher_eval.CASES
    )


def test_mock_validation_detects_committed_question_change():
    committed = deepcopy(
        json.loads(benchmark.MOCK_QUESTIONS_PATH.read_text(encoding="utf-8"))
    )
    committed[0]["question"] = "被偷改的问题"
    problems = benchmark.validate_mock_cases(
        mock_teacher_eval.CASES, committed=committed
    )
    assert any("不一致" in problem for problem in problems)


def test_ensure_questions_file_does_not_rewrite_same_content(tmp_path, monkeypatch):
    path = tmp_path / "questions.json"
    monkeypatch.setattr(mock_teacher_eval, "MOCK_QUESTIONS_PATH", path)
    payload = mock_teacher_eval.mock_questions_payload(mock_teacher_eval.CASES)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()
    assert mock_teacher_eval.ensure_questions_file() is False
    assert path.read_bytes() == before


def test_ensure_questions_file_refuses_to_clobber_changed_content(
    tmp_path, monkeypatch
):
    path = tmp_path / "questions.json"
    monkeypatch.setattr(mock_teacher_eval, "MOCK_QUESTIONS_PATH", path)
    path.write_text(json.dumps([{"id": "changed"}], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit, match="不会静默覆盖"):
        mock_teacher_eval.ensure_questions_file()


def test_rows_equal_preserves_duplicate_multiplicity():
    assert benchmark.rows_equal([{"a": 1}, {"a": 1}], [{"a": 1}, {"a": 1}])
    assert not benchmark.rows_equal([{"a": 1}, {"a": 1}], [{"a": 1}])


def mock_item(item_id, question="问题", passed=True):
    return {
        "id": item_id,
        "question": question,
        "gold_cypher": "MATCH (n) RETURN n LIMIT 1",
        "gold_nonempty": True,
        "answer_complete": True,
        "error": None,
        "gold_rows": [{"n": 1}],
        "actual_rows": [{"n": 1}] if passed else [{"n": 2}],
    }


def mock_run(items, mode="local_oracle", before="fp", after="fp"):
    return {
        "evaluation_mode": mode,
        "graph_fingerprint_before": before,
        "graph_fingerprint_after": after,
        "items": items,
    }


def test_inspect_checks_each_failure_mode():
    assert compare_mock_teacher.inspect(mock_item("S01")) == (True, None)

    empty = mock_item("S01")
    empty["gold_nonempty"] = False
    assert compare_mock_teacher.inspect(empty)[0] is False

    errored = mock_item("S01")
    errored["error"] = "boom"
    assert compare_mock_teacher.inspect(errored) == (False, "boom")

    incomplete = mock_item("S01")
    incomplete["answer_complete"] = False
    assert compare_mock_teacher.inspect(incomplete)[0] is False

    duplicate_mismatch = mock_item("S01")
    duplicate_mismatch["actual_rows"] = [{"n": 1}, {"n": 1}]
    assert compare_mock_teacher.inspect(duplicate_mismatch)[0] is False


def test_build_comparison_counts_and_requires_stable_graph():
    before_items = [mock_item("S01", passed=False), mock_item("S02")]
    after_items = [mock_item("S01"), mock_item("S02")]
    comparison = compare_mock_teacher.build_comparison(
        mock_run(before_items), mock_run(after_items), expected_ids=("S01", "S02")
    )
    assert comparison["before_passed"] == 1
    assert comparison["after_passed"] == 2
    assert comparison["graph_stable"] is True
    assert comparison["same_graph_between_runs"] is True
    assert comparison["all_reference_rows_same"] is True

    comparison = compare_mock_teacher.build_comparison(
        mock_run(before_items),
        mock_run(after_items, before="fp1", after="fp2"),
        expected_ids=("S01", "S02"),
    )
    assert comparison["graph_stable"] is False
    assert comparison["after_passed"] is None


def test_build_comparison_rejects_mode_or_question_changes():
    items = [mock_item("S01"), mock_item("S02")]
    with pytest.raises(RuntimeError, match="local_oracle"):
        compare_mock_teacher.build_comparison(
            mock_run(items, mode="external_model"),
            mock_run(items),
            expected_ids=("S01", "S02"),
        )
    changed = [mock_item("S01", question="changed"), mock_item("S02")]
    with pytest.raises(RuntimeError, match="问题或参考查询"):
        compare_mock_teacher.build_comparison(
            mock_run(items), mock_run(changed), expected_ids=("S01", "S02")
        )


def test_comparison_missing_input_is_actionable(tmp_path):
    with pytest.raises(FileNotFoundError, match="mock_teacher_eval"):
        compare_mock_teacher.read_run(tmp_path / "missing.json")


def test_cli_entry_points_validate_offline(capsys):
    assert evaluate.main(["--check-dataset"]) == 0
    assert "problems" in capsys.readouterr().out
    assert mock_teacher_eval.main(["--validate"]) == 0
    assert "problems" in capsys.readouterr().out
