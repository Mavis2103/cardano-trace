"""Tracing package."""

from __future__ import annotations

from ..models import TraceStep, TransactionEdge, UTxONode
from .address_interactions import trace_address_interactions
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

    for step in steps:
        if step.error and step.out_ref.node_id() not in node_map:
            # Skip error placeholders — they have no UTXO data to show.
            # The error is tracked via errors_count in trace metadata and
            # can be logged per step for debugging.
            pass

    for step in steps:
        if step.parent_out_ref and step.utxo:
            child_id = step.out_ref.node_id()
            parent_id = step.parent_out_ref.node_id()

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


__all__ = ["trace_backward", "trace_forward", "build_graph_from_steps"]
