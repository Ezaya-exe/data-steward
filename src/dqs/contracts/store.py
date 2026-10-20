"""Store: contract files on disk, always deterministic and strictly validated.

Why strict on both write and read: contracts are the rules the check engine
enforces. A typo'd key (`nullabel:`) or an extra field must never be silently
dropped into a rule set that code then trusts (AGENTS §7-style audited
strictness; §7.1 "loading validates strictly, unknown keys rejected").

Byte-stability guarantees: sorted keys, stable 6-dp float formatting at
generation (rounding in the generator), LF line endings (`.gitattributes`
pins LF repo-wide), no YAML anchors/aliases, no wall-clock in the document —
so two `dqs contracts generate` runs over one baseline are byte-identical
(Phase 2 acceptance criterion).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import yaml
from pydantic import ValidationError

from dqs.errors import ContractError
from dqs.models.contract import Contract
from dqs.models.enums import ContractStatus

CONTRACTS_DIRNAME: Final = "contracts"  # repo-root default (§7.1)
FLOAT_PRECISION_DIGITS: Final = 6


class _NoAliasDumper(yaml.SafeDumper):
    """A dumper that never emits YAML anchors/aliases.

    Why: identical sub-objects (reused sub-models) would otherwise serialize as
    `&id001` anchors even when they are separate rules — that couples the file
    bytes to object identity and makes diffs brittle.
    """

    def ignore_aliases(self, _data: object) -> bool:  # noqa: PLR6301 - pyyaml API
        return True


def contract_path(directory: Path, table: str) -> Path:
    """`<directory>/<table>.yml` — the qualified name is the file name (§7.1)."""
    _ = table  # naming documented: the fully qualified name is the stem
    return directory / f"{table}.yml"


def to_yaml(contract: Contract) -> str:
    """Contract → deterministic YAML text (one dumper per call, no state)."""
    document = contract.model_dump(mode="json", exclude_none=True)
    as_yaml = yaml.dump(
        document,
        Dumper=_NoAliasDumper,
        sort_keys=True,
        default_flow_style=False,
        width=4096,
        allow_unicode=True,
    )
    return as_yaml


def save(contract: Contract, directory: Path) -> Path:
    """Write one contract file atomically enough for a text artifact."""
    directory.mkdir(parents=True, exist_ok=True)
    path = contract_path(directory, contract.table)
    path.write_text(to_yaml(contract), encoding="utf-8", newline="\n")
    return path


def _normalized_yaml(contract: Contract) -> str:
    """Content bytes with the status line normalized away (approval is not content)."""
    return to_yaml(contract.model_copy(update={"status": ContractStatus.DRAFT}))


def save_generated(contract: Contract, directory: Path, *, force: bool = False) -> Path:
    """Save one freshly generated contract while protecting an approved one.

    Approval is a human decision. `dqs contracts generate` is safe to re-run,
    so an identical regeneration keeps `status: approved` instead of silently
    turning the rule set back into a draft. If the content really changed, the
    command refuses (unless `--force`), because a write would otherwise replace
    a reviewed rule set without a human seeing the diff.
    """
    written = save_generated_many([contract], directory, force=force)
    return written[0]


def save_generated_many(
    contracts: Sequence[Contract], directory: Path, *, force: bool = False
) -> list[Path]:
    """Validate every approved-file conflict first, then write all contracts.

    All-or-nothing: a refused overwrite must not leave a half-regenerated
    catalogue behind. `--force` replaces changed content with the generated
    **draft**, so the change has to be re-approved explicitly.
    """
    prepared: list[Contract] = []
    conflicts: list[Path] = []
    for contract in contracts:
        path = contract_path(directory, contract.table)
        if path.exists():
            existing = load_contract(path)
            if existing.status is ContractStatus.APPROVED:
                if _normalized_yaml(existing) == _normalized_yaml(contract):
                    prepared.append(contract.model_copy(update={"status": ContractStatus.APPROVED}))
                    continue
                if not force:
                    conflicts.append(path)
                    continue
        prepared.append(contract)
    if conflicts:
        names = ", ".join(str(path) for path in conflicts)
        raise ContractError(
            "refusing to overwrite approved contract(s) with different content: "
            f"{names}; review the diff and re-run with --force to write drafts"
        )
    return [save(contract, directory) for contract in prepared]


def load_contract(path: Path) -> Contract:
    """Parse + strictly validate one YAML file (unknown keys are errors)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ContractError(f"{path}: YAML parse failed: {exc}") from exc
    return _validate(path, raw)


def _validate(path: Path, raw: object) -> Contract:
    """Wrap Pydantic's error in a `ContractError` named after the file."""
    try:
        model = Contract.model_validate(raw)
    except ValidationError as exc:
        raise ContractError(f"{path}: contract validation failed: {exc}") from exc
    return model


def load_contracts(directory: Path) -> dict[str, Contract]:
    """Load every `*.yml` in `directory`, keyed by the contract's table."""
    contracts: dict[str, Contract] = {}
    for path in sorted(directory.glob("*.yml")):
        contract = load_contract(path)
        contracts[contract.table] = contract
    return contracts


def load_approved(directory: Path) -> dict[str, Contract]:
    """Only the approved contracts — checks enforce nothing else (§7.1).

    Draft contracts are a human-review artifact, not a rule set, so the check
    engine must never see them; the caller separately warns when this is empty.
    """
    return {
        table: contract
        for table, contract in load_contracts(directory).items()
        if contract.status == ContractStatus.APPROVED
    }


def approve(directory: Path, tables: Sequence[str] | None) -> list[Path]:
    """The human step (§7.2): flip `tables` — or all — from draft to approved.

    Reads strictly, writes deterministically; the resulting file bytes must
    match a regenerated file whose status was then approved (the status line
    is the only diff).
    """
    rewritten: list[Path] = []
    for table, contract in load_contracts(directory).items():
        if tables is not None and table not in tables:
            continue
        contract.status = ContractStatus.APPROVED
        rewritten.append(save(contract, directory))
    return rewritten
