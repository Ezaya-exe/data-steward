"""Lineage and impact analysis (ARCHITECTURE §9).

Deterministic only: manifest → DAG (`graph.py`), compiled-SQL column
references (`columns.py`), impact report builder (`impact.py`). No LLM here —
the summary field is a template with a clearly marked Phase-4 hook.
"""

from __future__ import annotations
