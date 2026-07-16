import pytest
from uuid import uuid4
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, OperationalError, IntegrityError

from app.models import Job
from app.db_engine import init_models_sync


async def test_fresh_job_table_contains_lifecycle_token(isolated_db):
    """1. Verify fresh Job table contains lifecycle_token column."""
    async with isolated_db._session() as session:
        job = Job(
            job_id="job-fresh-1",
            content="JD description",
            resume_id="res-1",
            lifecycle_token="lc-fresh",
        )
        session.add(job)
        await session.commit()

        res = await session.execute(
            select(Job).where(Job.job_id == "job-fresh-1")
        )
        row = res.scalar_one()
        assert row.lifecycle_token == "lc-fresh"


async def test_new_records_receive_token(isolated_db):
    """2. Verify new records receive a default lifecycle_token value."""
    async with isolated_db._session() as session:
        job = Job(
            job_id="job-default-1",
            content="JD",
        )
        session.add(job)
        await session.commit()

        res = await session.execute(
            select(Job).where(Job.job_id == "job-default-1")
        )
        row = res.scalar_one()
        assert row.lifecycle_token is not None
        assert len(row.lifecycle_token) > 0


async def test_two_new_records_have_different_lifecycle_tokens(isolated_db):
    """3. Verify two new records receive different default lifecycle_tokens."""
    async with isolated_db._session() as session:
        job1 = Job(job_id="job-diff-1", content="JD1")
        job2 = Job(job_id="job-diff-2", content="JD2")
        session.add(job1)
        session.add(job2)
        await session.commit()

        row1 = (await session.execute(select(Job).where(Job.job_id == "job-diff-1"))).scalar_one()
        row2 = (await session.execute(select(Job).where(Job.job_id == "job-diff-2"))).scalar_one()
        assert row1.lifecycle_token != row2.lifecycle_token


def test_migration_of_old_table_adds_column(isolated_db):
    """4. Verify migration of older table without lifecycle_token column adds it."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS jobs")
        conn.exec_driver_sql(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                resume_id TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO jobs (job_id, content, resume_id, created_at, metadata_json) "
            "VALUES ('job-1', 'content-1', 'res-1', '2026', '{}')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        cols = conn.exec_driver_sql("PRAGMA table_info(jobs)").mappings().all()
        col_names = {c["name"] for c in cols}
        assert "lifecycle_token" in col_names


def test_migration_existing_data_is_preserved(isolated_db):
    """5. Verify existing job data is preserved during migration."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS jobs")
        conn.exec_driver_sql(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                resume_id TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO jobs (job_id, content, resume_id, created_at, metadata_json) "
            "VALUES ('job-preserve', 'unique-content-abc', 'res-preserve', '2026', '{}')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT content, resume_id FROM jobs WHERE job_id = 'job-preserve'").mappings().one()
        assert row["content"] == "unique-content-abc"
        assert row["resume_id"] == "res-preserve"


def test_migration_null_is_backfilled(isolated_db):
    """6. Verify NULL lifecycle_tokens are backfilled with UUIDs."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS jobs")
        conn.exec_driver_sql(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                resume_id TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO jobs (job_id, content, created_at, metadata_json, lifecycle_token) "
            "VALUES ('job-null', 'JD', '2026', '{}', NULL)"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        tok = conn.exec_driver_sql("SELECT lifecycle_token FROM jobs WHERE job_id = 'job-null'").scalar()
        assert tok is not None
        assert len(tok.strip()) > 0


def test_migration_empty_token_is_backfilled(isolated_db):
    """7. Verify empty lifecycle_tokens are backfilled."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS jobs")
        conn.exec_driver_sql(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                resume_id TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO jobs (job_id, content, created_at, metadata_json, lifecycle_token) "
            "VALUES ('job-empty', 'JD', '2026', '{}', '')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        tok = conn.exec_driver_sql("SELECT lifecycle_token FROM jobs WHERE job_id = 'job-empty'").scalar()
        assert tok is not None
        assert len(tok.strip()) > 0


def test_migration_spaces_token_is_backfilled(isolated_db):
    """8. Verify lifecycle_tokens containing only whitespace are backfilled."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS jobs")
        conn.exec_driver_sql(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                resume_id TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO jobs (job_id, content, created_at, metadata_json, lifecycle_token) "
            "VALUES ('job-spaces', 'JD', '2026', '{}', '   ')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        tok = conn.exec_driver_sql("SELECT lifecycle_token FROM jobs WHERE job_id = 'job-spaces'").scalar()
        assert tok is not None
        assert len(tok.strip()) > 0


def test_migration_duplicates_are_repaired(isolated_db):
    """9. Verify duplicate lifecycle_tokens are repaired (only one remains, others updated)."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS jobs")
        conn.exec_driver_sql(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                resume_id TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO jobs (job_id, content, created_at, metadata_json, lifecycle_token) "
            "VALUES ('job-d1', 'JD1', '2026', '{}', 'duplicate-tok'), "
            "('job-d2', 'JD2', '2026', '{}', 'duplicate-tok')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        toks = conn.exec_driver_sql("SELECT lifecycle_token FROM jobs").scalars().all()
        assert len(toks) == 2
        assert toks[0] != toks[1]
        assert "duplicate-tok" in toks


def test_migration_valid_token_is_preserved(isolated_db):
    """10. Verify a correct, unique lifecycle_token is kept intact."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS jobs")
        conn.exec_driver_sql(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                resume_id TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO jobs (job_id, content, created_at, metadata_json, lifecycle_token) "
            "VALUES ('job-valid', 'JD', '2026', '{}', 'valid-unique-token')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        tok = conn.exec_driver_sql("SELECT lifecycle_token FROM jobs WHERE job_id = 'job-valid'").scalar()
        assert tok == "valid-unique-token"


def test_migration_is_idempotent(isolated_db):
    """11. Verify that migration is idempotent and can run repeatedly without error."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS jobs")
        conn.exec_driver_sql(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                resume_id TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO jobs (job_id, content, created_at, metadata_json, lifecycle_token) "
            "VALUES ('job-idem', 'JD', '2026', '{}', 'valid-idem')"
        )

    init_models_sync(engine)
    init_models_sync(engine)

    with engine.connect() as conn:
        tok = conn.exec_driver_sql("SELECT lifecycle_token FROM jobs WHERE job_id = 'job-idem'").scalar()
        assert tok == "valid-idem"


