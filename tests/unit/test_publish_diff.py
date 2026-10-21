"""Unit tests for deterministic publish diffs (Phase 6a).

The key acceptance test is the exact F1 patch: a known edit against the real
`stg_orders.sql` must render byte-for-byte as expected, because the dry-run
`.patch` is what a reviewer inspects and live mode commits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dqs.errors import FixPlanError
from dqs.models.fix import Edit
from dqs.publish.diff import build_file_patches, full_patch
from dqs.verify.patch import PROJECT_DIR

F1_OLD = "    customer_id::integer          as customer_id,"
F1_NEW = "    coalesce(customer_id::integer, cust_id::integer) as customer_id,"
F1_PATCH = (
    "--- a/models/staging/stg_orders.sql\n"
    "+++ b/models/staging/stg_orders.sql\n"
    "@@ -1,7 +1,7 @@\n"
    " {{ config(materialized='table') }}\n"
    " select\n"
    "     order_id::integer             as order_id,\n"
    "-    customer_id::integer          as customer_id,\n"
    "+    coalesce(customer_id::integer, cust_id::integer) as customer_id,\n"
    "     order_ts::timestamptz         as order_ts,\n"
    "     status,\n"
    "     total_amount::numeric(12, 2)  as total_amount,\n"
)


def test_known_f1_edit_produces_the_exact_patch() -> None:
    patches = build_file_patches(
        [Edit(path="models/staging/stg_orders.sql", old=F1_OLD, new=F1_NEW)]
    )

    assert len(patches) == 1
    patch = patches[0]
    assert patch.path == "models/staging/stg_orders.sql"
    assert patch.patch == F1_PATCH
    assert patch.added == 1
    assert patch.removed == 1
    assert F1_OLD in patch.old_text
    assert F1_NEW in patch.new_text
    assert full_patch(patches) == F1_PATCH


def test_the_real_project_file_is_not_modified() -> None:
    target = PROJECT_DIR / "models" / "staging" / "stg_orders.sql"
    before = target.read_bytes()

    build_file_patches([Edit(path="models/staging/stg_orders.sql", old=F1_OLD, new=F1_NEW)])

    assert target.read_bytes() == before


def test_multiple_edits_to_one_file_apply_sequentially(tmp_path: Path) -> None:
    (tmp_path / "models" / "staging").mkdir(parents=True)
    target = tmp_path / "models" / "staging" / "model.sql"
    target.write_text("select 1 as a\nselect 2 as b\n", encoding="utf-8", newline="\n")

    patches = build_file_patches(
        [
            Edit(path="models/staging/model.sql", old="select 1 as a", new="select 10 as a"),
            Edit(path="models/staging/model.sql", old="select 2 as b", new="select 20 as b"),
        ],
        project_dir=tmp_path,
    )

    assert len(patches) == 1
    assert "select 10 as a" in patches[0].new_text
    assert "select 20 as b" in patches[0].new_text
    assert patches[0].added == 2
    assert patches[0].removed == 2


def test_non_unique_old_string_is_refused(tmp_path: Path) -> None:
    (tmp_path / "models" / "staging").mkdir(parents=True)
    (tmp_path / "models" / "staging" / "model.sql").write_text("x\nx\n", encoding="utf-8")

    with pytest.raises(FixPlanError, match="exactly once"):
        build_file_patches(
            [Edit(path="models/staging/model.sql", old="x", new="y")], project_dir=tmp_path
        )


def test_edit_outside_the_allowlist_is_refused() -> None:
    with pytest.raises(FixPlanError, match="under warehouse/dbt_project/models"):
        build_file_patches([Edit(path="macros/evil.sql", old="a", new="b")])


def test_path_traversal_is_refused() -> None:
    with pytest.raises(FixPlanError):
        build_file_patches([Edit(path="models/../../evil.sql", old="a", new="b")])


def test_missing_target_file_is_refused() -> None:
    with pytest.raises(FixPlanError, match="does not exist"):
        build_file_patches([Edit(path="models/staging/nope.sql", old="a", new="b")])
