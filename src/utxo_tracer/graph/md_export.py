"""Export trace results to AI-readable Markdown (.md) format.

Produces structured Markdown documents that AI agents can parse for
cashflow analysis, CEX detection, and graph traversal — full addresses,
semantic section labels, pipe tables, and adjacency-list graph structure.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

from ..models import (
    AddressInteractionEdge,
    AddressInteractionNode,
    AddressTraceResult,
    OutRef,
    TraceResult,
    TransactionEdge,
    UTxONode,
)

# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def export_trace_markdown(result: TraceResult, output_path: str) -> str:
    """Export a UTXO trace result to an AI-readable Markdown file.

    Returns the absolute path to the written ``.md`` file.
    """
    path = Path(output_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    md = _build_utxo_markdown(result)
    path.write_text(md, encoding="utf-8")
    return str(path)


def export_address_trace_markdown(result: AddressTraceResult, output_path: str) -> str:
    """Export an address-interaction trace result to an AI-readable Markdown file.

    Returns the absolute path to the written ``.md`` file.
    """
    path = Path(output_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    md = _build_address_markdown(result)
    path.write_text(md, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# UTXO trace Markdown builder
# ---------------------------------------------------------------------------


def _build_utxo_markdown(result: TraceResult) -> str:
    lines: list[str] = []

    _utxo_header(lines, result)
    lines.append("")
    _utxo_summary(lines, result)
    lines.append("")
    _utxo_asset_inventory(lines, result)
    lines.append("")
    _utxo_cashflow_path(lines, result)
    lines.append("")
    _utxo_nodes_table(lines, result)
    lines.append("")
    _utxo_edges_table(lines, result)
    lines.append("")
    _utxo_graph_structure(lines, result)
    lines.append("")
    _utxo_cex_findings(lines, result)
    if result.error or result.errors_count:
        lines.append("")
        _utxo_errors(lines, result)

    return "\n".join(lines) + "\n"


# -- helpers that append to a lines list -----------------------------------


def _md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Build a pipe-table block (returns list of lines)."""
    out: list[str] = []
    out.append("| " + " | ".join(headers) + " |")
    out.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        escaped = [_escape_pipe(cell) for cell in row]
        out.append("| " + " | ".join(escaped) + " |")
    return out


def _escape_pipe(text: str) -> str:
    return text.replace("|", "\\|")


def _node_depth_map(result: TraceResult) -> dict[str, str]:
    """Node ID → displayable depth string (int or '?')."""
    dmap: dict[str, int] = {}
    for step in result.steps:
        nid = step.out_ref.node_id()
        if nid not in dmap or step.depth < dmap[nid]:
            dmap[nid] = step.depth
    return {nid: str(d) for nid, d in dmap.items()}


def _node_cex_map(result: TraceResult) -> dict[str, dict]:
    """Node ID → cex_finding dict for quick lookup."""
    cmap: dict[str, dict] = {}
    for cf in result.cex_findings:
        cmap[cf["node_id"]] = cf
    return cmap


def _node_addr_map(result: TraceResult) -> dict[str, UTxONode]:
    """Node ID → UTxONode for use in cashflow path rendering."""
    return {n.id: n for n in result.nodes}


# -- section builders ------------------------------------------------------


def _utxo_header(lines: list[str], result: TraceResult) -> None:
    lines.append("# UTXO Trace Report")
    lines.append("")
    timestamp = datetime.datetime.now().isoformat(timespec="seconds")
    rows: list[list[str]] = [
        ["Start UTXO", f"`{result.start_out_ref}`"],
        ["Direction", result.direction],
        ["Maximum Depth", str(result.max_depth)],
        ["Provider", result.provider_name or "(auto-detected)"],
        ["Generated", timestamp],
    ]
    lines.extend(_md_table(["Field", "Value"], rows))


