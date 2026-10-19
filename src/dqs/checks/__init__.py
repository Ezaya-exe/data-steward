"""Deterministic check engine (docs/ARCHITECTURE.md §7.3).

One module per check family plus `engine.py`, which turns an approved contract
and a batch (or a staging/marts snapshot) into `list[Violation]` by reusing the
profiler's facts. No LLM and no parsing happen here.
"""
