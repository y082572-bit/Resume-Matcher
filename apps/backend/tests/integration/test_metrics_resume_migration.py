import pytest
from uuid import uuid4
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, OperationalError, IntegrityError

from app.models import Resume
from app.db_engine import init_models_sync


async def test_fresh_resume_table_contains_lifecycle_token(isolated_db):
    """1. Verify fresh Resume table contains lifecycle_token column."""
    async with isolated_db._session() as session:
        resume = Resume(
            resume_id="res-fresh-1",
            content="Resume content",
            content_type="md",
            lifecycle_token="lc-fresh",
        )
        session.add(resume)
        await session.commit()

        res = await session.execute(
            select(Resume).where(Resume.resume_id == "res-fresh-1")
        )
        row = res.scalar_one()
        assert row.lifecycle_token == "lc-fresh"


async def test_new_records_receive_token(isolated_db):
    """2. Verify new records receive a default lifecycle_token value."""
    # Note: create_resume does the token assignment in database.py
    doc = await isolated_db.create_resume(content="Resume")
    resume_id = doc["resume_id"]

    async with isolated_db._session() as session:
        res = await session.execute(
            select(Resume).where(Resume.resume_id == resume_id)
        )
        row = res.scalar_one()
        assert row.lifecycle_token is not None
        assert len(row.lifecycle_token) > 0


async def test_two_new_records_have_different_lifecycle_tokens(isolated_db):
    """3. Verify two new records receive different default lifecycle_tokens."""
    doc1 = await isolated_db.create_resume(content="R1")
    doc2 = await isolated_db.create_resume(content="R2")

    async with isolated_db._session() as session:
        row1 = (await session.execute(select(Resume).where(Resume.resume_id == doc1["resume_id"]))).scalar_one()
        row2 = (await session.execute(select(Resume).where(Resume.resume_id == doc2["resume_id"]))).scalar_one()
        assert row1.lifecycle_token != row2.lifecycle_token


