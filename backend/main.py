import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import DBAPIError, OperationalError
from backend.database import create_tables, check_connection
from backend.routers import documents, analysis, assistant, measure

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Blueprint QA API — creating tables if needed...")
    try:
        await create_tables()
        logger.info("Database tables ready.")
    except Exception as exc:
        logger.warning("DB init failed at startup (will retry on first request): %s", exc)
    yield
    logger.info("Shutting down Blueprint QA API.")


app = FastAPI(title="Blueprint QA API", version="1.0.0", lifespan=lifespan)

_extra_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:4173",
        *_extra_origins,
    ],
    allow_origin_regex=r"https://(.*\.onrender\.com|.*\.vercel\.app)",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(documents.router)
app.include_router(analysis.router)
app.include_router(assistant.router)
app.include_router(measure.router)


@app.exception_handler(OperationalError)
@app.exception_handler(DBAPIError)
async def database_unavailable_handler(request: Request, exc: Exception):
    """Report a dead database as 503 with a reason, not an opaque 500."""
    logger.error("Database error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Database unavailable. Check the DATABASE_URL environment "
            "variable on the API host and see /health/ready for details."
        },
    )


@app.get("/health")
async def health():
    """Liveness: the process is up. Deliberately does not touch the database,
    so a DB outage does not cause the platform to restart-loop the container."""
    return {"status": "ok"}


@app.get("/health/ready")
async def readiness():
    """Readiness: the app can actually serve requests.

    /health alone returned 200 while every DB-backed route was returning 500,
    which made a fully broken deploy look healthy. This probe executes a real
    query, so the failure is visible from outside.
    """
    try:
        await check_connection()
    except Exception as exc:
        logger.error("Readiness check: database unreachable: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "database": f"{type(exc).__name__}: {exc}"},
        )
    return {"status": "ready", "database": "ok"}


# Serve the SvelteKit static build (placed here by Dockerfile)
_static = Path(__file__).parent / "static"

if _static.is_dir():
    # Serve immutable assets with StaticFiles (efficient, proper cache headers)
    _app_assets = _static / "_app"
    if _app_assets.is_dir():
        app.mount("/_app", StaticFiles(directory=str(_app_assets)), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        """Serve static files or fall back to the SPA entry point."""
        candidate = _static / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_static / "200.html")
