"""Run the CURRENT analyze_page pipeline on the benchmark pages and score it.

    python benchmarks/drawing_qa/run_benchmark.py [--runs 3]

Needs NVIDIA_API_KEY (or the sandbox proxy), tesseract and the model in backend.config.
Results are printed and written to benchmarks/drawing_qa/results/latest.json.
"""
import argparse, asyncio, json, os, re, sys, time
from collections import defaultdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ.setdefault("NVIDIA_API_KEY", "proxy-injected")

from PIL import Image
from backend.services.ocr_service import ocr_image
from backend.services.llm_service import analyze_page
from benchmarks.drawing_qa.expected import EXPECTED
from benchmarks.drawing_qa.generate_drawings import PAGES, build, OUT

DET_MARKERS = ("stacked over the same span", "is dimensioned inconsistently")


def source_of(issue):
    d = issue["description"].lower()
    if "evidence" in issue or any(m in d for m in DET_MARKERS):
        return "deterministic"
    return "model"


def matches(defect, issue):
    desc = issue["description"].lower()
    if defect["types"] is not None and issue["issue_type"] not in defect["types"]:
        return False
    if not all(re.search(p, desc) for p in defect["all"]):
        return False
    if any(re.search(p, desc) for p in defect.get("none", [])):
        return False
    return True


def score_run(page, issues):
    defects = EXPECTED[page]
    caught, used, fps, dups = {}, set(), [], []
    for i, issue in enumerate(issues):
        hit = next((df for df in defects if matches(df, issue)), None)
        if hit is None:
            fps.append(issue)
        elif hit["id"] in caught:
            dups.append(issue)
        else:
            caught[hit["id"]] = issue
    return caught, fps, dups


async def one(page, runs, sem):
    img = Image.open(os.path.join(OUT, f"{page}.png")).convert("RGB")
    ocr = await asyncio.to_thread(ocr_image, img)
    results = []
    for _ in range(runs):
        async with sem:
            t = time.time()
            try:
                issues = await analyze_page(img, ocr, 1, 1)
                err = None
            except Exception as e:
                issues, err = [], f"{type(e).__name__}: {str(e)[:80]}"
            results.append({"issues": issues, "secs": round(time.time() - t, 1), "error": err})
    return page, ocr, results


async def main(runs):
    build()
    sem = asyncio.Semaphore(3)
    out = await asyncio.gather(*[one(p, runs, sem) for p in PAGES])
    report, tp = {}, 0
    per_defect = defaultdict(int)
    fp_total = dup_total = err_total = ok_runs = 0
    fp_by_source = defaultdict(int)
    for page, ocr, results in out:
        rec = {"ocr_chars": len(ocr), "runs": []}
        for r in results:
            if r["error"]:
                err_total += 1
            else:
                ok_runs += 1
            caught, fps, dups = score_run(page, r["issues"])
            for did, issue in caught.items():
                per_defect[did] += 1
                tp += 1
            fp_total += len(fps)
            dup_total += len(dups)
            for f in fps:
                fp_by_source[source_of(f)] += 1
            rec["runs"].append({
                "secs": r["secs"], "error": r["error"],
                "caught": {k: {**v, "source": source_of(v)} for k, v in caught.items()},
                "false_positives": [{**f, "source": source_of(f)} for f in fps],
                "duplicates": len(dups),
            })
        report[page] = rec

    n_def = sum(len(v) for v in EXPECTED.values())
    print(f"\n{'defect':<9}{'description':<58}{'caught':>8}")
    for page, defects in EXPECTED.items():
        for df in defects:
            print(f"{df['id']:<9}{df['label'][:56]:<58}{per_defect[df['id']]:>3}/{runs}")
    print(f"\nruns completed: {ok_runs}/{len(PAGES) * runs}   API errors: {err_total}")
    print(f"true positives (defect-runs caught): {tp}/{n_def * runs}  = recall {tp / (n_def * runs):.0%}")
    all_found = tp + fp_total
    print(f"false positives: {fp_total} total ({fp_total / max(ok_runs, 1):.2f} per page-run), "
          f"by source {dict(fp_by_source)};  precision {tp / all_found:.0%}" if all_found else "no findings")
    print("clean-page false positives:",
          sum(len(r["false_positives"]) for p in ("clean_1", "clean_2") for r in report[p]["runs"]))
    os.makedirs(os.path.join(os.path.dirname(__file__), "results"), exist_ok=True)
    json.dump({"runs": runs, "pages": report, "recall": tp / (n_def * runs)},
              open(os.path.join(os.path.dirname(__file__), "results", "latest.json"), "w"), indent=1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--runs", type=int, default=3)
    asyncio.run(main(ap.parse_args().runs))
