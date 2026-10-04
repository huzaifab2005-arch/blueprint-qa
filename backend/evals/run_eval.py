"""Measure drawing-assistant reliability.

Two modes:

  retrieval (default, offline, deterministic)
      Indexes the PDF locally and checks that the sheets each question needs are
      among the top-K retrieved. No model, no database, no network.

  full (live)
      Uploads the PDF to a running API, indexes it, asks every question through
      the real pipeline and scores the answers. Needs NVIDIA_API_KEY on the API
      host. This is the number that matters; retrieval is only its ceiling.

Usage (from the repo root):
  python -m backend.evals.run_eval --sample
  python -m backend.evals.run_eval --pdf set.pdf --cases cases.json
  python -m backend.evals.run_eval --sample --mode full --api http://localhost:8000
  python -m backend.evals.run_eval --pdf set.pdf --cases cases.json --mode full --inprocess --show-answers

Case format (cases.json = a list of objects):
  {"id": "rtu1-model",
   "question": "What model is RTU-1?",
   "expected_sheets": ["M6.01"],      # sheets that must be retrieved / cited
   "must_include": ["YHC074"],        # substrings a correct answer contains
   "must_not_include": [],            # substrings that mean the answer is wrong
   "expect_unverified": false}        # true = the drawings do not say; must decline

Headline metrics, full mode:
  false_answer_rate  negatives answered as if known (must be 0)
  false_verified_rate  count answers that were wrong but reported as cross_checked or
                     single_source (must be 0; counting cases carry "expected_count")
  answer_accuracy    positives answered correctly
  citation_accuracy  positives citing every expected sheet
  abstention_rate    negatives correctly declined
"""
import argparse
import asyncio
import json
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend.services.drawing_metadata import page_label
from backend.services.grounding import alnum
from backend.services.retrieval_service import PageDoc, rank_pages

UNVERIFIED_PREFIX = "The information could not be verified"


# ── Scoring (pure) ──────────────────────────────────────────────────────────

@dataclass
class CaseResult:
    id: str
    ok: bool
    detail: str = ""
    kind: str = "positive"  # positive | negative | count
    checks: dict[str, bool] = field(default_factory=dict)
    answer: str = ""        # the answer text, so a live run can be read, not just scored
    status: str = ""        # count answers: cross_checked | single_source | ...