def test_migration_creates_missing_unique_index(isolated_db):
    """12. Verify missing unique index uq_jobs_lifecycle_token is created."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS jobs")
        conn.exec_driver_sql(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                resume_id TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO jobs (job_id, content, created_at, metadata_json, lifecycle_token) "
            "VALUES ('job-idx', 'JD', '2026', '{}', 'tok')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        indexes = conn.exec_driver_sql("PRAGMA index_list(jobs)").mappings().all()
        target_idx = next((i for i in indexes if i["name"] == "uq_jobs_lifecycle_token"), None)
        assert target_idx is not None


def test_index_is_unique_on_lifecycle_token(isolated_db):
    """13. Verify unique index uq_jobs_lifecycle_token is unique and targets lifecycle_token column."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS jobs")
        conn.exec_driver_sql(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                resume_id TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO jobs (job_id, content, created_at, metadata_json, lifecycle_token) "
            "VALUES ('job-idx', 'JD', '2026', '{}', 'tok')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        indexes = conn.exec_driver_sql("PRAGMA index_list(jobs)").mappings().all()
        target_idx = next((i for i in indexes if i["name"] == "uq_jobs_lifecycle_token"), None)
        assert target_idx is not None
        assert target_idx["unique"] == 1

        info = conn.exec_driver_sql("PRAGMA index_info(uq_jobs_lifecycle_token)").mappings().all()
        assert len(info) == 1
        assert info[0]["name"] == "lifecycle_token"


async def test_insert_null_token_is_blocked(isolated_db):
    """14. Verify triggers block INSERT of a NULL lifecycle_token."""
    async with isolated_db._session() as session:
        with pytest.raises((OperationalError, DBAPIError)):
            await session.execute(
                text(
                    "INSERT INTO jobs (job_id, content, created_at, metadata_json, lifecycle_token) "
                    "VALUES ('job-fail-null', 'JD', '2026', '{}', NULL)"
                )
            )
            await session.commit()


async def test_insert_empty_token_is_blocked(isolated_db):
    """15. Verify triggers block INSERT of an empty/whitespace lifecycle_token."""
    async with isolated_db._session() as session:
        with pytest.raises((OperationalError, DBAPIError)):
            await session.execute(
                text(
                    "INSERT INTO jobs (job_id, content, created_at, metadata_json, lifecycle_token) "
                    "VALUES ('job-fail-empty', 'JD', '2026', '{}', '   ')"
                )
            )
            await session.commit()


async def test_update_empty_token_is_blocked(isolated_db):
    """16. Verify triggers block UPDATE setting lifecycle_token to an empty value."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-upd-fail", content="JD", lifecycle_token="initial-token")
        session.add(job)
        await session.commit()

    async with isolated_db._session() as session:
        with pytest.raises((OperationalError, DBAPIError)):
            await session.execute(
                text("UPDATE jobs SET lifecycle_token = '' WHERE job_id = 'job-upd-fail'")
            )
            await session.commit()


async def test_update_immutable_token_is_blocked(isolated_db):
    """17. Verify triggers block UPDATE changing a valid non-empty lifecycle_token to another value."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-immutable", content="JD", lifecycle_token="token-initial")
        session.add(job)
        await session.commit()

    async with isolated_db._session() as session:
        with pytest.raises((OperationalError, DBAPIError)):
            await session.execute(
                text("UPDATE jobs SET lifecycle_token = 'token-new' WHERE job_id = 'job-immutable'")
            )
            await session.commit()
