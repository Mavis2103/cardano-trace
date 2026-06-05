"""Tracing package."""

from __future__ import annotations

from ..models import Asset, TraceStep, TransactionEdge, UTxONode

# Sentinel address for nodes the provider could not fetch (timeout/404/error).
# Rendered as a distinct "broken" marker in the graph so a hole in the middle
# of a chain is visible instead of silently severing downstream branches.
MISSING_ADDRESS = "⚠ unfetched"
from .address_interactions import apply_cex_filter, trace_address_interactions
from .backward import trace_backward
from .forward import trace_forward


def build_graph_from_steps(
    steps: list[TraceStep], direction: str
) -> tuple[list[UTxONode], list[TransactionEdge], list[str]]:
    """Reduce trace steps to deduplicated nodes/edges/traced_path."""
    node_map: dict[str, UTxONode] = {}
    edges: list[TransactionEdge] = []
    traced_path: list[str] = []
    seen_path: set[str] = set()
    seen_edges: set[str] = set()

    for step in steps:
        if step.utxo:
            nid = step.out_ref.node_id()
            node_map[nid] = step.utxo
            if nid not in seen_path:
                seen_path.add(nid)
                traced_path.append(nid)

    # Materialise error steps that have a parent as visible "missing" markers so
    # a 404/timeout in the middle of a chain shows up as a broken node rather
    # than silently disconnecting everything downstream of it.
    for step in steps:
        nid = step.out_ref.node_id()
        if step.error and nid not in node_map and step.parent_out_ref is not None:
            node_map[nid] = UTxONode(
                id=nid,
                out_ref=step.out_ref,
                address=MISSING_ADDRESS,
                assets=[Asset(policy_id="", asset_name="", quantity=0)],
            )
            if nid not in seen_path:
                seen_path.add(nid)
                traced_path.append(nid)

    for step in steps:
        # Draw the edge whenever both endpoints resolved to a node (real or a
        # missing-marker), so the topology survives provider holes.
        if step.parent_out_ref is None:
            continue
        child_id = step.out_ref.node_id()
        parent_id = step.parent_out_ref.node_id()
        if child_id not in node_map or parent_id not in node_map:
            continue

        if direction == "backward":
            src, dst = child_id, parent_id
        else:
            src, dst = parent_id, child_id

        edge_id = f"{src}->{dst}"
        if edge_id not in seen_edges:
            seen_edges.add(edge_id)
            edges.append(
                TransactionEdge(
                    id=edge_id,
                    source=src,
                    target=dst,
                    direction="input" if direction == "backward" else "output",
                    tx_hash=step.parent_out_ref.tx_hash,
                )
            )

    return list(node_map.values()), edges, traced_path


__all__ = [
    "trace_backward",
    "trace_forward",
    "trace_address_interactions",
    "apply_cex_filter",
    "build_graph_from_steps",
]
