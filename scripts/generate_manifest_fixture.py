"""One-shot generator for tests/fixtures/manifest_fixture.json (Phase 3).

Reads the REAL manifest (target/manifest.json, produced by `dbt compile`) and
writes a trimmed fixture that provably mirrors the real DAG: every source,
every model, node-generating tests, and the three exposures, each reduced to
the keys the lineage code reads. Byte-deterministic output (sort_keys).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "warehouse" / "dbt_project" / "target" / "manifest.json"
OUT = ROOT / "tests" / "fixtures" / "manifest_fixture.json"

NODE_KEYS = (
    "unique_id",
    "name",
    "resource_type",
    "depends_on",
    "original_file_path",
    "compiled_code",
)
SOURCE_KEYS = ("unique_id", "name", "resource_type", "schema", "identifier")
EXPOSURE_KEYS = ("unique_id", "name", "resource_type", "owner", "maturity", "depends_on")


def trim(payload: dict, keys: tuple[str, ...]) -> dict:
    out = {k: payload[k] for k in keys if k in payload}
    deps = out.get("depends_on")
    if isinstance(deps, dict):
        out["depends_on"] = {"nodes": list(deps.get("nodes") or [])}
    return out


def main() -> None:
    manifest = json.loads(SRC.read_text(encoding="utf-8"))
    nodes = {}
    for uid, node in manifest["nodes"].items():
        if node.get("resource_type") in ("model", "test", "unit"):
            nodes[uid] = trim(node, NODE_KEYS)
    sources = {uid: trim(s, SOURCE_KEYS) for uid, s in manifest["sources"].items()}
    exposures = {uid: trim(e, EXPOSURE_KEYS) for uid, e in manifest["exposures"].items()}

    # child_map/parent_map rebuilt from the trimmed set so they are consistent
    # with it (the graph code prefers them when present).
    child_map: dict[str, list[str]] = {}
    parent_map: dict[str, list[str]] = {}
    everything = {**nodes, **sources, **exposures}
    for uid, payload in everything.items():
        parents = [(payload.get("depends_on") or {}).get("nodes") or []]
        parent_map[uid] = [p for p in (parents[0] or []) if p in everything]

    for uid, parents in parent_map.items():
        for parent in parents:
            child_map.setdefault(parent, []).append(uid)
    all_ids = sorted(everything)
    parent_map = {uid: sorted(parent_map.get(uid, [])) for uid in all_ids}
    child_map = {uid: sorted(child_map.get(uid, [])) for uid in all_ids}

    fixture = {
        "metadata": {"dbt_version": manifest["metadata"]["dbt_version"], "fixture": True},
        "nodes": nodes,
        "sources": sources,
        "exposures": exposures,
        "child_map": child_map,
        "parent_map": parent_map,
    }
    OUT.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(nodes)} nodes, {len(sources)} sources, {len(exposures)} exposures)")


if __name__ == "__main__":
    main()
