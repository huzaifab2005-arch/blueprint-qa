import json

import pytest

pytest.importorskip("reportlab")

from backend.evals import run_eval
from backend.evals.run_eval import CaseResult, score_answer, score_retrieval, summarise
from backend.evals.sample_set import CASES, build_sample_pdf


def test_retrieval_regression_on_sample_set(tmp_path):
    """Every question in the sample set must retrieve the sheets it needs in the top 4.
    If a retrieval change breaks this, it made the assistant worse at finding pages."""
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(build_sample_pdf())
    results = run_eval.run_retrieval(str(pdf), CASES, top_k=4)
    failures = [f"{r.id}: {r.detail}" for r in results if not r.ok]
    assert not failures, failures
    assert len(results) >= 15


def test_score_answer_positive():
    case = {"id": "x", "expected_sheets": ["M6.01"], "must_include": ["YHC074"]}
    good = {"content": "RTU-1 is a TRANE YHC-074.", "verified": True, "sources": [{"label": "M6.01"}]}
    assert score_answer(case, good).ok
    wrong_sheet = {**good, "sources": [{"label": "M2.01"}]}
    assert not score_answer(case, wrong_sheet).ok
    wrong_text = {**good, "content": "RTU-1 is a CARRIER."}
    assert not score_answer(case, wrong_text).ok
    declined = {"content": "The information could not be verified from the uploaded drawings.", "verified": False, "sources": []}
    r = score_answer(case, declined)
    assert not r.ok and "declined" in r.detail


def test_score_answer_forbidden_text():
    case = {"id": "x", "must_include": ["YHC074"], "must_not_include": ["YHC092"]}
    ans = {"content": "YHC074 and YHC092", "verified": True, "sources": []}
    assert not score_answer(case, ans).ok


def test_score_answer_negative_case():
    case = {"id": "n", "expect_unverified": True}
    declined = {"content": "The information could not be verified from the uploaded drawings.", "verified": False}
    assert score_answer(case, declined).ok
    invented = {"content": "The chiller is a TRANE CVHF.", "verified": True, "sources": [{"label": "M6.01"}]}
    r = score_answer(case, invented)
    assert not r.ok and "FALSE ANSWER" in r.detail


def test_score_retrieval():
    case = {"id": "r", "expected_sheets": ["E2.01", "E6.01"]}
    assert score_retrieval(case, ["E6.01", "E2.01"]).ok
    assert not score_retrieval(case, ["E6.01"]).ok
    assert score_retrieval({"id": "n", "expect_unverified": True}, []) is None


def test_summarise_and_exit_semantics():
    results = [
        CaseResult("a", True, checks={"answer": True, "citation": True}),
        CaseResult("b", False, checks={"answer": False, "citation": True}),
        CaseResult("c", True, kind="negative", checks={"abstained": True}),
        CaseResult("d", False, kind="negative", checks={"abstained": False}),
    ]
    s = summarise(results)
    assert s["answer_accuracy"] == 0.5 and s["citation_accuracy"] == 1.0
    assert s["abstention_rate"] == 0.5 and s["false_answer_rate"] == 0.5


def test_cli_retrieval_mode_exit_code(capsys):
    assert run_eval.main(["--sample", "--json"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["retrieval_recall"] == 1.0


def test_numeric_titleblock_set_end_to_end(tmp_path):
    """Plain numeric sheet numbers are read from the title block through the real
    pdftotext path, and the right sheets are retrieved."""
    from backend.evals.sample_set import NUMERIC_CASES, build_numeric_titleblock_pdf

    pdf = tmp_path / "n.pdf"
    pdf.write_bytes(build_numeric_titleblock_pdf())
    pages = run_eval.index_pdf_offline(str(pdf))
    assert [(s, t) for _, s, t, _ in pages] == [
        ("1.1a", "EQUIPMENT PLAN"), ("1.3", "REFLECTED CEILING PLAN"),
        ("4.0", "WASHROOM DETAILS"), ("0.1", "GENERAL REQUIREMENTS & SPECIFICATIONS"),
    ]
    results = run_eval.run_retrieval(str(pdf), NUMERIC_CASES, top_k=2)
    assert all(r.ok for r in results), [(r.id, r.detail) for r in results]
