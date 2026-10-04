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

Case format (cases.json = a list of objects):
  {"id": "rtu1-model",
   "question": "What model is RTU-1?",
   "expected_sheets": ["M6.01"],      # sheets that must be retrieved / cited
   "must_include": ["YHC074"],        # substrings a correct answer contains
   "must_not_include": [],            # substrings that mean the answer is wrong
   "expect_unverified": false}        # true = the drawings do not say; must decline

Headline metrics, full mode:
  false_answer_rate  negatives answered as if known (must be 0)
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
    kind: str = "positive"  # positive | negative
    checks: dict[str, bool] = field(default_factory=dict)


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


def score_answer(case: dict, answer: dict) -> CaseResult:
    """Score one API answer (an AskResponse['answer'] dict)."""
    content = answer.get("content", "")
    verified = bool(answer.get("verified"))
    cited = [s.get("label", "") for s in answer.get("sources", [])]

    if case.get("expect_unverified"):
        declined = (not verified) and content.startswith(UNVERIFIED_PREFIX)
        return CaseResult(
            case["id"], declined,
            "" if declined else f"FALSE ANSWER: {content[:160]!r}",
            kind="negative", checks={"abstained": declined},
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
        checks={"answer": correct, "citation": cites},
    )


def summarise(results: list[CaseResult]) -> dict:
    def rate(items: list[bool]) -> float | None:
        return round(sum(items) / len(items), 3) if items else None

    pos = [r for r in results if r.kind == "positive"]
    neg = [r for r in results if r.kind == "negative"]
    summary = {
        "cases": len(results),
        "passed": sum(r.ok for r in results),
        "answer_accuracy": rate([r.checks["answer"] for r in pos if "answer" in r.checks]),
        "citation_accuracy": rate([r.checks["citation"] for r in pos if "citation" in r.checks]),
        "abstention_rate": rate([r.ok for r in neg]),
        "false_answer_rate": (rate([not r.ok for r in neg])),
    }
    if not any("answer" in r.checks for r in pos):  # retrieval mode
        summary["retrieval_recall"] = rate([r.ok for r in pos])
    return summary


# ── Modes ───────────────────────────────────────────────────────────────────

def index_pdf_offline(pdf_path: str, max_pages: int = 150) -> list[tuple[int, str | None, str | None, str]]:
    """(page_number, sheet_number, sheet_title, text) for each page, no DB."""
    from backend.services.drawing_metadata import detect_sheet_number, detect_sheet_title
    from backend.services.indexing_service import count_pdf_pages, process_page

    out = []
    for n in range(1, min(count_pdf_pages(pdf_path), max_pages) + 1):
        processed = process_page(pdf_path, n)
        sheet = detect_sheet_number(processed.text)
        out.append((n, sheet, detect_sheet_title(processed.text, sheet), processed.text))
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


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["retrieval", "full"], default="retrieval")
    ap.add_argument("--pdf")
    ap.add_argument("--cases")
    ap.add_argument("--sample", action="store_true", help="use the built-in synthetic drawing set")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--top-k", type=int, default=4)
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)

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
    else:
        results = asyncio.run(run_full(pdf, cases, args.api))

    summary = summarise(results)
    for r in results:
        print(f"{'PASS' if r.ok else 'FAIL'}  {r.id}" + (f"  - {r.detail}" if r.detail else ""))
    print(json.dumps(summary) if args.json else "\n" + "\n".join(f"{k}: {v}" for k, v in summary.items()))
    if tmp:
        Path(tmp.name).unlink(missing_ok=True)

    failed = summary["false_answer_rate"] not in (None, 0) or summary["passed"] < summary["cases"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