def _utxo_summary(lines: list[str], result: TraceResult) -> None:
    lines.append("## Summary")
    node_count = len(result.nodes)
    edge_count = len(result.edges)
    cex_hits = len(result.cex_findings)
    total_ada = sum(n.ada for n in result.nodes)
    unique_assets: set[tuple[str, str]] = set()
    for n in result.nodes:
        for asset in n.assets:
            if not asset.is_lovelace:
                unique_assets.add((asset.policy_id, asset.asset_name))
    native_asset_count = len(unique_assets)
    lines.extend(
        _md_table(
            ["Metric", "Value"],
            [
                ["Total Nodes", str(node_count)],
                ["Total Edges", str(edge_count)],
                ["Centralized Exchange (CEX) Hits", str(cex_hits)],
                ["Total ADA in Trace", f"{total_ada:,.6f}"],
                ["Unique Native Assets", str(native_asset_count)],
            ],
        )
    )


def _utxo_asset_inventory(lines: list[str], result: TraceResult) -> None:
    lines.append("## Asset Inventory")
    lines.append("")
    asset_data: dict[tuple[str, str], dict] = {}
    for n in result.nodes:
        for asset in n.assets:
            if asset.is_lovelace:
                continue
            key = (asset.policy_id, asset.asset_name)
            if key not in asset_data:
                asset_data[key] = {"total_qty": 0, "node_ids": []}
            asset_data[key]["total_qty"] += asset.quantity
            asset_data[key]["node_ids"].append(n.id)
    if not asset_data:
        lines.append("*No native assets found in this trace.*")
        return
    rows: list[list[str]] = []
    for (policy_id, asset_name), info in asset_data.items():
        policy_display = (
            f"`{policy_id[:8]}…`" if len(policy_id) > 8 else f"`{policy_id}`"
        )
        rows.append(
            [
                policy_display,
                asset_name,
                f"{info['total_qty']:,}",
                str(len(info["node_ids"])),
            ]
        )
    lines.extend(
        _md_table(
            ["Policy ID", "Asset Name", "Total Quantity", "Nodes"],
            rows,
        )
    )


def _utxo_cashflow_path(lines: list[str], result: TraceResult) -> None:
    lines.append("## Cashflow Path")
    lines.append("")
    lines.append(
        "Ordered list of nodes in BFS traversal order. "
        "Each entry shows the UTXO ID, ADA amount, address type, "
        "CEX status, and discovery depth."
    )
    lines.append("")

    depth_map = _node_depth_map(result)
    cex_map = _node_cex_map(result)
    addr_map = _node_addr_map(result)

    for i, nid in enumerate(result.traced_path, 1):
        node = addr_map.get(nid)
        depth = depth_map.get(nid, "?")
        if node is None:
            lines.append(f"{i}. `{nid}` (depth {depth} — missing data)")
            continue
        ada_str = f"{node.ada:,.6f} ADA"
        # Build multi-asset string
        asset_parts: list[str] = []
        for asset in node.assets:
            if asset.is_lovelace:
                continue
            aname = (
                asset.asset_name
                if len(asset.asset_name) <= 12
                else asset.asset_name[:8] + "…"
            )
            asset_parts.append(f"{asset.quantity:,} {aname}")
        assets_str = " + ".join(asset_parts) if asset_parts else ""
        full_value = ada_str
        if assets_str:
            full_value += f" + {assets_str}"
        type_str = node.address_type
        label_parts: list[str] = [full_value, f"depth {depth}", type_str]
        cex = cex_map.get(nid)
        if cex:
            label_parts.append(f"**{cex['name']}** [CEX]")
        label = " — ".join(label_parts)
        lines.append(f"{i}. `{nid}` ({label})")


