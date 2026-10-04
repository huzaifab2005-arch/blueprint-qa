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
