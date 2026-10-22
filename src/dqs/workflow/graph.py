"""The incident LangGraph wiring (ARCHITECTURE §10.2).

Why the routing is plain functions: every edge is a product rule, not a model
judgement. `plan_resolution` decides PR vs ISSUE vs NONE from the closed
enums; `verify_fix` decides pass / retry / exhaust from the deterministic
verdict and the configured attempt limit. Keeping those predicates here makes
the graph diagram and the tests read the same.

The gate uses LangGraph's `interrupt`/`Command(resume=...)`. Because a node is
re-executed from the start when resumed, the gate is written to be idempotent:
setting `awaiting_approval` twice is harmless, and the decision is persisted
only after the interrupt returns.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from dqs.models.enums import ResolutionType
from dqs.models.state import WorkflowState
from dqs.workflow.nodes import (
    WorkflowDeps,
    approval_gate_node,
    assess_impact_node,
    build_issue_node,
    investigate_node,
    plan_fix_node,
    plan_resolution_node,
    publish_node,
    verify_fix_node,
)


def route_after_resolution(state: WorkflowState) -> str:
    """NONE ends; PR plans a fix; everything else files an issue."""
    resolution = state.get("resolution_type")
    if resolution is ResolutionType.NONE:
        return "none"
    if resolution is ResolutionType.PR:
        return "pr"
    return "issue"


def make_route_after_verification(
    max_fix_attempts: int,
) -> Callable[[WorkflowState], str]:
    """Build the verification predicate with the configured attempt limit."""

    def route(state: WorkflowState) -> str:
        if bool(state.get("budget_exceeded")):
            return "budget"
        if bool(state.get("verification_passed")):
            return "passed"
        if int(state.get("attempt_no", 0)) < max_fix_attempts:
            return "retry"
        return "exhausted"

    return route


def route_after_approval(state: WorkflowState) -> str:
    """Only an explicit approve reaches `publish`; a rejection ends the run."""
    approval = state.get("approval")
    if approval is not None and approval.decision == "approve":
        return "publish"
    return "end"


def _add_node(
    builder: StateGraph[WorkflowState],
    name: str,
    node: Callable[[WorkflowState], dict[str, Any]],
) -> None:
    # LangGraph 1.2 bounds node state to a state-like protocol that mypy does
    # not accept for a TypedDict (upstream limitation); runtime matching is
    # correct and the node/state contract is exercised by the workflow tests.
    builder.add_node(name, node)  # type: ignore[call-overload]


def build_graph(
    deps: WorkflowDeps,
    *,
    checkpointer: object | None = None,
) -> CompiledStateGraph:
    """Compile the workflow; the checkpointer is supplied by the runner/tests."""
    builder: StateGraph[WorkflowState] = StateGraph(WorkflowState)
    _add_node(builder, "investigate", investigate_node(deps))
    _add_node(builder, "assess_impact", assess_impact_node(deps))
    _add_node(builder, "plan_resolution", plan_resolution_node(deps))
    _add_node(builder, "plan_fix", plan_fix_node(deps))
    _add_node(builder, "verify_fix", verify_fix_node(deps))
    _add_node(builder, "build_issue", build_issue_node(deps))
    _add_node(builder, "approval_gate", approval_gate_node(deps))
    _add_node(builder, "publish", publish_node(deps))

    builder.add_edge(START, "investigate")
    builder.add_edge("investigate", "assess_impact")
    builder.add_edge("assess_impact", "plan_resolution")
    builder.add_conditional_edges(
        "plan_resolution",
        route_after_resolution,
        {"none": END, "pr": "plan_fix", "issue": "build_issue"},
    )
    builder.add_edge("plan_fix", "verify_fix")
    builder.add_conditional_edges(
        "verify_fix",
        make_route_after_verification(deps.settings.max_fix_attempts),
        {
            "budget": "build_issue",
            "passed": "approval_gate",
            "retry": "plan_fix",
            "exhausted": "build_issue",
        },
    )
    builder.add_edge("build_issue", "approval_gate")
    builder.add_conditional_edges(
        "approval_gate",
        route_after_approval,
        {"publish": "publish", "end": END},
    )
    builder.add_edge("publish", END)
    return builder.compile(checkpointer=checkpointer)  # type: ignore[arg-type]


def draw_mermaid(deps: WorkflowDeps) -> str:
    """The compiled graph diagram (used in the phase note and docs)."""
    return build_graph(deps).get_graph().draw_mermaid()


__all__ = [
    "build_graph",
    "draw_mermaid",
    "make_route_after_verification",
    "route_after_approval",
    "route_after_resolution",
]