def _utxo_nodes_table(lines: list[str], result: TraceResult) -> None:
    lines.append("## Nodes")
    lines.append("")
    depth_map = _node_depth_map(result)
    cex_map = _node_cex_map(result)

    rows: list[list[str]] = []
    for n in result.nodes:
        depth = depth_map.get(n.id, "?")
        asset_count = str(len(n.assets))
        cex_name = cex_map[n.id]["name"] if n.id in cex_map else "—"
        asset_details: list[str] = []
        for asset in n.assets:
            if asset.is_lovelace:
                continue
            policy_short = (
                asset.policy_id[:8] + "…"
                if len(asset.policy_id) > 8
                else asset.policy_id
            )
            asset_details.append(
                f"{policy_short}.{asset.asset_name}: {asset.quantity:,}"
            )
        asset_detail_str = ", ".join(asset_details) if asset_details else "—"
        rows.append(
            [
                f"`{n.id}`",
                n.address,
                n.address_type,
                f"{n.ada:,.6f}",
                asset_count,
                asset_detail_str,
                cex_name,
                depth,
            ]
        )

    lines.extend(
        _md_table(
            ["ID", "Address", "Type", "ADA", "Assets", "Asset Details", "CEX", "Depth"],
            rows,
        )
    )


def _utxo_edges_table(lines: list[str], result: TraceResult) -> None:
    lines.append("## Edges")
    lines.append("")
    rows: list[list[str]] = []
    for e in result.edges:
        rows.append(
            [
                f"`{e.source}`",
                f"`{e.target}`",
                e.direction,
                f"`{e.tx_hash}`" if e.tx_hash else "—",
            ]
        )
    lines.extend(_md_table(["Source", "Target", "Direction", "TX Hash"], rows))


def _utxo_graph_structure(lines: list[str], result: TraceResult) -> None:
    lines.append("## Graph Structure")
    lines.append("")
    lines.append(
        "Adjacency-list format for machine consumption. Each line is `source → target`."
    )
    lines.append("")
    lines.append("```text")
    for e in result.edges:
        lines.append(f"{e.source} → {e.target}")
    lines.append("```")


def _utxo_cex_findings(lines: list[str], result: TraceResult) -> None:
    lines.append("## Centralized Exchange (CEX) Findings")
    lines.append("")
    if not result.cex_findings:
        lines.append("*No centralized exchange addresses detected in this trace.*")
        return
    rows: list[list[str]] = []
    for cf in result.cex_findings:
        rows.append(
            [
                f"`{cf['node_id']}`",
                cf["address"],
                cf["name"],
                cf.get("type", "exchange"),
                cf.get("confidence", "—"),
                f"{cf.get('ada', 0):,.6f}",
            ]
        )
    lines.extend(
        _md_table(
            ["Node ID", "Address", "Exchange", "Type", "Confidence", "ADA"],
            rows,
        )
    )


def _utxo_errors(lines: list[str], result: TraceResult) -> None:
    lines.append("## Errors")
    lines.append("")
    if result.error:
        lines.append(f"- **Trace error:** {result.error}")
    for i, step in enumerate(result.steps, 1):
        if step.error:
            lines.append(
                f"- **Step {i}** (`{step.out_ref}`, depth {step.depth}): {step.error}"
            )


# ---------------------------------------------------------------------------
# Address trace Markdown builder
# ---------------------------------------------------------------------------


def _build_address_markdown(result: AddressTraceResult) -> str:
    lines: list[str] = []

    _addr_header(lines, result)
    lines.append("")
    _addr_summary(lines, result)
    lines.append("")
    _addr_nodes_table(lines, result)
    lines.append("")
    _addr_edges_table(lines, result)
    lines.append("")
    _addr_graph_structure(lines, result)
    lines.append("")
    _addr_cex_findings(lines, result)
    if result.error:
        lines.append("")
        _addr_errors(lines, result)

    return "\n".join(lines) + "\n"


# -- section builders ------------------------------------------------------


def _addr_header(lines: list[str], result: AddressTraceResult) -> None:
    lines.append("# Address Trace Report")
    lines.append("")
    timestamp = datetime.datetime.now().isoformat(timespec="seconds")
    rows: list[list[str]] = [
        ["Target Address", f"`{result.target_address}`"],
        ["Direction", result.direction],
        ["Maximum Depth", str(result.max_depth)],
        ["Provider", result.provider_name or "(auto-detected)"],
        ["Generated", timestamp],
    ]
    lines.extend(_md_table(["Field", "Value"], rows))


