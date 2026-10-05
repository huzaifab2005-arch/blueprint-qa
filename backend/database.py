import logging
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from backend.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# Supabase hands out three connection strings. Only two of them work from a
# host without IPv6 egress (Render, Fly, most CI runners):
#
#   db.<ref>.supabase.co:5432          direct     IPv6-only, DO NOT USE
#   <region>.pooler.supabase.com:5432  session    IPv4, safe default
#   <region>.pooler.supabase.com:6543  transaction IPv4, needs prepare_threshold=None
#
# Pointing at the direct host from an IPv4-only host fails with a connection
# timeout, which surfaces as a 500 on every DB-backed route.
_DIRECT_SUPABASE_HOST = re.compile(r"@db\.[a-z0-9]+\.supabase\.co")


def normalise_driver(url: str) -> str:
    """Point the URL at the async driver that is actually installed.

    The project moved from asyncpg to psycopg3, but a DATABASE_URL set before
    that migration still says `postgresql+asyncpg://`. SQLAlchemy resolves the
    driver from the URL scheme at import time, so a stale value crashes the
    process with ModuleNotFoundError before the app can serve anything. On a
    platform that health-checks a new container, that turns into a failed deploy
    and a silent rollback to the previous image, which is very hard to spot.

    Rewriting the scheme when the named driver is missing keeps a stale env var
    from taking the service down. The warning says what to fix.
    """
    if "+asyncpg" not in url:
        return url
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        logger.warning(
            "DATABASE_URL requests the asyncpg driver, which is not installed. "
            "Falling back to psycopg. Update DATABASE_URL to use "
            "postgresql+psycopg:// to silence this."
        )
        return url.replace("+asyncpg", "+psycopg", 1)
    return url


def _describe(url: str) -> tuple[bool, bool, bool]:
    """Return (is_supabase, is_pooler, is_transaction_mode) for a database URL."""
    is_pooler = "pooler.supabase.com" in url
    is_supabase = "supabase.co" in url or is_pooler
    is_transaction_mode = is_pooler and ":6543" in url
    return is_supabase, is_pooler, is_transaction_mode


def build_connect_args(url: str) -> dict:
    """Connection kwargs for psycopg3, tuned for how the URL reaches Postgres."""
    is_supabase, is_pooler, is_transaction_mode = _describe(url)
    # Without this, an unreachable host hangs on the OS TCP timeout (over a
    # minute), which stalls startup and every request behind it instead of
    # failing fast with a usable error.
    connect_args: dict = {"connect_timeout": 10}

    if is_supabase:
        connect_args["sslmode"] = "require"

    if is_transaction_mode:
        # PgBouncer in transaction mode multiplexes one server connection
        # across clients, so a prepared statement created on one request is
        # not there on the next. psycopg3 auto-prepares after 5 executions,
        # which then fails with DuplicatePreparedStatement / InvalidSqlStatementName.
        connect_args["prepare_threshold"] = None

    return connect_args


DATABASE_URL = normalise_driver(settings.database_url)

if _DIRECT_SUPABASE_HOST.search(DATABASE_URL):
    logger.warning(
        "DATABASE_URL points at the direct Supabase host (db.<ref>.supabase.co), which "
        "resolves to IPv6 only. Hosts without IPv6 egress (Render, Fly) cannot reach it. "
        "Use the session pooler URL (<region>.pooler.supabase.com:5432) instead."
    )

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args=build_connect_args(DATABASE_URL),
    # Free-tier Postgres and PgBouncer both drop idle connections; without this
    # the first request after an idle period fails on a stale pooled connection.
    pool_pre_ping=True,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


# Columns added after a table first shipped. create_all() creates missing TABLES but
# never alters existing ones, so a database created by an earlier version would
# otherwise be missing these and fail on first insert.
_ADDED_COLUMNS = (
    ("chat_messages", "count_result", "JSON"),
    ("takeoff_items", "details", "TEXT NOT NULL DEFAULT ''"),
    ("takeoff_items", "model", "VARCHAR(300) NOT NULL DEFAULT ''"),
)


def _ensure_columns(sync_conn) -> None:
    from sqlalchemy import inspect

    insp = inspect(sync_conn)
    for table, column, ddl_type in _ADDED_COLUMNS:
        if not insp.has_table(table):
            continue
        if column not in {c["name"] for c in insp.get_columns(table)}:
            logger.warning("Adding missing column %s.%s", table, column)
            sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_columns)


async def check_connection() -> None:
    """Raise if the database is not reachable. Used by the health endpoint."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
