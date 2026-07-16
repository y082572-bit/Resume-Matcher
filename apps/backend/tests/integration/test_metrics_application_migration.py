import pytest
from uuid import uuid4
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, OperationalError, IntegrityError

from app.models import Application
from app.db_engine import init_models_sync


async def test_fresh_application_table_contains_columns(isolated_db):
    """1 & 2. Verify fresh Application table contains status_version and lifecycle_token."""
    async with isolated_db._session() as session:
        # Check that we can query these fields directly
        app = Application(
            application_id="app-fresh-1",
            job_id="job-1",
            resume_id="res-1",
            status="saved",
        )
        session.add(app)
        await session.commit()

        # Retrieve and check
        res = await session.execute(
            select(Application).where(Application.application_id == "app-fresh-1")
        )
        row = res.scalar_one()
        assert row.status_version == 0
        assert row.lifecycle_token is not None
        assert len(row.lifecycle_token) > 0


async def test_status_version_initial_value_0(isolated_db):
    """3. Verify status_version initial value is 0."""
    async with isolated_db._session() as session:
        app = Application(
            application_id="app-version-0",
            job_id="job-1",
            resume_id="res-2",
            status="applied",
        )
        session.add(app)
        await session.commit()

        res = await session.execute(
            select(Application).where(Application.application_id == "app-version-0")
        )
        row = res.scalar_one()
        assert row.status_version == 0


async def test_two_new_records_have_different_lifecycle_tokens(isolated_db):
    """4. Verify two new records receive different lifecycle_token values."""
    async with isolated_db._session() as session:
        app1 = Application(
            application_id="app-diff-1",
            job_id="job-1",
            resume_id="res-3",
            status="applied",
        )
        app2 = Application(
            application_id="app-diff-2",
            job_id="job-1",
            resume_id="res-4",
            status="applied",
        )
        session.add(app1)
        session.add(app2)
        await session.commit()

        row1 = (await session.execute(select(Application).where(Application.application_id == "app-diff-1"))).scalar_one()
        row2 = (await session.execute(select(Application).where(Application.application_id == "app-diff-2"))).scalar_one()

        assert row1.lifecycle_token != row2.lifecycle_token


