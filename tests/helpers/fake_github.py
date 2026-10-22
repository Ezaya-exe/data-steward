"""In-memory GitHub client for publish tests (Phase 6a).

Records every call and models just enough of GitHub to assert the live
publisher's safety properties: one branch, commits only on that branch, one PR,
the label step, and no duplicate on the second run. No network, no token.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dqs.publish.github import RemoteFile


@dataclass
class FakeGitHubClient:
    base_branches: dict[str, str] = field(default_factory=lambda: {"main": "base-sha"})
    branches: dict[str, str] = field(default_factory=dict)
    files: dict[tuple[str, str], RemoteFile] = field(default_factory=dict)
    pulls: dict[str, tuple[int, str]] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    labels: set[str] = field(default_factory=set)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail_on: str | None = None
    fail_message: str = "boom"

    def _record(self, call: str, **payload: Any) -> None:
        self.calls.append((call, payload))
        if self.fail_on == call:
            raise RuntimeError(self.fail_message)

    def base_sha(self, branch: str) -> str:
        self._record("base_sha", branch=branch)
        return self.base_branches[branch]

    def branch_exists(self, branch: str) -> bool:
        self._record("branch_exists", branch=branch)
        return branch in self.branches

    def read_file(self, path: str, ref: str) -> RemoteFile | None:
        self._record("read_file", path=path, ref=ref)
        return self.files.get((path, ref))

    def create_branch(self, branch: str, sha: str) -> None:
        self._record("create_branch", branch=branch, sha=sha)
        self.branches[branch] = sha

    def put_file(self, path: str, content: str, message: str, branch: str, sha: str | None) -> None:
        self._record(
            "put_file",
            path=path,
            content=content,
            message=message,
            branch=branch,
            sha=sha,
        )
        self.files[(path, branch)] = RemoteFile(text=content, sha=f"sha-{len(self.files) + 1}")

    def pull_for_branch(self, branch: str) -> tuple[int, str] | None:
        self._record("pull_for_branch", branch=branch)
        return self.pulls.get(branch)

    def create_pull(self, *, title: str, body: str, head: str, base: str) -> tuple[int, str]:
        self._record("create_pull", title=title, body=body, head=head, base=base)
        number = len(self.pulls) + 1
        url = f"https://github.com/acme/warehouse/pull/{number}"
        self.pulls[head] = (number, url)
        return number, url

    def ensure_label(self, name: str) -> None:
        self._record("ensure_label", name=name)
        self.labels.add(name)

    def add_labels(self, number: int, labels: list[str] | tuple[str, ...]) -> None:
        self._record("add_labels", number=number, labels=list(labels))

    def create_issue(
        self, *, title: str, body: str, labels: list[str] | tuple[str, ...]
    ) -> tuple[int, str]:
        self._record("create_issue", title=title, body=body, labels=list(labels))
        number = len(self.issues) + 1
        url = f"https://github.com/acme/warehouse/issues/{number}"
        self.issues.append(
            {"number": number, "title": title, "body": body, "url": url, "labels": list(labels)}
        )
        return number, url

    def find_issue(self, marker: str) -> tuple[int, str] | None:
        self._record("find_issue", marker=marker)
        for issue in self.issues:
            if marker in (issue["title"] or "") or marker in (issue["body"] or ""):
                return int(issue["number"]), str(issue["url"])
        return None

    def seed_base_files(self, plan: Any, base_branch: str = "main") -> None:
        """Make the fake base branch hold the local pre-patch file contents.

        The live publisher refuses to commit when the remote base differs from
        the local checkout; tests that expect a successful publish must seed
        the fake remote with exactly the same content.
        """
        from dqs.publish.diff import build_file_patches

        for patch in build_file_patches(plan.edits):
            self.files[(patch.path, base_branch)] = RemoteFile(
                text=patch.old_text, sha=f"base-{patch.path}"
            )

    def reset_calls(self) -> None:
        self.calls.clear()
