"""Phase 6a acceptance on a throwaway Postgres database.

The dev warehouse is never touched: this module creates `dqs_publish_test`,
initializes the dq tables, and drops it in `finally`. It proves the real
`dq.publications` row (mode=dry_run, file URL) and DB-backed idempotency for
both dry-run and live publishing (live uses the in-memory fake client).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from tests.helpers.fake_github import FakeGitHubClient
from tests.helpers.publish import (
    INCIDENT_ID,
    make_approval,
    make_incident,
    make_plan,
    make_verification,
)

from dqs import db as dbmod
from dqs.config import Settings
from dqs.publish.dry_run import preview_paths, run_dry_run
from dqs.publish.github import GitHubPublisher
from dqs.publish.store import DbPublicationStore

pytestmark = pytest.mark.integration

TEST_DB = "dqs_publish_test"


@pytest.fixture
def publish_engine(settings: Settings) -> Iterator[Engine]:
    # CREATE/DROP DATABASE must run outside a transaction and on the
    # maintenance database, not the warehouse the fixture is about to drop.
    admin = create_engine(
        settings.rw_database_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
            conn.exec_driver_sql(f'CREATE DATABASE "{TEST_DB}"')
        test_settings = settings.model_copy(update={"pg_database": TEST_DB})
        dbmod.init_database(test_settings)
        engine = dbmod.make_rw_engine(test_settings)
        try:
            yield engine
        finally:
            engine.dispose()
            with admin.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
    finally:
        admin.dispose()


def test_dry_run_writes_a_preview_and_records_one_publication(
    publish_engine: Engine, tmp_path: Path
) -> None:
    store = DbPublicationStore(publish_engine)
    first = run_dry_run(
        make_plan(),
        make_incident(),
        make_verification(),
        store=store,
        output_dir=tmp_path,
    )

    md_path, patch_path = preview_paths(INCIDENT_ID, output_dir=tmp_path)
    assert md_path.is_file() and patch_path.is_file()
    assert "## Verification" in md_path.read_text(encoding="utf-8")
    assert "coalesce(customer_id::integer, cust_id::integer)" in patch_path.read_text(
        encoding="utf-8"
    )
    assert first.url.startswith("file:///")

    with publish_engine.connect() as conn:
        rows = conn.execute(text("SELECT incident_id, kind, mode, url FROM dq.publications")).all()
    assert len(rows) == 1
    assert rows[0][0] == INCIDENT_ID
    assert (rows[0][1], rows[0][2]) == ("pr", "dry_run")
    assert rows[0][3].startswith("file:///")

    body_before = md_path.read_bytes()
    second = run_dry_run(
        make_plan(),
        make_incident(),
        make_verification(),
        store=store,
        output_dir=tmp_path,
    )

    assert second.reused
    assert second.url == first.url
    with publish_engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM dq.publications")).scalar_one()
    assert count == 1
    assert md_path.read_bytes() == body_before


def test_live_publication_idempotency_survives_a_new_publisher(
    publish_engine: Engine,
) -> None:
    fake = FakeGitHubClient()
    settings = Settings(
        _env_file=None,
        github_mode="live",
        github_token="ghp_test_token_value",
        github_repo="acme/warehouse",
        github_base_branch="main",
    )
    store = DbPublicationStore(publish_engine)
    first_publisher = GitHubPublisher(
        fake,
        store,
        repo_slug="acme/warehouse",
        base_branch="main",
        settings=settings,
    )
    first = first_publisher.create_pr(
        make_plan(), make_incident(), make_verification(), make_approval()
    )
    calls_after_first = len(fake.calls)

    second_publisher = GitHubPublisher(
        fake,
        store,
        repo_slug="acme/warehouse",
        base_branch="main",
        settings=settings,
    )
    second = second_publisher.create_pr(
        make_plan(), make_incident(), make_verification(), make_approval()
    )

    assert second.reused and second.url == first.url
    assert len(fake.calls) == calls_after_first
    assert len(fake.pulls) == 1
    with publish_engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM dq.publications")).scalar_one()
    assert count == 1