def _norm_sheet(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def score_retrieval(case: dict, retrieved_labels: list[str]) -> CaseResult | None:
    """Recall check. Negatives have nothing to retrieve, so they are skipped."""
    expected = case.get("expected_sheets") or []
    if case.get("expect_unverified") or not expected:
        return None
    got = {_norm_sheet(x) for x in retrieved_labels}
    missing = [s for s in expected if _norm_sheet(s) not in got]
    return CaseResult(
        case["id"], not missing,
        "" if not missing else f"missing {missing}; retrieved {retrieved_labels}",
    )


def score_count(case: dict, answer: dict) -> CaseResult:
    """Score an object-count answer. The failure that matters most is a count that is
    wrong yet reported as cross_checked or single_source ("false verified")."""
    cr = answer.get("count_result") or {}
    qty, status = cr.get("quantity"), cr.get("status")
    expected = case["expected_count"]
    correct = qty == expected
    verified_claim = status in ("cross_checked", "single_source")
    allowed = status in case.get("expected_status", [status])
    problems = []
    if not correct:
        problems.append(f"counted {qty}, expected {expected}")
    if not allowed:
        problems.append(f"status {status}, expected one of {case.get('expected_status')}")
    false_verified = verified_claim and not correct
    if false_verified:
        problems.insert(0, "FALSE VERIFIED")
    res = CaseResult(
        case["id"], correct and allowed, "; ".join(problems), kind="count",
        checks={"count": correct, "status": allowed, "false_verified": false_verified},
        answer=answer.get("content", ""), status=str(status),
    )
    return res


def score_answer(case: dict, answer: dict) -> CaseResult:
    """Score one API answer (an AskResponse['answer'] dict)."""
    if "expected_count" in case:
        return score_count(case, answer)
    content = answer.get("content", "")
    verified = bool(answer.get("verified"))
    cited = [s.get("label", "") for s in answer.get("sources", [])]

    if case.get("expect_unverified"):
        declined = (not verified) and content.startswith(UNVERIFIED_PREFIX)
        return CaseResult(
            case["id"], declined,
            "" if declined else f"FALSE ANSWER: {content[:160]!r}",
            kind="negative", checks={"abstained": declined}, answer=content,
        )

    text = alnum(content)
    includes = all(alnum(x) in text for x in case.get("must_include", []))
    excludes = not any(alnum(x) in text for x in case.get("must_not_include", []))
    correct = verified and includes and excludes
    cites = all(_norm_sheet(s) in {_norm_sheet(c) for c in cited} for s in case.get("expected_sheets", []))
    problems = []
    if not verified:
        problems.append("declined to answer")
    elif not includes:
        problems.append(f"missing {case.get('must_include')}")
    if not excludes:
        problems.append("contains forbidden text")
    if not cites:
        problems.append(f"cited {cited}, expected {case.get('expected_sheets')}")
    return CaseResult(
        case["id"], correct and cites,
        "; ".join(problems) + (f" | answer: {content[:120]!r}" if problems else ""),
        checks={"answer": correct, "citation": cites}, answer=content,
    )


def summarise(results: list[CaseResult]) -> dict:
    def rate(items: list[bool]) -> float | None:
        return round(sum(items) / len(items), 3) if items else None

    pos = [r for r in results if r.kind == "positive"]
    neg = [r for r in results if r.kind == "negative"]
    searches = [r for r in results if r.kind == "search"]
    if searches:
        return {"cases": len(results), "passed": sum(r.ok for r in results),
                "search_pass_rate": rate([r.ok for r in searches]), "false_answer_rate": None}
    summary = {
        "cases": len(results),
        "passed": sum(r.ok for r in results),
        "answer_accuracy": rate([r.checks["answer"] for r in pos if "answer" in r.checks]),
        "citation_accuracy": rate([r.checks["citation"] for r in pos if "citation" in r.checks]),
        "abstention_rate": rate([r.ok for r in neg]),
        "false_answer_rate": (rate([not r.ok for r in neg])),
    }
    counts = [r for r in results if r.kind == "count"]
    if counts:
        summary["count_accuracy"] = rate([r.checks["count"] for r in counts])
        summary["count_status_ok"] = rate([r.checks["status"] for r in counts])
        summary["false_verified_rate"] = rate([r.checks["false_verified"] for r in counts])
    if not any("answer" in r.checks for r in pos) and pos:  # retrieval mode
        summary["retrieval_recall"] = rate([r.ok for r in pos])
    return summary


# ── Modes ───────────────────────────────────────────────────────────────────

def index_pdf_offline(pdf_path: str, max_pages: int = 150) -> list[tuple[int, str | None, str | None, str]]:
    """(page_number, sheet_number, sheet_title, text) for each page, no DB."""
    from backend.services.drawing_metadata import detect_sheet_metadata
    from backend.services.indexing_service import count_pdf_pages, process_page

    out = []
    for n in range(1, min(count_pdf_pages(pdf_path), max_pages) + 1):
        processed = process_page(pdf_path, n)
        sheet, title = detect_sheet_metadata(processed.raw_text, processed.text)
        out.append((n, sheet, title, processed.text))
    return out


def run_search(pdf_path: str, cases: list[dict]) -> list[CaseResult]:
    """Literal-search quality, offline. A case is
    {"id", "query", "expected_sheets": [...], "first": "1.3"?, "expect_none": true?, "suggestion": "washroom"?}.
    A sheet counts as found if it is among the results; "first" pins the top result."""
    from backend.services.search_service import PageRecord, search_pages

    pages = index_pdf_offline(pdf_path)
    records = [PageRecord(n, text, sheet, title) for n, sheet, title, text in pages]
    out = []
    for case in cases:
        resp = search_pages(records, case["query"])
        got = [r.label for r in resp.results]
        problems = []
        if case.get("expect_none") and resp.results:
            problems.append(f"expected no results, got {got[:5]}")
        missing = [x for x in case.get("expected_sheets", []) if _norm_sheet(x) not in {_norm_sheet(g) for g in got}]
        if missing:
            problems.append(f"missing {missing}; got {got[:6]}")
        if case.get("first") and (not got or _norm_sheet(got[0]) != _norm_sheet(case["first"])):
            problems.append(f"top result {got[:1]}, expected {case['first']}")
        if case.get("suggestion") and resp.suggestion != case["suggestion"]:
            problems.append(f"suggestion {resp.suggestion!r}, expected {case['suggestion']!r}")
        out.append(CaseResult(case["id"], not problems, "; ".join(problems), kind="search"))
    return out


def run_retrieval(pdf_path: str, cases: list[dict], top_k: int = 4) -> list[CaseResult]:
    pages = index_pdf_offline(pdf_path)
    docs = [PageDoc(n, text, sheet, title) for n, sheet, title, text in pages]
    labels = {n: page_label(n, sheet) for n, sheet, _, _ in pages}
    results = []
    for case in cases:
        ranked = rank_pages(case["question"], docs, top_k=top_k)
        scored = score_retrieval(case, [labels[r.page_number] for r in ranked])
        if scored:
            results.append(scored)
    return results


async def run_full(pdf_path: str, cases: list[dict], api: str, transport=None) -> list[CaseResult]:
    """`transport` lets tests drive an in-process app instead of a live server."""
    import httpx

    async with httpx.AsyncClient(base_url=api, timeout=300, transport=transport) as c:
        with open(pdf_path, "rb") as f:
            r = await c.post("/api/documents/upload", files={"file": (Path(pdf_path).name, f, "application/pdf")})
        r.raise_for_status()
        doc_id = r.json()["id"]
        (await c.post(f"/api/assistant/{doc_id}/index")).raise_for_status()
        while True:
            st = (await c.get(f"/api/assistant/{doc_id}/index")).json()
            if st["status"] == "ready":
                break
            if st["status"] == "failed":
                raise RuntimeError(f"indexing failed: {st['error']}")
            await asyncio.sleep(2)

        results = []
        for case in cases:
            await c.delete(f"/api/assistant/{doc_id}/messages")  # keep cases independent
            t0 = time.time()
            resp = await c.post(f"/api/assistant/{doc_id}/ask", json={"question": case["question"]})
            if resp.status_code != 200:
                results.append(CaseResult(case["id"], False, f"HTTP {resp.status_code}: {resp.text[:120]}",
                                          kind="negative" if case.get("expect_unverified") else "positive"))
                continue
            res = score_answer(case, resp.json()["answer"])
            res.detail = (res.detail + f" ({time.time() - t0:.1f}s)").strip()
            results.append(res)
        await c.delete(f"/api/documents/{doc_id}")
    return results


async def run_inprocess(pdf_path: str, cases: list[dict]) -> list[CaseResult]:
    """Run the full pipeline against the app inside this process, on a throwaway SQLite
    database and upload directory. Needs only the key (and network to the LLM): no
    Postgres, no separate server. Requires `pip install -r backend/requirements-dev.txt`."""
    import tempfile

    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.config import get_settings
    from backend.database import Base, get_db
    from backend.main import app
    from backend.services import indexing_service

    tmp = tempfile.mkdtemp(prefix="bpqa-eval-")
    get_settings().upload_dir = f"{tmp}/uploads"
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp}/eval.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with session() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    original = indexing_service.AsyncSessionLocal
    indexing_service.AsyncSessionLocal = session
    try:
        return await run_full(pdf_path, cases, "http://eval", transport=httpx.ASGITransport(app=app))
    finally:
        indexing_service.AsyncSessionLocal = original
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["retrieval", "full", "search"], default="retrieval")
    ap.add_argument("--pdf")
    ap.add_argument("--cases")
    ap.add_argument("--sample", action="store_true", help="use the built-in synthetic drawing set")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--top-k", type=int, default=None, help="default: ASSISTANT_TOP_K")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    ap.add_argument("--inprocess", action="store_true",
                    help="full mode: run the app in this process on a temporary SQLite database (no server needed)")
    ap.add_argument("--show-answers", action="store_true", help="print each answer, not just pass/fail")
    ap.add_argument("--report", help="write per-case results and the summary to this JSON file")
    ap.add_argument("--skip-preflight", action="store_true", help="full mode: do not check the LLM endpoint first")
    args = ap.parse_args(argv)
    if args.top_k is None:
        from backend.config import get_settings
        args.top_k = get_settings().assistant_top_k

    tmp = None
    if args.sample:
        from backend.evals.sample_set import CASES, build_sample_pdf
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.write(build_sample_pdf())
        tmp.close()
        pdf, cases = tmp.name, CASES
    elif args.pdf and args.cases:
        pdf, cases = args.pdf, json.loads(Path(args.cases).read_text())
    else:
        ap.error("give --sample, or both --pdf and --cases")

    if args.mode == "retrieval":
        results = run_retrieval(pdf, cases, args.top_k)
    elif args.mode == "search":
        results = run_search(pdf, cases)
    else:
        if not args.skip_preflight:
            from backend.evals.preflight import run_checks

            checks = asyncio.run(run_checks())
            for name, status, detail in checks:
                print(f"preflight {status:4} {name}: {detail}")
            if any(c[1] == "FAIL" for c in checks):
                print("\nPreflight failed; not running the evaluation. Fix the above, or pass --skip-preflight.")
                return 2
            print()
        results = asyncio.run(run_inprocess(pdf, cases) if args.inprocess else run_full(pdf, cases, args.api))

    summary = summarise(results)
    for r in results:
        tag = f" [{r.status}]" if r.status else ""
        print(f"{'PASS' if r.ok else 'FAIL'}  {r.id}{tag}" + (f"  - {r.detail}" if r.detail else ""))
        if args.show_answers and r.answer:
            print("      " + r.answer.replace("\n", "\n      ")[:500])
    if args.report:
        Path(args.report).write_text(json.dumps({
            "summary": summary,
            "results": [{"id": r.id, "ok": r.ok, "kind": r.kind, "status": r.status, "detail": r.detail,
                         "answer": r.answer} for r in results],
        }, indent=2))
    print(json.dumps(summary) if args.json else "\n" + "\n".join(f"{k}: {v}" for k, v in summary.items()))
    if tmp:
        Path(tmp.name).unlink(missing_ok=True)

    failed = (summary["false_answer_rate"] not in (None, 0) or summary.get("false_verified_rate") not in (None, 0)
              or summary["passed"] < summary["cases"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