def test_migration_of_old_table_adds_column(isolated_db):
    """4. Verify migration of older table without lifecycle_token column adds it."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS resumes")
        conn.exec_driver_sql(
            """
            CREATE TABLE resumes (
                resume_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL,
                filename TEXT,
                is_master INTEGER NOT NULL,
                parent_id TEXT,
                processed_data TEXT,
                processing_status TEXT NOT NULL,
                cover_letter TEXT,
                outreach_message TEXT,
                interview_prep TEXT,
                title TEXT,
                original_markdown TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO resumes (resume_id, content, content_type, is_master, processing_status, created_at, updated_at) "
            "VALUES ('res-1', 'content-1', 'md', 0, 'completed', '2026', '2026')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        cols = conn.exec_driver_sql("PRAGMA table_info(resumes)").mappings().all()
        col_names = {c["name"] for c in cols}
        assert "lifecycle_token" in col_names


def test_migration_existing_data_is_preserved(isolated_db):
    """5. Verify existing resume data is preserved during migration."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS resumes")
        conn.exec_driver_sql(
            """
            CREATE TABLE resumes (
                resume_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL,
                filename TEXT,
                is_master INTEGER NOT NULL,
                parent_id TEXT,
                processed_data TEXT,
                processing_status TEXT NOT NULL,
                cover_letter TEXT,
                outreach_message TEXT,
                interview_prep TEXT,
                title TEXT,
                original_markdown TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO resumes (resume_id, content, content_type, is_master, processing_status, created_at, updated_at) "
            "VALUES ('res-preserve', 'unique-content-abc', 'md', 1, 'ready', '2026', '2026')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT content, is_master FROM resumes WHERE resume_id = 'res-preserve'").mappings().one()
        assert row["content"] == "unique-content-abc"
        assert row["is_master"] == 1


def test_migration_null_is_backfilled(isolated_db):
    """6. Verify NULL lifecycle_tokens are backfilled with UUIDs."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS resumes")
        conn.exec_driver_sql(
            """
            CREATE TABLE resumes (
                resume_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL,
                is_master INTEGER NOT NULL,
                processing_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO resumes (resume_id, content, content_type, is_master, processing_status, created_at, updated_at, lifecycle_token) "
            "VALUES ('res-null', 'content', 'md', 0, 'ready', '2026', '2026', NULL)"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        tok = conn.exec_driver_sql("SELECT lifecycle_token FROM resumes WHERE resume_id = 'res-null'").scalar()
        assert tok is not None
        assert len(tok.strip()) > 0


def test_migration_empty_token_is_backfilled(isolated_db):
    """7. Verify empty lifecycle_tokens are backfilled."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS resumes")
        conn.exec_driver_sql(
            """
            CREATE TABLE resumes (
                resume_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL,
                is_master INTEGER NOT NULL,
                processing_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO resumes (resume_id, content, content_type, is_master, processing_status, created_at, updated_at, lifecycle_token) "
            "VALUES ('res-empty', 'content', 'md', 0, 'ready', '2026', '2026', '')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        tok = conn.exec_driver_sql("SELECT lifecycle_token FROM resumes WHERE resume_id = 'res-empty'").scalar()
        assert tok is not None
        assert len(tok.strip()) > 0


def test_migration_spaces_token_is_backfilled(isolated_db):
    """8. Verify lifecycle_tokens containing only whitespace are backfilled."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS resumes")
        conn.exec_driver_sql(
            """
            CREATE TABLE resumes (
                resume_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL,
                is_master INTEGER NOT NULL,
                processing_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO resumes (resume_id, content, content_type, is_master, processing_status, created_at, updated_at, lifecycle_token) "
            "VALUES ('res-spaces', 'content', 'md', 0, 'ready', '2026', '2026', '   ')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        tok = conn.exec_driver_sql("SELECT lifecycle_token FROM resumes WHERE resume_id = 'res-spaces'").scalar()
        assert tok is not None
        assert len(tok.strip()) > 0


def test_migration_duplicates_are_repaired(isolated_db):
    """9. Verify duplicate lifecycle_tokens are repaired (only one remains, others updated)."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS resumes")
        conn.exec_driver_sql(
            """
            CREATE TABLE resumes (
                resume_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL,
                is_master INTEGER NOT NULL,
                processing_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO resumes (resume_id, content, content_type, is_master, processing_status, created_at, updated_at, lifecycle_token) "
            "VALUES ('res-d1', 'content', 'md', 0, 'ready', '2026', '2026', 'duplicate-tok'), "
            "('res-d2', 'content', 'md', 0, 'ready', '2026', '2026', 'duplicate-tok')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        toks = conn.exec_driver_sql("SELECT lifecycle_token FROM resumes").scalars().all()
        assert len(toks) == 2
        assert toks[0] != toks[1]
        assert "duplicate-tok" in toks


def test_migration_valid_token_is_preserved(isolated_db):
    """10. Verify a correct, unique lifecycle_token is kept intact."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS resumes")
        conn.exec_driver_sql(
            """
            CREATE TABLE resumes (
                resume_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL,
                is_master INTEGER NOT NULL,
                processing_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO resumes (resume_id, content, content_type, is_master, processing_status, created_at, updated_at, lifecycle_token) "
            "VALUES ('res-valid', 'content', 'md', 0, 'ready', '2026', '2026', 'valid-unique-token')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        tok = conn.exec_driver_sql("SELECT lifecycle_token FROM resumes WHERE resume_id = 'res-valid'").scalar()
        assert tok == "valid-unique-token"


def test_migration_is_idempotent(isolated_db):
    """11. Verify that migration is idempotent and can run repeatedly without error."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS resumes")
        conn.exec_driver_sql(
            """
            CREATE TABLE resumes (
                resume_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL,
                is_master INTEGER NOT NULL,
                processing_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO resumes (resume_id, content, content_type, is_master, processing_status, created_at, updated_at, lifecycle_token) "
            "VALUES ('res-idem', 'content', 'md', 0, 'ready', '2026', '2026', 'valid-idem')"
        )

    init_models_sync(engine)
    init_models_sync(engine)

    with engine.connect() as conn:
        tok = conn.exec_driver_sql("SELECT lifecycle_token FROM resumes WHERE resume_id = 'res-idem'").scalar()
        assert tok == "valid-idem"


def test_migration_creates_missing_unique_index(isolated_db):
    """12. Verify missing unique index uq_resumes_lifecycle_token is created."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS resumes")
        conn.exec_driver_sql(
            """
            CREATE TABLE resumes (
                resume_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL,
                is_master INTEGER NOT NULL,
                processing_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO resumes (resume_id, content, content_type, is_master, processing_status, created_at, updated_at, lifecycle_token) "
            "VALUES ('res-idx', 'content', 'md', 0, 'ready', '2026', '2026', 'tok')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        indexes = conn.exec_driver_sql("PRAGMA index_list(resumes)").mappings().all()
        target_idx = next((i for i in indexes if i["name"] == "uq_resumes_lifecycle_token"), None)
        assert target_idx is not None


def test_index_is_unique_on_lifecycle_token(isolated_db):
    """13. Verify unique index uq_resumes_lifecycle_token is unique and targets lifecycle_token column."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS resumes")
        conn.exec_driver_sql(
            """
            CREATE TABLE resumes (
                resume_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL,
                is_master INTEGER NOT NULL,
                processing_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                lifecycle_token TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO resumes (resume_id, content, content_type, is_master, processing_status, created_at, updated_at, lifecycle_token) "
            "VALUES ('res-idx', 'content', 'md', 0, 'ready', '2026', '2026', 'tok')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        indexes = conn.exec_driver_sql("PRAGMA index_list(resumes)").mappings().all()
        target_idx = next((i for i in indexes if i["name"] == "uq_resumes_lifecycle_token"), None)
        assert target_idx is not None
        assert target_idx["unique"] == 1

        info = conn.exec_driver_sql("PRAGMA index_info(uq_resumes_lifecycle_token)").mappings().all()
        assert len(info) == 1
        assert info[0]["name"] == "lifecycle_token"


async def test_insert_null_token_is_blocked(isolated_db):
    """14. Verify triggers block INSERT of a NULL lifecycle_token."""
    async with isolated_db._session() as session:
        with pytest.raises((OperationalError, DBAPIError)):
            await session.execute(
                text(
                    "INSERT INTO resumes (resume_id, content, content_type, is_master, processing_status, created_at, updated_at, lifecycle_token) "
                    "VALUES ('res-fail-null', 'content', 'md', 0, 'ready', '2026', '2026', NULL)"
                )
            )
            await session.commit()


async def test_insert_empty_token_is_blocked(isolated_db):
    """15. Verify triggers block INSERT of an empty/whitespace lifecycle_token."""
    async with isolated_db._session() as session:
        with pytest.raises((OperationalError, DBAPIError)):
            await session.execute(
                text(
                    "INSERT INTO resumes (resume_id, content, content_type, is_master, processing_status, created_at, updated_at, lifecycle_token) "
                    "VALUES ('res-fail-empty', 'content', 'md', 0, 'ready', '2026', '2026', '   ')"
                )
            )
            await session.commit()


async def test_update_empty_token_is_blocked(isolated_db):
    """16. Verify triggers block UPDATE setting lifecycle_token to an empty value."""
    async with isolated_db._session() as session:
        resume = Resume(resume_id="res-upd-fail", content="content", content_type="md", lifecycle_token="initial-token")
        session.add(resume)
        await session.commit()

    async with isolated_db._session() as session:
        with pytest.raises((OperationalError, DBAPIError)):
            await session.execute(
                text("UPDATE resumes SET lifecycle_token = '' WHERE resume_id = 'res-upd-fail'")
            )
            await session.commit()


async def test_update_immutable_token_is_blocked(isolated_db):
    """17. Verify triggers block UPDATE changing a valid non-empty lifecycle_token to another value."""
    async with isolated_db._session() as session:
        resume = Resume(resume_id="res-immutable", content="content", content_type="md", lifecycle_token="token-initial")
        session.add(resume)
        await session.commit()

    async with isolated_db._session() as session:
        with pytest.raises((OperationalError, DBAPIError)):
            await session.execute(
                text("UPDATE resumes SET lifecycle_token = 'token-new' WHERE resume_id = 'res-immutable'")
            )
            await session.commit()


def test_migration_does_not_modify_parent_id(isolated_db):
    """18. Verify migration does not change parent_id values."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS resumes")
        conn.exec_driver_sql(
            """
            CREATE TABLE resumes (
                resume_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL,
                is_master INTEGER NOT NULL,
                processing_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                parent_id TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO resumes (resume_id, content, content_type, is_master, processing_status, created_at, updated_at, parent_id) "
            "VALUES ('res-parent', 'content', 'md', 0, 'ready', '2026', '2026', 'parent-abc')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        val = conn.exec_driver_sql("SELECT parent_id FROM resumes WHERE resume_id = 'res-parent'").scalar()
        assert val == "parent-abc"


def test_migration_does_not_modify_is_master(isolated_db):
    """19. Verify migration does not change is_master values."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS resumes")
        conn.exec_driver_sql(
            """
            CREATE TABLE resumes (
                resume_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL,
                is_master INTEGER NOT NULL,
                processing_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO resumes (resume_id, content, content_type, is_master, processing_status, created_at, updated_at) "
            "VALUES ('res-master-test', 'content', 'md', 1, 'ready', '2026', '2026')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        val = conn.exec_driver_sql("SELECT is_master FROM resumes WHERE resume_id = 'res-master-test'").scalar()
        assert val == 1


def test_migration_does_not_modify_content_or_status(isolated_db):
    """20. Verify migration does not change content or processing_status."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS resumes")
        conn.exec_driver_sql(
            """
            CREATE TABLE resumes (
                resume_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL,
                is_master INTEGER NOT NULL,
                processing_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO resumes (resume_id, content, content_type, is_master, processing_status, created_at, updated_at) "
            "VALUES ('res-attr', 'xyz-content-123', 'md', 0, 'status-abc', '2026', '2026')"
        )

    init_models_sync(engine)

    with engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT content, processing_status FROM resumes WHERE resume_id = 'res-attr'").mappings().one()
        assert row["content"] == "xyz-content-123"
        assert row["processing_status"] == "status-abc"


async def test_direct_orm_resume_receives_lifecycle_token(isolated_db):
    """Verify that a Resume created directly via ORM without lifecycle_token receives a non-empty UUID."""
    async with isolated_db._session() as session:
        resume = Resume(
            resume_id="res-direct-orm-1",
            content="Resume content",
            content_type="md",
        )
        session.add(resume)
        await session.commit()

    async with isolated_db._session() as session:
        row = (await session.execute(
            select(Resume).where(Resume.resume_id == "res-direct-orm-1")
        )).scalar_one()
        assert row.lifecycle_token is not None
        assert len(row.lifecycle_token.strip()) > 0
        from uuid import UUID
        try:
            UUID(row.lifecycle_token)
        except ValueError:
            pytest.fail("lifecycle_token is not a valid UUID string")


async def test_two_direct_orm_resumes_receive_different_lifecycle_tokens(isolated_db):
    """Verify that two Resumes created directly via ORM receive different default lifecycle_tokens."""
    async with isolated_db._session() as session:
        r1 = Resume(resume_id="res-direct-orm-diff-1", content="R1", content_type="md")
        r2 = Resume(resume_id="res-direct-orm-diff-2", content="R2", content_type="md")
        session.add(r1)
        session.add(r2)
        await session.commit()

    async with isolated_db._session() as session:
        row1 = (await session.execute(select(Resume).where(Resume.resume_id == "res-direct-orm-diff-1"))).scalar_one()
        row2 = (await session.execute(select(Resume).where(Resume.resume_id == "res-direct-orm-diff-2"))).scalar_one()
        assert row1.lifecycle_token != row2.lifecycle_token