def test_migration_of_old_table_adds_columns_and_keeps_data(isolated_db):
    """5, 6, 7, 8, 9 & 10. Verify additive migration on older tables without columns."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    # 1. Drop existing applications table to simulate old state
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS applications")
        # Recreate old applications table without status_version and lifecycle_token
        conn.exec_driver_sql(
            """
            CREATE TABLE applications (
                application_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                resume_id TEXT NOT NULL,
                status TEXT NOT NULL,
                company TEXT,
                role TEXT,
                applied_at TEXT,
                notes TEXT,
                position INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        # Insert test data into the older schema
        conn.exec_driver_sql(
            """
            INSERT INTO applications (application_id, job_id, resume_id, status, position, created_at, updated_at)
            VALUES ('old-1', 'job-1', 'res-1', 'applied', 0, '2026-07-16', '2026-07-16'),
                   ('old-2', 'job-2', 'res-2', 'saved', 0, '2026-07-16', '2026-07-16')
            """
        )

    # 2. Run additive migration via init_models_sync
    init_models_sync(engine)

    # 3. Check migration results
    with engine.connect() as conn:
        # Check table columns
        cols = conn.exec_driver_sql("PRAGMA table_info(applications)").mappings().all()
        col_names = {c["name"] for c in cols}
        assert "status_version" in col_names
        assert "lifecycle_token" in col_names

        # Check that data was preserved
        rows = conn.exec_driver_sql("SELECT * FROM applications ORDER BY application_id").mappings().all()
        assert len(rows) == 2
        assert rows[0]["application_id"] == "old-1"
        assert rows[0]["status"] == "applied"
        assert rows[0]["status_version"] == 0
        assert rows[0]["lifecycle_token"] is not None
        assert len(rows[0]["lifecycle_token"]) > 0

        assert rows[1]["application_id"] == "old-2"
        assert rows[1]["status"] == "saved"
        assert rows[1]["status_version"] == 0
        assert rows[1]["lifecycle_token"] is not None
        assert len(rows[1]["lifecycle_token"]) > 0

        # 8. Check that tokens are unique
        assert rows[0]["lifecycle_token"] != rows[1]["lifecycle_token"]

        # 10. Check unique index exists
        indexes = conn.exec_driver_sql("PRAGMA index_list(applications)").mappings().all()
        token_index_exists = any(
            idx["origin"] == "u" or "lifecycle_token" in idx["name"]
            for idx in indexes
        )
        assert token_index_exists

    # 9. Verify running migration again is idempotent (does not fail or alter existing tokens)
    with engine.connect() as conn:
        token_before = conn.exec_driver_sql("SELECT lifecycle_token FROM applications WHERE application_id = 'old-1'").scalar()

    init_models_sync(engine)

    with engine.connect() as conn:
        token_after = conn.exec_driver_sql("SELECT lifecycle_token FROM applications WHERE application_id = 'old-1'").scalar()
        assert token_before == token_after


async def test_direct_insert_with_lifecycle_token_null_is_blocked(isolated_db):
    """11. Verify triggers block direct INSERT with NULL lifecycle_token."""
    async with isolated_db._session() as session:
        # Avoid ORM verification by executing raw SQL INSERT
        with pytest.raises((OperationalError, DBAPIError)):
            await session.execute(
                text(
                    """
                    INSERT INTO applications (application_id, job_id, resume_id, status, position, status_version, lifecycle_token, created_at, updated_at)
                    VALUES ('direct-null', 'job-1', 'res-1', 'saved', 0, 0, NULL, '2026-07-16', '2026-07-16')
                    """
                )
            )
            await session.commit()


async def test_direct_insert_with_lifecycle_token_empty_is_blocked(isolated_db):
    """12. Verify triggers block direct INSERT with empty or whitespace lifecycle_token."""
    async with isolated_db._session() as session:
        # Empty string
        with pytest.raises((OperationalError, DBAPIError)):
            await session.execute(
                text(
                    """
                    INSERT INTO applications (application_id, job_id, resume_id, status, position, status_version, lifecycle_token, created_at, updated_at)
                    VALUES ('direct-empty', 'job-1', 'res-1', 'saved', 0, 0, '', '2026-07-16', '2026-07-16')
                    """
                )
            )
            await session.commit()

        # Whitespace string
        with pytest.raises((OperationalError, DBAPIError)):
            await session.execute(
                text(
                    """
                    INSERT INTO applications (application_id, job_id, resume_id, status, position, status_version, lifecycle_token, created_at, updated_at)
                    VALUES ('direct-space', 'job-1', 'res-1', 'saved', 0, 0, '   ', '2026-07-16', '2026-07-16')
                    """
                )
            )
            await session.commit()


async def test_direct_update_setting_empty_token_is_blocked(isolated_db):
    """13. Verify triggers block direct UPDATE with empty lifecycle_token."""
    async with isolated_db._session() as session:
        # Create normal application
        app = Application(
            application_id="app-normal",
            job_id="job-1",
            resume_id="res-1",
            status="saved",
        )
        session.add(app)
        await session.commit()

    async with isolated_db._session() as session:
        with pytest.raises((OperationalError, DBAPIError)):
            await session.execute(
                text("UPDATE applications SET lifecycle_token = '' WHERE application_id = 'app-normal'")
            )
            await session.commit()


async def test_duplicate_lifecycle_token_is_blocked(isolated_db):
    """14. Verify unique constraint / index blocks duplicate lifecycle_token."""
    token = str(uuid4())
    async with isolated_db._session() as session:
        # Insert first
        await session.execute(
            text(
                f"""
                INSERT INTO applications (application_id, job_id, resume_id, status, position, status_version, lifecycle_token, created_at, updated_at)
                VALUES ('app-1', 'job-1', 'res-1', 'saved', 0, 0, '{token}', '2026', '2026')
                """
            )
        )
        await session.commit()

    async with isolated_db._session() as session:
        # Insert duplicate token
        with pytest.raises((IntegrityError, OperationalError, DBAPIError)):
            await session.execute(
                text(
                    f"""
                    INSERT INTO applications (application_id, job_id, resume_id, status, position, status_version, lifecycle_token, created_at, updated_at)
                    VALUES ('app-2', 'job-1', 'res-2', 'saved', 0, 0, '{token}', '2026', '2026')
                    """
                )
            )
            await session.commit()


async def test_partial_migration_backfills_existing_null_tokens(isolated_db):
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS applications")
        conn.exec_driver_sql(
            """
            CREATE TABLE applications (
                application_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                resume_id TEXT NOT NULL,
                status TEXT NOT NULL,
                position INTEGER NOT NULL,
                lifecycle_token TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO applications (application_id, job_id, resume_id, status, position, lifecycle_token, created_at, updated_at) "
            "VALUES ('app-null', 'j', 'r', 'saved', 0, NULL, '2026', '2026')"
        )
    init_models_sync(engine)
    with engine.connect() as conn:
        token = conn.exec_driver_sql("SELECT lifecycle_token FROM applications WHERE application_id = 'app-null'").scalar()
        assert token is not None
        assert len(token.strip()) > 0


async def test_partial_migration_backfills_existing_empty_tokens(isolated_db):
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS applications")
        conn.exec_driver_sql(
            """
            CREATE TABLE applications (
                application_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                resume_id TEXT NOT NULL,
                status TEXT NOT NULL,
                position INTEGER NOT NULL,
                lifecycle_token TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO applications (application_id, job_id, resume_id, status, position, lifecycle_token, created_at, updated_at) "
            "VALUES ('app-empty-1', 'j', 'r1', 'saved', 0, '', '2026', '2026'), "
            "       ('app-empty-2', 'j', 'r2', 'saved', 0, '   ', '2026', '2026')"
        )
    init_models_sync(engine)
    with engine.connect() as conn:
        t1 = conn.exec_driver_sql("SELECT lifecycle_token FROM applications WHERE application_id = 'app-empty-1'").scalar()
        t2 = conn.exec_driver_sql("SELECT lifecycle_token FROM applications WHERE application_id = 'app-empty-2'").scalar()
        assert t1 is not None and len(t1.strip()) > 0
        assert t2 is not None and len(t2.strip()) > 0
        assert t1 != t2


async def test_partial_migration_repairs_duplicate_tokens(isolated_db):
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine
    token = "dup-token-123"
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS applications")
        conn.exec_driver_sql(
            """
            CREATE TABLE applications (
                application_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                resume_id TEXT NOT NULL,
                status TEXT NOT NULL,
                position INTEGER NOT NULL,
                lifecycle_token TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO applications (application_id, job_id, resume_id, status, position, lifecycle_token, created_at, updated_at) "
            f"VALUES ('app-dup-1', 'j', 'r1', 'saved', 0, '{token}', '2026', '2026'), "
            f"       ('app-dup-2', 'j', 'r2', 'saved', 0, '{token}', '2026', '2026')"
        )
    init_models_sync(engine)
    with engine.connect() as conn:
        t1 = conn.exec_driver_sql("SELECT lifecycle_token FROM applications WHERE application_id = 'app-dup-1'").scalar()
        t2 = conn.exec_driver_sql("SELECT lifecycle_token FROM applications WHERE application_id = 'app-dup-2'").scalar()
        assert t1 is not None and len(t1.strip()) > 0
        assert t2 is not None and len(t2.strip()) > 0
        assert t1 != t2
        assert (t1 == token and t2 != token) or (t2 == token and t1 != token)


async def test_partial_migration_creates_missing_unique_index(isolated_db):
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS applications")
        conn.exec_driver_sql(
            """
            CREATE TABLE applications (
                application_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                resume_id TEXT NOT NULL,
                status TEXT NOT NULL,
                position INTEGER NOT NULL,
                lifecycle_token TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO applications (application_id, job_id, resume_id, status, position, lifecycle_token, created_at, updated_at) "
            "VALUES ('app-idx', 'j', 'r', 'saved', 0, 'tok', '2026', '2026')"
        )
    init_models_sync(engine)

    with engine.connect() as conn:
        indexes = conn.exec_driver_sql("PRAGMA index_list(applications)").mappings().all()
        target_idx = None
        for idx in indexes:
            if idx["name"] == "uq_applications_lifecycle_token":
                target_idx = idx
                break
        assert target_idx is not None, "Index uq_applications_lifecycle_token does not exist"
        assert target_idx["unique"] == 1, "Index must be unique"

        info = conn.exec_driver_sql("PRAGMA index_info(uq_applications_lifecycle_token)").mappings().all()
        assert len(info) == 1
        assert info[0]["name"] == "lifecycle_token"


async def test_partial_migration_preserves_valid_existing_tokens(isolated_db):
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine
    t1 = "valid-token-1"
    t2 = "valid-token-2"
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS applications")
        conn.exec_driver_sql(
            """
            CREATE TABLE applications (
                application_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                resume_id TEXT NOT NULL,
                status TEXT NOT NULL,
                position INTEGER NOT NULL,
                lifecycle_token TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO applications (application_id, job_id, resume_id, status, position, lifecycle_token, created_at, updated_at) "
            f"VALUES ('app-val-1', 'j', 'r1', 'saved', 0, '{t1}', '2026', '2026'), "
            f"       ('app-val-2', 'j', 'r2', 'saved', 0, '{t2}', '2026', '2026')"
        )
    init_models_sync(engine)
    with engine.connect() as conn:
        r1 = conn.exec_driver_sql("SELECT lifecycle_token FROM applications WHERE application_id = 'app-val-1'").scalar()
        r2 = conn.exec_driver_sql("SELECT lifecycle_token FROM applications WHERE application_id = 'app-val-2'").scalar()
        assert r1 == t1
        assert r2 == t2


async def test_partial_migration_second_run_is_idempotent(isolated_db):
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine
    init_models_sync(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO applications (application_id, job_id, resume_id, status, position, status_version, lifecycle_token, created_at, updated_at) "
            "VALUES ('app-idem', 'j', 'r', 'saved', 0, 0, 'tok-idem', '2026', '2026')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        tok = conn.exec_driver_sql("SELECT lifecycle_token FROM applications WHERE application_id = 'app-idem'").scalar()
        assert tok == "tok-idem"


async def test_direct_update_changing_valid_lifecycle_token_is_blocked(isolated_db):
    """Verify triggers block updating a valid lifecycle token to a different value."""
    async with isolated_db._session() as session:
        app = Application(
            application_id="app-mut",
            job_id="job-1",
            resume_id="res-1",
            status="saved",
            lifecycle_token="token-initial"
        )
        session.add(app)
        await session.commit()

    async with isolated_db._session() as session:
        with pytest.raises((OperationalError, DBAPIError)):
            await session.execute(
                text("UPDATE applications SET lifecycle_token = 'token-new' WHERE application_id = 'app-mut'")
            )
            await session.commit()


async def test_regular_application_update_preserves_lifecycle_token(isolated_db):
    """Verify regular updates (like company/role) succeed when token is unchanged."""
    async with isolated_db._session() as session:
        app = Application(
            application_id="app-reg",
            job_id="job-1",
            resume_id="res-1",
            status="saved",
            lifecycle_token="token-const",
            company="OldCompany"
        )
        session.add(app)
        await session.commit()

    await isolated_db.update_application("app-reg", {"company": "NewCompany"})

    async with isolated_db._session() as session:
        row = (await session.execute(select(Application).where(Application.application_id == "app-reg"))).scalar_one()
        assert row.company == "NewCompany"
        assert row.lifecycle_token == "token-const"


async def test_status_update_preserves_lifecycle_token(isolated_db):
    """Verify status update succeeds and preserves lifecycle token."""
    async with isolated_db._session() as session:
        app = Application(
            application_id="app-stat",
            job_id="job-1",
            resume_id="res-1",
            status="saved",
            lifecycle_token="token-stat"
        )
        session.add(app)
        await session.commit()

    await isolated_db.update_application("app-stat", {"status": "applied"})

    async with isolated_db._session() as session:
        row = (await session.execute(select(Application).where(Application.application_id == "app-stat"))).scalar_one()
        assert row.status == "applied"
        assert row.lifecycle_token == "token-stat"
