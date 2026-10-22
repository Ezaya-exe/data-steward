"""Unit tests for live GitHub publishing with a fake client (Phase 6a).

No test here touches the network. The fake records every call, so the safety
properties are asserted directly: validated branch name, commits only on the
new branch, one PR, the label step, and idempotency on a second run.
"""

from __future__ import annotations

import pytest
from tests.helpers.fake_github import FakeGitHubClient
from tests.helpers.publish import (
    INCIDENT_ID,
    make_approval,
    make_incident,
    make_plan,
    make_verification,
)

from dqs.errors import PublicationError
from dqs.publish.base import PublicationKind, PublicationMode
from dqs.publish.dry_run import DryRunPublisher
from dqs.publish.github import (
    BRANCH_PATTERN,
    LABEL_NAME,
    GitHubPublisher,
    RemoteFile,
    branch_name,
    build_publisher,
    validate_branch_name,
)
from dqs.publish.store import MemoryPublicationStore

BASE = "main"


def _settings(**overrides):
    from dqs.config import Settings

    values = {
        "github_mode": "live",
        "github_token": "ghp_test_token_value",
        "github_repo": "acme/warehouse",
        "github_base_branch": BASE,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _publisher(
    fake: FakeGitHubClient, *, settings=None, store: MemoryPublicationStore | None = None
) -> tuple[GitHubPublisher, MemoryPublicationStore]:
    resolved_store = store or MemoryPublicationStore()
    publisher = GitHubPublisher(
        fake,
        resolved_store,
        repo_slug="acme/warehouse",
        base_branch=BASE,
        settings=settings or _settings(),
    )
    return publisher, resolved_store


def test_live_pr_uses_the_validated_incident_branch_and_never_the_base() -> None:
    fake = FakeGitHubClient()
    fake.seed_base_files(make_plan())
    publisher, store = _publisher(fake)
    expected = branch_name(INCIDENT_ID)

    result = publisher.create_pr(make_plan(), make_incident(), make_verification(), make_approval())

    assert BRANCH_PATTERN.match(expected)
    assert expected == "dqs/incident-12345678"
    assert result.mode is PublicationMode.LIVE
    assert result.kind.value == "pr"
    assert fake.branches == {expected: "base-sha"}
    assert [name for name, _ in fake.calls].count("create_branch") == 1
    assert [name for name, _ in fake.calls].count("create_pull") == 1

    writes = [payload for name, payload in fake.calls if name == "put_file"]
    assert len(writes) == 1
    assert writes[0]["path"] == "warehouse/dbt_project/models/staging/stg_orders.sql"
    assert writes[0]["branch"] == expected != BASE
    assert "coalesce(customer_id::integer, cust_id::integer)" in writes[0]["content"]

    pulls = [payload for name, payload in fake.calls if name == "create_pull"]
    assert pulls[0]["head"] == expected
    assert pulls[0]["base"] == BASE
    assert "## Verification" in pulls[0]["body"]

    assert LABEL_NAME in fake.labels
    assert [name for name, _ in fake.calls].count("ensure_label") == 1
    labels = [payload for name, payload in fake.calls if name == "add_labels"]
    assert labels[0]["labels"] == [LABEL_NAME]
    assert len(store.rows) == 1


def test_live_pr_is_idempotent_across_a_second_run() -> None:
    fake = FakeGitHubClient()
    fake.seed_base_files(make_plan())
    store = MemoryPublicationStore()
    first_publisher, _ = _publisher(fake, store=store)
    first = first_publisher.create_pr(
        make_plan(), make_incident(), make_verification(), make_approval()
    )
    call_count = len(fake.calls)

    # A new publisher object simulates a fresh process with the same DB.
    second_publisher, _ = _publisher(fake, store=store)
    second = second_publisher.create_pr(
        make_plan(), make_incident(), make_verification(), make_approval()
    )

    assert second.reused
    assert second.url == first.url
    assert len(fake.calls) == call_count
    assert len(fake.branches) == 1
    assert len(fake.pulls) == 1
    assert len(store.rows) == 1


def test_an_existing_branch_and_pr_are_reused_after_a_crash() -> None:
    fake = FakeGitHubClient()
    expected = branch_name(INCIDENT_ID)
    fake.branches[expected] = "base-sha"
    fake.pulls[expected] = (7, "https://github.com/acme/warehouse/pull/7")
    publisher, _ = _publisher(fake)

    result = publisher.create_pr(make_plan(), make_incident(), make_verification(), make_approval())

    assert result.reused
    assert result.url.endswith("/pull/7")
    assert not [
        name for name, _ in fake.calls if name in {"create_branch", "create_pull", "put_file"}
    ]


def test_live_issue_is_idempotent_and_sanitizes_hostile_markdown() -> None:
    fake = FakeGitHubClient()
    publisher, store = _publisher(fake)
    body = "# [data-quality] hostile\n\nha @octocat #1 <script>x</script> ghp_abcdefghijklmnop\n"

    first = publisher.create_issue(body, make_incident(), make_approval())
    second = publisher.create_issue(body, make_incident(), make_approval())

    assert second.reused
    assert first.url.endswith("/issues/1")
    assert len(fake.issues) == 1
    assert "@octocat" not in fake.issues[0]["body"]
    assert "#1" not in fake.issues[0]["body"]
    assert "<script>" not in fake.issues[0]["body"]
    assert "ghp_" not in fake.issues[0]["body"]
    assert fake.issues[0]["labels"] == [LABEL_NAME]
    assert len(store.rows) == 1


def test_live_mode_without_token_or_repo_fails_closed() -> None:
    with pytest.raises(PublicationError, match="GITHUB_TOKEN"):
        build_publisher(_settings(github_token=""), store=MemoryPublicationStore())
    with pytest.raises(PublicationError, match="GITHUB_REPO"):
        build_publisher(_settings(github_repo=""), store=MemoryPublicationStore())
    with pytest.raises(PublicationError, match="GITHUB_TOKEN and GITHUB_REPO"):
        build_publisher(
            _settings(github_token="", github_repo=""),
            store=MemoryPublicationStore(),
        )


def test_live_mode_rejects_a_malformed_repo_slug() -> None:
    with pytest.raises(PublicationError, match="owner/repo"):
        build_publisher(
            _settings(github_repo="not a repo"),
            store=MemoryPublicationStore(),
            github_client=FakeGitHubClient(),
        )


def test_dry_run_build_never_touches_the_github_client(tmp_path) -> None:
    fake = FakeGitHubClient()
    settings = _settings(github_mode="dry_run")
    publisher = build_publisher(
        settings,
        store=MemoryPublicationStore(),
        github_client=fake,
        output_dir=tmp_path,
    )

    assert isinstance(publisher, DryRunPublisher)
    result = publisher.create_pr(make_plan(), make_incident(), make_verification(), make_approval())

    assert result.mode is PublicationMode.DRY_RUN
    assert fake.calls == []
    assert (tmp_path / f"{INCIDENT_ID.hex[:8]}.md").is_file()


def test_branch_names_are_validated_before_any_call() -> None:
    for bad in ("main", "dqs/incident-XYZ", "dqs/incident-123456789", "../main"):
        with pytest.raises(PublicationError, match="unsafe branch name"):
            validate_branch_name(bad)


def test_refuses_when_the_base_branch_is_the_incident_branch() -> None:
    fake = FakeGitHubClient()
    publisher = GitHubPublisher(
        fake,
        MemoryPublicationStore(),
        repo_slug="acme/warehouse",
        base_branch=branch_name(INCIDENT_ID),
        settings=_settings(github_base_branch=branch_name(INCIDENT_ID)),
    )

    with pytest.raises(PublicationError, match="base branch"):
        publisher.create_pr(make_plan(), make_incident(), make_verification(), make_approval())

    assert fake.calls == []


def test_refuses_when_the_base_branch_file_differs_from_the_local_checkout() -> None:
    fake = FakeGitHubClient()
    target = "warehouse/dbt_project/models/staging/stg_orders.sql"
    fake.files[(target, BASE)] = RemoteFile(text="someone else's version", sha="remote")

    publisher, store = _publisher(fake)

    with pytest.raises(PublicationError, match="push local commits first"):
        publisher.create_pr(make_plan(), make_incident(), make_verification(), make_approval())

    written = [
        name for name, _ in fake.calls if name in {"create_branch", "put_file", "create_pull"}
    ]
    assert written == []
    assert store.rows == []


def test_refuses_when_a_target_file_is_missing_on_the_base_branch() -> None:
    fake = FakeGitHubClient()
    publisher, _ = _publisher(fake)

    with pytest.raises(PublicationError, match="push local commits first"):
        publisher.create_pr(make_plan(), make_incident(), make_verification(), make_approval())

    assert not [name for name, _ in fake.calls if name in {"create_branch", "put_file"}]


def test_pending_issue_retry_recovers_the_existing_issue_by_marker() -> None:
    fake = FakeGitHubClient()
    store = MemoryPublicationStore()
    publisher, _ = _publisher(fake, store=store)
    incident = make_incident()

    # Simulate a crash after GitHub created the issue but before the row went
    # from pending to done: the next attempt sees the pending marker.
    publisher.create_issue("body text", incident, make_approval())
    created = fake.issues[0]
    store.rows.clear()
    store.pending.add((incident.incident_id, PublicationKind.ISSUE))
    fake.reset_calls()

    result = publisher.create_issue("body text", incident, make_approval())

    assert result.reused
    assert result.url == created["url"]
    assert [name for name, _ in fake.calls] == ["find_issue"]
    assert len(fake.issues) == 1


def test_github_errors_are_wrapped_with_the_token_redacted() -> None:
    fake = FakeGitHubClient(
        fail_on="create_pull",
        fail_message="401 bad credentials for ghp_supersecrettoken1234567890",
    )
    fake.seed_base_files(make_plan())
    publisher, store = _publisher(fake)

    with pytest.raises(PublicationError) as raised:
        publisher.create_pr(make_plan(), make_incident(), make_verification(), make_approval())

    message = str(raised.value)
    assert "ghp_" not in message
    assert "***redacted***" in message
    assert store.rows == []
