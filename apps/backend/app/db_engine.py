"""SQLite engine/session plumbing for the SQLAlchemy data layer.

Every ``Database`` instance owns its own engines (one async for the document
tables, one sync for the encrypted ``api_keys`` table read on the synchronous
LLM hot path) built from these factories. Keeping construction here lets tests
spin up fully isolated engines against a temp-file database.
"""

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.models import Base

__all__ = ["Base", "make_async_engine", "make_sync_engine", "init_models_sync"]


def _apply_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Set per-connection SQLite PRAGMAs.

    WAL improves concurrent read/write between the async (doc tables) and sync
    (api_keys) engines pointed at the same file; ``busy_timeout`` rides out the
    brief lock contention that creates; ``foreign_keys`` enforces relational
    integrity (off by default in SQLite).
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def _url(path: Path, *, driver: str) -> str:
    """Build a SQLite URL. Absolute paths yield the required four slashes."""
    return f"sqlite+{driver}:///{path}" if driver else f"sqlite:///{path}"


def make_async_engine(path: Path) -> AsyncEngine:
    """Create the async engine (``aiosqlite``) for the document tables."""
    engine = create_async_engine(_url(path, driver="aiosqlite"), future=True)
    event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return engine


def make_sync_engine(path: Path) -> Engine:
    """Create the sync engine used for the encrypted api_keys table.

    Key reads happen synchronously (``get_llm_config`` → ``load_config_file`` →
    ``resolve_api_key``), so a sync engine avoids threading async through
    ``llm.py``. It points at the same file as the async engine.
    """
    engine = create_engine(_url(path, driver=""), future=True)
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


def init_models_sync(engine: Engine) -> None:
    """Create all tables (idempotent) using a sync engine connection."""
    Base.metadata.create_all(engine)

    # ``create_all`` does not ALTER existing SQLite tables. Keep this additive
    # migration idempotent so older local databases can load resumes safely.
    with engine.begin() as conn:
        columns = conn.exec_driver_sql("PRAGMA table_info(resumes)").mappings().all()
        if columns and "interview_prep" not in {column["name"] for column in columns}:
            conn.exec_driver_sql("ALTER TABLE resumes ADD COLUMN interview_prep TEXT")

        # Idempotent migration for applications table
        app_cols = conn.exec_driver_sql("PRAGMA table_info(applications)").mappings().all()
        if app_cols:
            existing_app_cols = {column["name"] for column in app_cols}
            if "status_version" not in existing_app_cols:
                conn.exec_driver_sql("ALTER TABLE applications ADD COLUMN status_version INTEGER NOT NULL DEFAULT 0")
            if "lifecycle_token" not in existing_app_cols:
                conn.exec_driver_sql("ALTER TABLE applications ADD COLUMN lifecycle_token TEXT")

            # Backfill/repair NULL, empty, or duplicate lifecycle_tokens
            rows = conn.exec_driver_sql("SELECT application_id, lifecycle_token FROM applications").mappings().all()
            token_counts = {}
            for r in rows:
                tok = r["lifecycle_token"]
                if tok is not None and len(tok.strip()) > 0:
                    token_counts[tok] = token_counts.get(tok, 0) + 1

            processed_tokens = set()
            from uuid import uuid4
            for r in rows:
                app_id = r["application_id"]
                tok = r["lifecycle_token"]

                needs_backfill = False
                if tok is None or len(tok.strip()) == 0:
                    needs_backfill = True
                elif token_counts.get(tok, 0) > 1:
                    if tok in processed_tokens:
                        needs_backfill = True
                    else:
                        processed_tokens.add(tok)
                else:
                    processed_tokens.add(tok)

                if needs_backfill:
                    new_token = str(uuid4())
                    while new_token in processed_tokens:
                        new_token = str(uuid4())
                    conn.exec_driver_sql(
                        "UPDATE applications SET lifecycle_token = ? WHERE application_id = ?",
                        (new_token, app_id)
                    )
                    processed_tokens.add(new_token)

            # Always ensure the unique index exists
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_applications_lifecycle_token ON applications(lifecycle_token)"
            )

        # Create triggers for applications lifecycle token checks
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS applications_lifecycle_required_insert
            BEFORE INSERT ON applications
            FOR EACH ROW
            BEGIN
                SELECT RAISE(FAIL, 'lifecycle_token cannot be NULL or empty')
                WHERE NEW.lifecycle_token IS NULL OR length(trim(NEW.lifecycle_token)) = 0;
            END;
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS applications_lifecycle_required_update
            BEFORE UPDATE ON applications
            FOR EACH ROW
            BEGIN
                SELECT RAISE(FAIL, 'lifecycle_token cannot be NULL or empty')
                WHERE NEW.lifecycle_token IS NULL OR length(trim(NEW.lifecycle_token)) = 0;
            END;
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS applications_lifecycle_immutable
            BEFORE UPDATE ON applications
            FOR EACH ROW
            BEGIN
                SELECT RAISE(FAIL, 'lifecycle_token is immutable')
                WHERE OLD.lifecycle_token IS NOT NULL AND length(trim(OLD.lifecycle_token)) > 0
                  AND NEW.lifecycle_token != OLD.lifecycle_token;
            END;
            """
        )

        # Create append-only triggers on metric_events table idempotent-ly
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS metric_events_no_update
            BEFORE UPDATE ON metric_events
            BEGIN
                SELECT RAISE(FAIL, 'Updates on metric_events are prohibited');
            END;
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS metric_events_no_delete
            BEFORE DELETE ON metric_events
            BEGIN
                SELECT RAISE(FAIL, 'Deletes on metric_events are prohibited');
            END;
            """
        )
