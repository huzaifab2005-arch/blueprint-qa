# Blueprint QA

AI-powered quality assurance for construction and engineering drawings. Upload a PDF and a vision model will flag missing tags, dimension mismatches, unlabeled elements, and more.

## Live Demo

| Service | URL |
|---|---|
| Frontend | https://blueprint-qa-git-main-shlok-thakkars-projects.vercel.app |
| Backend API | https://blueprint-qa-3.onrender.com |
| API docs (Swagger) | https://blueprint-qa-3.onrender.com/docs |

## Deployment

- **Single URL (simplest):** deploy the Docker image from [`backend/Dockerfile`](backend/Dockerfile) on Render, [Fly.io](https://fly.io) ([`fly.toml`](fly.toml)), or Railway. The container serves the API and the static SPA; the UI uses same-origin `/api/...` (`.env.production` is excluded from the Docker build via [`.dockerignore`](.dockerignore)).
- **Split frontend + API:** point a Vercel project at the [`frontend/`](frontend/) directory. [`frontend/vercel.json`](frontend/vercel.json) treats the app as a static export (`index.html` + SPA rewrites). Keep [`frontend/.env.production`](frontend/.env.production) set to your public API origin.

## Drawing Assistant

Ask natural-language questions about an uploaded drawing set ("What model is RTU-1?", "How many 2x4 fixtures are there?", "What does detail 3/M2.02 show?"). Open a document and use the **Drawing Assistant** tab. The existing **QA Report** tab is unchanged.

**Pipeline**

```
PDF -> per-page render -> text (embedded text layer, OCR if absent) -> sheet number/title detection
    -> stored pages (document_pages)            [index, built once per document]

question -> (follow-up rewrite) -> retrieve top pages (BM25 over text + sheet metadata)
         -> vision model reads each candidate page (that page's text + image)
         -> synthesis over per-page findings -> answer + cited sheets
```

- Only the few retrieved pages are sent to the model, never the whole PDF.
- Every answer lists its source sheets; click one to open the page (with the quoted evidence).
- Cited pages must be pages that were retrieved and reported relevant findings. If nothing supports an answer, the reply is "The information could not be verified from the uploaded drawings."
- Evidence quotes are checked against the page's extracted text; ones that are not found are shown as *unconfirmed*.
- Counts come from a vision model and carry a warning to verify against the sheets.

**Endpoints** (`/api/assistant`): `POST /{id}/index`, `GET /{id}/index`, `GET /{id}/pages`, `GET /{id}/pages/{n}/image`, `POST /{id}/ask`, `GET|DELETE /{id}/messages`.

**Notes**: indexing runs in the API process after the request returns (progress is polled); a restart mid-index can be retried from the UI. Page images live in the configured storage backend, so on Render's ephemeral `/tmp` they are lost on redeploy (as is the PDF itself). Run tests with `pip install -r backend/requirements-dev.txt && pytest`.

### Search and visual navigation (Phase 2)

The **Search & Sheets** tab turns the indexed set into something you can look through:

- **Search** (`GET /api/assistant/{id}/search`): literal search across every sheet. All words must be on a sheet (quoted text is a phrase; the last word may be a prefix, so results appear as you type); tags and sizes match however the drawing writes them (`RTU-1` = `rtu1`, `2x4` = `2'x4'`); a dimension like `10'-8"` keeps its foot and inch marks; a sheet number finds that sheet first (`1.3`, `go to E2.01`, `page 7`). If no sheet has every word, sheets with some are shown and labelled as partial; a query that matches nothing offers a spelling taken from the document's own words. Search needs only the database.
- **Highlights** (`.../pages/{n}/highlights`): where the matches are on a sheet, from the PDF's word positions. Step through them with `n`/`p` or the buttons, "Zoom to match", or jump to the next sheet with a match. The same endpoint locates a cited quote, so an answer's source opens with its evidence highlighted.
- **Cross-references** (`.../pages/{n}/references`): "REFER TO DRAWING 1.1b" becomes a clickable region that opens sheet 1.1b, with a Back button. Letter-prefixed numbers (E2.01) count anywhere; plain numbers (1.3) only after a cue word (REFER TO, SEE, DRAWING) or as a detail callout, because "1.3" is also a dimension and a quantity.
- **Sheet grid** (`.../pages/{n}/thumbnail`): thumbnails of every sheet, made on first request and cached.

Limits: search matches text, so a note drawn as a picture is not searchable; highlights and references need the original PDF (if storage has lost it they are unavailable but text search still works); text split across wrapped lines is found by words, not as one phrase.

### Object counting (Phase 3)

Ask "How many 2x4 lights?", "How many RTUs?", "How many diffusers?", "How many doors?", "How many toilets?". A counting question is detected and answered by a separate pipeline, not by the language model:

1. The question is parsed into an object, optional sizes (`2x4`) and an optional area (`in BOH`).
2. Candidate sheets are retrieved, then each is read from the PDF's own data: words with positions and vector shapes (`pdfplumber`).
3. Evidence is collected, most reliable first: **tag labels** on the plan (`L1` x 8, `RTU-1`), a **legend symbol** matched against the plan's vector shapes, the schedule's **QTY column**, and the number of **schedule rows**. A vision-model estimate is used only if nothing in the PDF identifies the object but the sheets do mention it.
4. The answer says how it was counted, shows a count per sheet, and **outlines every counted object on the sheet** ("View marked sheet").

**How far to trust a number.** Status comes from agreement between independent methods, never from a model's say-so, and no status claims certainty:

| Status | Meaning |
|---|---|
| Cross-checked | two or more methods agree and nothing is in doubt |
| One source: verify | one method found it; nothing contradicts it |
| Needs verification | methods disagree, an area filter ("in BOH") could not be applied, symbol sizes are mixed, sheets of different types disagree, tags are known to mean types not units (doors), or the number is a vision-model estimate |
| Not counted | nothing countable was found (this does not prove there are none) |

Things it deliberately does not do: a size you ask about (2x4) is never answered with a different size; the vision model is not asked to count an object the sheets never mention; plain circles and squares are refused as legend symbols because they cannot be told apart from anything else; numbered note bubbles are rejected.

Limits: it counts what the legend or tags identify, so fixture types without a legend entry are not included (the answer says so); area filters need room boundaries, which are not read; scanned drawings without vector data fall back to the (flagged) vision estimate. `false_verified_rate` in the eval harness (`backend/evals/count_sample.py`) is the metric to watch: a count that is wrong but reported as cross-checked or single-source must never happen.

### Measurement (Phase 4)

Click **Measure** in the page viewer to measure lengths, paths and areas on a sheet.

- **Scale is verified, not trusted.** The stated scale ("3/16" = 1'-0"") is compared with the sheet's own tick-marked dimension lines (the real distance between ticks versus the dimension text). Each scale gets a status: `verified` (dimensions agree), `measured` (derived from dimensions, e.g. a reduced print), `calibrated` (you set it), `stated` (written on the sheet but not confirmed) or `conflict` (disagrees with the dimensions). Sheets with several scales list each one; measurements use the nearest unless you pick one.
- **Calibrate** by clicking both ends of a length you know and typing it (`12'-7"`, `24"`, `1200 mm`). Calibrations are saved per sheet and can be removed.
- **Snapping.** Clicks snap to the drawing's own vector corners (green ring); hold Alt to place freely.
- **Uncertainty is reported** with every value (click precision plus scale spread), in imperial and metric. Warnings are shown for stated-only or conflicting scales and multi-scale sheets. With no scale found, measuring is refused until you calibrate.
- Measurements are saved per document page (rename/delete) via `/api/measure/...`.

Limits: only horizontal dimension text is used to verify a scale; dimensions without tick marks are not used; scanned (raster-only) sheets have no snap points and rely on calibration. Treat results as checks to confirm against the dimensions on the drawing, not as certified quantities.

### Quantity takeoff (Phase 5)

The **Takeoff** tab collects quantities into lines. A line comes from a count, from saved measurements, or from a person, and keeps the evidence behind its number.

- **From a count:** "Add to takeoff" on a count answer in the Drawing Assistant, or type what to count in the Takeoff tab. Unit is EA; the pages counted and any caveats are stored with the line. Nothing counted means no line (add it manually instead).
- **From measurements:** tick saved measurements and total them (LF/SF, or m/m² on metric sheets). Lengths and areas, or metric and imperial sheets, are never mixed in one line. Uncertainty is the sum of the measurements' uncertainties.
- **Manual entry:** always marked **Manual**.
- **Status is not upgraded.** *Verified* means the source checked itself: a count cross-checked by a second independent reading, or measurements all on verified/measured/calibrated scales. A count with one reading, or measurements on a scale that is only stated or in conflict, is *Needs verification*. A person's entry, or an edited number, is *Manual* (the drawing's own value stays visible beside it, and returns the line to its original status if you restore it).
- **Waste** is a per-line percentage shown as a separate order quantity (whole items round up); it is never folded into the quantity.
- **Totals** are per unit only and always say how much of the total is verified.
- **Refresh** (↻) recomputes a line from the drawing; an edited quantity is kept and the new drawing value is shown next to it.
- **Export CSV** includes status, sources, basis and warnings for every line (text is escaped so spreadsheets do not run it as a formula).

Limits: lines are only as good as the counts and measurements behind them; there are no prices, assemblies or unit conversions between units, and area counts ("in the BOH") are not filtered by room. Treat the result as a starting list to check against the drawings, not a bid.

### Set checks (Phase 6)

The **Set Checks** tab runs cross-sheet checks over an indexed set and lists what a reviewer should look at. It sits beside the existing AI QA report and does not change it. Each finding gives the sheet, what was seen, the evidence, a severity (how much it matters if real) and a confidence (how sure the check is); "View sheet" opens the page with the spot outlined.

| Check | Flags |
|---|---|
| Duplicate sheet number | two pages with the same sheet number |
| Drawing list | a sheet named on the cover's "LIST OF DRAWINGS" that is not in the set, and sheets in the set that the list omits (numeric ranges like `0.0-0.8` are honoured) |
| Missing reference | "SEE E9.01" where no such sheet exists (only structured letter-prefixed numbers after a cue word, so dimensions and clause numbers are not mistaken for sheets) |
| Scale | a stated scale that disagrees with the sheet's own tick-marked dimensions (a reduced print, or a wrong label) |
| Dimension | one dimension whose text differs by >10% (and >6") from the length drawn, on a sheet whose other dimensions agree with each other; details at a different scale are not flagged |

The checks are deliberately conservative: on the real 25-sheet test set they raise nothing, and they do find each planted fault in synthetic sheets (wrong dimension, reduced print, missing reference, missing/extra sheet). Findings are prompts to look, not verdicts, and "nothing flagged" is not a guarantee the set is correct. A finding can be dismissed (it stays dismissed until its content changes) and restored.

Not checked yet: schedule-versus-plan tag consistency, title-block data (project name, dates, revisions) across sheets, and cross-sheet language-model questions.

### Discipline takeoffs: "Give me the lighting takeoff"

Ask the Drawing Assistant for **the lighting takeoff**, **the HVAC takeoff** or **the plumbing fixture takeoff** (or press the buttons at the top of the Takeoff tab). The set is searched for what belongs in that discipline and every item comes back with the same fields:

```
Item:        2X4 LED TROFFER
Description: Type L1
Model:       LITHONIA 2BLT4
Quantity:    8
Unit:        EA
Source:      E2.01            (consecutive sheets collapse: E2.01-E2.04)
Status:      Verified (cross-checked)
```

- **How items are found:** schedule rows (a line starting with a tag such as L1, RTU-1 or WC-1, read against the table's own column headers for description, manufacturer/model and QTY) and legend entries (a symbol with a heading and a text block, whose `MFR:` / `MODEL:` / `CODE:` lines give the model). A legend entry that describes the same thing as a schedule row (matched by size and name, e.g. "2'x4' LED TROFFER" and "2X4 LED TROFFER") is merged into that row as another independent reading.
- **How each item is counted:** with the same evidence as a "how many" question: tags on the plans, legend symbols matched on the plans, schedule QTY. It is **Verified** only when at least two of those agree; one reading is "Needs verification (one reading only)", and an item listed in the drawings but not found on the plans comes back with quantity 0 and a warning. Nothing is estimated and no model call is made, so it works without an API key.
- **Saved to the Takeoff tab** (category = the discipline), where quantities can be edited, waste added and CSV exported (columns: Item, Description, Model / specification, Quantity, Unit, Drawing source, Status, ...). Generating again refreshes the lines: ones you edited keep your quantity (the drawing's value is shown beside it) and manual lines are never touched.
- **On the real 25-sheet test set** (a restaurant fit-out with no mechanical or plumbing schedules): lighting gives its 4 legend items (13 recessed, 4 slim recessed, 2 pendant, 0 track, none cross-checked); HVAC finds nothing and says so; plumbing finds the floor drains. Findings like these show what the drawings let a program read; they are a starting list to check, not a bid.

Limits: letter-spaced headings are rejoined by their gaps and can still be misread; wrapped or overlapping legend text can leave a model number incomplete; switches, sensors and controls are not part of the lighting takeoff; sets that draw fixtures with no tag, schedule or legend symbol cannot be taken off.

### Running against the real NVIDIA API

Question answering and the counting fallback call a hosted vision model; everything else (indexing, retrieval, counting from tags/legends/schedules) runs without it. To evaluate the full pipeline with a real key:

1. **Provide the key as an environment variable** (`NVIDIA_API_KEY`), never in code, a commit or a chat message. In a cloud environment, add it under the environment's settings and start a new session.
2. **Allow the host.** If outbound network access is restricted, allow `integrate.api.nvidia.com` (the host in `LLM_BASE_URL`).
3. `pip install -r backend/requirements-dev.txt`
4. **Preflight**: `python -m backend.evals.preflight`. It checks the key, reachability, that the model is offered, JSON mode, that the model really *reads* an attached image, and that a page-sized image (hundreds of KB) is accepted. A failed "large" check means the endpoint caps inline image size: set `ASSISTANT_MODEL_IMAGE_MAX_BYTES=170000` (the app also retries once with a smaller image on its own).
5. **Full evaluation, no server or Postgres needed:**
   ```bash
   python -m backend.evals.run_eval --pdf your_set.pdf --cases backend/evals/cases/real_set_qa.json \
       --mode full --inprocess --show-answers --report qa_report.json
   python -m backend.evals.run_eval --pdf your_set.pdf --cases backend/evals/cases/real_set_count.json \
       --mode full --inprocess --show-answers
   ```
   The preflight runs first and the evaluation is skipped if it fails. Watch `false_answer_rate` and `false_verified_rate`: both must be 0.

Counting questions do not need a key at all, so the counting half can be run with step 5's second command on its own.

### Reliability (Phase 1)

Beyond citing sources, answers are checked against the drawing text before they are shown:

- **Grounding.** Identifiers in an answer (model numbers, tags, sizes) must appear in the cited sheet's extracted text or in the question. An unsupported claim is discarded, so the reply becomes "could not be verified". A combined multi-sheet answer that adds unsupported details is replaced by the per-sheet findings.
- **Confidence is capped by evidence.** If quotes are missing or not found in the sheet text, confidence is *low* regardless of what the model claimed.
- **Incompleteness is disclosed.** The reply warns when more sheets matched than were read, and when pages have no readable text (so they could not be searched).
- **Resilience.** Transient API errors and unparseable model output are retried (`ASSISTANT_LLM_RETRIES`, default 2).

**Measuring it.** `backend/evals/` has an evaluation harness:

```bash
python -m backend.evals.run_eval --sample                          # offline retrieval recall on a synthetic set
python -m backend.evals.run_eval --pdf set.pdf --cases cases.json  # retrieval recall on your drawings
python -m backend.evals.run_eval --pdf set.pdf --cases cases.json --mode full --api http://localhost:8000
```

Full mode asks every question through the real model and reports `answer_accuracy`, `citation_accuracy`, `abstention_rate` and `false_answer_rate` (questions the drawings do not answer, but which got an answer anyway; this must be 0). The case format is documented in `run_eval.py`. The built-in sample set is a plumbing and retrieval regression fixture, not a model benchmark: build a case file from your own drawings to measure real quality.

**Sheet detection and retrieval.** Sheet numbers and titles are read from the title block by anchoring on its labels (`DESCRIPTION:`, `SHEET NO.`), which handles plain numeric numbers (1.3, 4.0, 1.1a) as well as E2.01-style ones; if no title block is found the page is shown as "Page N" rather than guessed. Retrieval corrects typos against the document's own vocabulary, treats `A.F.F.` as `AFF` and "back of house" as `BOH`, weights answer-type words (dimensions, size, model, list) below topic words, and favours dimension-dense sheets for dimension questions. **Documents indexed before this change must be re-indexed** (Re-index button) to pick up the new sheet numbers.

Known limits: a question whose answer needs a second hop through the drawings (for example, "how many 2x4 fixtures" when the plan only shows tag `L1`) retrieves the schedule but not the plan. That belongs to the object-counting phase.

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | SvelteKit + Tailwind CSS (deployed on Vercel) |
| Backend | FastAPI (Python 3.12, deployed on Render) |
| Database | PostgreSQL / Supabase (async SQLAlchemy + psycopg) |
| OCR | pytesseract + pdf2image |
| AI | NVIDIA NIM — `meta/llama-3.2-11b-vision-instruct` (multimodal, OpenAI-compatible API) |
| Storage | Local filesystem (/tmp on Render) |

---

## Quick Start — Docker Compose

### 1. Prerequisites
- Docker + Docker Compose
- An [NVIDIA API key](https://build.nvidia.com/) (starts with `nvapi-`)

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and set your NVIDIA_API_KEY and DATABASE_URL
```

### 3. Start all services

```bash
docker-compose up --build
```

| Service | URL |
|---|---|
| Frontend | http://localhost:5173 |
| Backend API | http://localhost:8000 |
| API docs (Swagger) | http://localhost:8000/docs |

---

## Manual Dev Setup

### Backend

#### System requirements (macOS/Ubuntu)

```bash
# macOS
brew install poppler tesseract

# Ubuntu/Debian
sudo apt-get install poppler-utils tesseract-ocr tesseract-ocr-eng
```

#### Python setup

```bash
cd blueprint-qa
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

#### Configure and run

```bash
cp .env.example .env
# Fill in NVIDIA_API_KEY and DATABASE_URL

uvicorn backend.main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
# Open http://localhost:5173
```

---

## Seed Demo Data

Insert 2 completed documents with 5 issues each (no real PDF files needed for viewing):

```bash
# From the project root with the venv active:
python -m backend.seed
```

---

## API Reference

| Method | Path | Description |
|---|---|---|
| POST | `/api/documents/upload` | Upload a PDF |
| GET | `/api/documents` | List all documents |
| GET | `/api/documents/{id}` | Get document details |
| DELETE | `/api/documents/{id}` | Delete document + issues |
| POST | `/api/analysis/{id}/run` | Run QA analysis |
| GET | `/api/analysis/{id}/issues` | Get issues for a document |
| GET | `/api/analysis/{id}/summary` | Issue summary stats |
| — | `/api/assistant/...` | Drawing assistant (see above) |

Interactive docs: https://blueprint-qa-3.onrender.com/docs

---

## Project Structure

```
blueprint-qa/
├── backend/
│   ├── main.py               # FastAPI app + lifespan
│   ├── config.py             # Pydantic settings
│   ├── database.py           # Async SQLAlchemy engine
│   ├── models/               # SQLAlchemy ORM models
│   ├── schemas/              # Pydantic request/response schemas
│   ├── routers/              # API route handlers
│   ├── services/
│   │   ├── ocr_service.py    # pdf2image + pytesseract
│   │   ├── llm_service.py    # NVIDIA NIM multimodal calls
│   │   └── qa_service.py     # Pipeline orchestration
│   ├── storage/              # Local storage adapter
│   ├── seed.py               # Demo data seeder
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── routes/           # SvelteKit pages + server load functions
│   │   └── lib/
│   │       ├── api.ts        # Typed API client
│   │       ├── components/   # Svelte UI components
│   │       └── stores/       # Svelte writable stores
│   └── package.json
├── docker-compose.yml
├── render.yaml               # Render deployment config
├── .env.example
└── README.md
```

---

## Deployment

### Backend (Render)
- Runtime: Docker
- Dockerfile: `./backend/Dockerfile`
- Docker Build Context: `.` (repo root)
- Environment variables: `DATABASE_URL`, `NVIDIA_API_KEY`, `UPLOAD_DIR=/tmp/uploads`

### Frontend (Vercel)
- Framework: SvelteKit
- Root directory: `frontend`
- Connected to GitHub `shlok1806/blueprint-qa`

### Database (Supabase)
- PostgreSQL with SSL required
- Tables: `documents`, `issues`
- **Use a pooler connection string, not the direct one.** Supabase's direct host
  (`db.<ref>.supabase.co`) resolves to IPv6 only, and Render has no IPv6 egress, so
  it fails with a connection timeout on every request. Copy the **Session pooler**
  string from Project Settings -> Database:
  `postgresql+psycopg://postgres.<ref>:<password>@<cluster>-<region>.pooler.supabase.com:5432/postgres`

---

## Troubleshooting

| Symptom | Check | Likely cause |
|---|---|---|
| API routes return 503 "Database unavailable" | `GET /health/ready` | `DATABASE_URL` wrong, or pointed at the IPv6-only direct Supabase host |
| `/health` is 200 but the UI shows "Couldn't reach the API" | `GET /health/ready` | Database is down; the app is up |
| UI calls the wrong API host | Network tab on the deployed page | Stale `VITE_API_URL` baked into the frontend build. It is compiled in at build time, so changing it requires a redeploy |

`/health` is liveness only and never touches the database, because `render.yaml`
uses it as the platform health check. `/health/ready` is the database readiness probe.