def _addr_summary(lines: list[str], result: AddressTraceResult) -> None:
    lines.append("## Summary")
    lines.extend(
        _md_table(
            ["Metric", "Value"],
            [
                ["Unique Addresses", str(len(result.addresses))],
                ["Interaction Edges", str(len(result.edges))],
                ["Total Transactions", str(result.total_transactions)],
            ],
        )
    )


def _addr_nodes_table(lines: list[str], result: AddressTraceResult) -> None:
    lines.append("## Addresses")
    lines.append("")
    rows: list[list[str]] = []
    for node in result.addresses:
        cex_label: str = "—"
        if node.is_cex:
            cex_label = node.cex_name or "(unnamed CEX)"
        elif node.cex_user:
            cex_label = f"{node.cex_user} User"
        target_marker = " **[TARGET]**" if node.is_target else ""
        rows.append(
            [
                f"`{node.address}`{target_marker}",
                node.address_type,
                f"{node.net_ada:+,.6f}",
                f"{node.total_incoming_ada:,.6f}",
                f"{node.total_outgoing_ada:,.6f}",
                str(node.tx_count),
                str(node.depth),
                cex_label,
            ]
        )
    lines.extend(
        _md_table(
            [
                "Address",
                "Type",
                "Net ADA",
                "Incoming ADA",
                "Outgoing ADA",
                "TX Count",
                "Depth",
                "CEX",
            ],
            rows,
        )
    )


def _addr_edges_table(lines: list[str], result: AddressTraceResult) -> None:
    lines.append("## Edges")
    lines.append("")
    rows: list[list[str]] = []
    for edge in result.edges:
        tx_hashes_str = (
            ", ".join(f"`{h}`" for h in edge.tx_hashes) if edge.tx_hashes else "—"
        )
        rows.append(
            [
                f"`{edge.source}`",
                f"`{edge.target}`",
                edge.direction_relative_to_target,
                str(edge.interaction_count),
                tx_hashes_str,
            ]
        )
    lines.extend(
        _md_table(
            ["Source", "Target", "Direction", "Interactions", "TX Hashes"],
            rows,
        )
    )


def _addr_graph_structure(lines: list[str], result: AddressTraceResult) -> None:
    lines.append("## Graph Structure")
    lines.append("")
    lines.append(
        "Adjacency-list format for machine consumption. Each line is `source → target`."
    )
    lines.append("")
    lines.append("```text")
    for edge in result.edges:
        lines.append(f"{edge.source} → {edge.target}")
    lines.append("```")


def _addr_cex_findings(lines: list[str], result: AddressTraceResult) -> None:
    lines.append("## Centralized Exchange (CEX) Findings")
    lines.append("")
    cex_nodes = [n for n in result.addresses if n.is_cex or n.cex_user]
    if not cex_nodes:
        lines.append(
            "*No centralized exchange addresses or CEX users detected in this trace.*"
        )
        return
    rows: list[list[str]] = []
    for node in cex_nodes:
        if node.is_cex:
            label = node.cex_name or "(unnamed CEX)"
        else:
            label = f"{node.cex_user} User"
        rows.append(
            [
                f"`{node.address}`",
                label,
                "CEX" if node.is_cex else "CEX-adjacent wallet",
                f"{node.net_ada:+,.6f}",
                str(node.tx_count),
            ]
        )
    lines.extend(
        _md_table(
            ["Address", "Exchange", "Type", "Net ADA", "TX Count"],
            rows,
        )
    )


def _addr_errors(lines: list[str], result: AddressTraceResult) -> None:
    lines.append("## Errors")
    lines.append("")
    lines.append(f"- **Trace error:** {result.error}")
