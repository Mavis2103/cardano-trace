"""Dash Cytoscape for address-level interaction graph.

Shows addresses as nodes connected by shared transactions.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Optional

import dash
import dash_cytoscape as cyto
from dash import html

from utxo_tracer.cex.registry import identify_cex
from utxo_tracer.models import AddressTraceResult
from utxo_tracer.utils import AddressType, classify_address


def _address_colour(address: str) -> str:
    h = int(hashlib.sha256(address.encode()).hexdigest()[:8], 16)
    return _hsl_to_hex(h % 360, 0.55, 0.48)


def _hsl_to_hex(h: int, s: float, l: float) -> str:
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = l - c / 2
    if   h < 60:  r0, g0, b0 = c, x, 0
    elif h < 120: r0, g0, b0 = x, c, 0
    elif h < 180: r0, g0, b0 = 0, c, x
    elif h < 240: r0, g0, b0 = 0, x, c
    elif h < 300: r0, g0, b0 = x, 0, c
    else:         r0, g0, b0 = c, 0, x
    r = int((r0 + m) * 255.999)
    g = int((g0 + m) * 255.999)
    b = int((b0 + m) * 255.999)
    return f"#{r:02x}{g:02x}{b:02x}"


def _short_addr(address: str) -> str:
    if not address:            return "?"
    if len(address) <= 18:     return address
    pref = 8
    post = 8
    if address[:4] in ("addr", "Ae2"):
        return address[:pref] + "…" + address[-post:]
    return "cred:" + address[:6] + "…" + address[-4:]


def create_address_app(
    result: AddressTraceResult,
    target_address: Optional[str] = None,
) -> dash.Dash:
    app = dash.Dash(__name__, title="Address Interaction Graph")

    # ── aggregate data ──────────────────────────────────────────────
    addr_colours: dict[str, str] = {}
    addr_cex: dict[str, str] = {}
    addr_type_map: dict[str, AddressType] = {}

    for node in result.addresses:
        addr = node.address
        if addr not in addr_colours:
            addr_colours[addr] = _address_colour(addr)
            addr_type_map[addr] = classify_address(addr)
            if node.is_cex:
                addr_cex[addr] = node.cex_name
            else:
                c = identify_cex(addr)
                addr_cex[addr] = c.name if c else ""

    # ── Cytoscape elements ─────────────────────────────────────────
    elements: list[dict] = []

    for node in result.addresses:
        addr = node.address
        hex_c = addr_colours[addr]
        cex_name = addr_cex.get(addr, node.cex_name if node.is_cex else "")
        is_target = node.is_target or (target_address and addr == target_address)
        psize = max(35, min(90, 40 + int(8 * (node.tx_count ** 0.35))))

        addr_type = addr_type_map.get(addr, AddressType.UNKNOWN)

        # Shape by address type
        if addr_type == AddressType.SCRIPT:
            shape = "diamond"
        elif addr_type == AddressType.BYRON:
            shape = "triangle"
        elif addr_type == AddressType.STAKE:
            shape = "hexagon"
        elif addr_type == AddressType.UNKNOWN:
            shape = "square"
        else:
            shape = "ellipse"

        # Border: target = gold, CEX = red, default = subtle
        if is_target:
            border_color = "#ffd700"
            border_width = 5
        elif cex_name:
            border_color = "#f85149"
            border_width = 4
        else:
            border_color = "rgba(255,255,255,.35)"
            border_width = 2

        elements.append({
            "data": {
                "id": addr,
                "bg_color": hex_c,
                "address": addr,
                "address_type": addr_type.value,
                "total_ada": node.total_ada,
                "net_ada": node.net_ada,
                "incoming_ada": node.total_incoming_ada,
                "outgoing_ada": node.total_outgoing_ada,
                "tx_count": node.tx_count,
                "is_target": str(is_target).lower(),
                "cex": cex_name,
            },
            "style": {
                "width": psize, "height": psize,
                "shape": shape,
                "border-width": border_width,
                "border-color": border_color,
                "border-opacity": 1,
            },
        })

    for edge in result.edges:
        # Edge width reflects interaction count
        width = min(8, 1.5 + edge.interaction_count * 0.3)
        elements.append({
            "data": {
                "source": edge.source,
                "target": edge.target,
                "interaction_count": edge.interaction_count,
                "tx_hashes": json.dumps(edge.tx_hashes),
            },
            "style": {
                "width": width,
            },
        })

    # ── Layout: concentric around target ───────────────────────────
    # Simple concentric layout: target at center, others arranged by
    # interaction count
    W, H = 1200, 900
    positions: dict[str, dict] = {}

    # Place target at center
    if target_address and target_address in {n.address for n in result.addresses}:
        positions[target_address] = {"x": W / 2, "y": H / 2}

    # Arrange other addresses in concentric circles by interaction strength
    target_edges = {
        e.target if e.source == target_address else e.source: e.interaction_count
        for e in result.edges
        if e.source == target_address or e.target == target_address
    }

    # Others arranged by how many shared TXs they have with target
    other_addrs = [
        n.address for n in result.addresses
        if n.address != target_address
    ]

    # Sort: most interactive first
    other_addrs.sort(
        key=lambda a: target_edges.get(a, 0),
        reverse=True,
    )

    n_others = len(other_addrs)
    if n_others > 0:
        # Up to 2 concentric rings
        ring1_count = min(n_others, 12)
        ring1 = other_addrs[:ring1_count]
        ring2 = other_addrs[ring1_count:]

        R1 = 180
        for i, addr in enumerate(ring1):
            angle = (2 * 3.14159 * i / len(ring1)) - 3.14159 / 2
            positions[addr] = {
                    "x": W / 2 + R1 * math.cos(angle),
                    "y": H / 2 + R1 * math.sin(angle),
            }

        if ring2:
            R2 = 340
            for i, addr in enumerate(ring2):
                angle = (2 * 3.14159 * i / len(ring2)) - 3.14159 / 2
                positions[addr] = {
                    "x": W / 2 + R2 * math.cos(angle),
                    "y": H / 2 + R2 * math.sin(angle),
                }

    for el in elements:
        eid = el["data"].get("id")
        if eid and eid in positions:
            el["position"] = positions[eid]

    # ── initial zoom/pan ───────────────────────────────────────────
    zoom = 0.65
    pan = {"x": 200, "y": 80}

    # ── stylesheet ──────────────────────────────────────────────────
    stylesheet = [
        {"selector": "node", "style": {
            "background-color": "data(bg_color)",
            "border-opacity": 1,
            "label": "data(address_type)",
            "font-size": "8px",
            "color": "#8b949e",
            "text-valign": "bottom",
            "text-halign": "center",
            "text-margin-y": 4,
        }},
        {"selector": "edge", "style": {
            "line-color": "#3fb95055",
            "target-arrow-color": "#3fb95055",
            "curve-style": "bezier",
            "target-arrow-shape": "triangle",
            "arrow-scale": 1.0,
            "opacity": 0.6,
        }},
    ]

    # ── panels ──────────────────────────────────────────────────────
    bg = "#0d1117"
    panel_style: dict = {
        "background": "rgba(13,17,23,.98)",
        "font-family": "monospace", "font-size": "12px",
        "color": "#c9d1d9", "padding": "14px 16px",
        "overflow-y": "auto", "z-index": 9999,
    }

    # Address legend
    sorted_addrs = sorted(
        result.addresses,
        key=lambda n: n.tx_count,
        reverse=True,
    )
    legend_rows: list[html.Div] = []
    _TYPE_LABEL = {
        AddressType.WALLET: ("W", "#58a6ff"),
        AddressType.SCRIPT: ("S", "#d29922"),
        AddressType.BYRON: ("B", "#bc8cff"),
        AddressType.STAKE: ("K", "#3fb950"),
        AddressType.UNKNOWN: ("?", "#8b949e"),
    }
    for node in sorted_addrs[:20]:
        addr = node.address
        c = addr_colours[addr]
        _type = addr_type_map.get(addr, AddressType.UNKNOWN)
        _tlabel, _tcolor = _TYPE_LABEL.get(_type, ("?", "#8b949e"))
        target_marker = " ★" if node.is_target else ""
        cex_marker = f" [{node.cex_name}]" if node.is_cex else ""
        legend_rows.append(html.Div([
            html.Span(style={
                "display": "inline-block", "width": 11, "height": 11,
                "border-radius": "50%", "background": c,
                "border": "1.5px solid rgba(255,255,255,.35)",
                "margin-right": 6, "vertical-align": "middle",
            }),
            html.Code(_short_addr(addr), style={"font-size": "10px"}),
            html.Span(_tlabel, style={
                "font-size": "8px", "margin-left": 4, "padding": "0 4px",
                "border-radius": 3, "background": _tcolor + "33",
                "color": _tcolor, "border": f"1px solid {_tcolor}66",
                "font-weight": 700,
            }),
            html.Span(f" {node.tx_count}tx", style={
                "font-size": "8px", "color": "#8b949e", "margin-left": 4,
            }),
            html.Span(target_marker + cex_marker, style={
                "font-size": "9px", "color": "#d29922" if node.is_target else "#f85149" if node.is_cex else "#8b949e",
                "margin-left": 2,
            }),
        ], style={"padding": "2px 0"}))

    app.layout = html.Div([
        cyto.Cytoscape(
            id="cytoscape-address", elements=elements,
            layout={"name": "preset"},
            zoom=zoom, pan=pan,
            stylesheet=stylesheet,
            style={"width": "100%", "height": "100vh", "background": bg},
            userZoomingEnabled=True, userPanningEnabled=True,
            minZoom=0.1, maxZoom=8, boxSelectionEnabled=False,
        ),
        # Legend panel
        html.Div(style={
            **panel_style, "position": "fixed", "top": 10, "left": 10,
            "width": 280, "border": "1px solid #30363d", "border-radius": 10,
            "max-height": "80vh", "overflow-y": "auto",
        }, children=[
            html.Div([
                html.Span("Address Graph", style={
                    "font-weight": 700, "font-size": "13px", "color": "#58a6ff",
                }),
                html.Span(f" ({len(result.addresses)} nodes, {len(result.edges)} edges)",
                          style={"font-size": "10px", "color": "#8b949e", "margin-left": 4}),
            ], style={"margin-bottom": 8}),
            html.Details([
                html.Summary([
                    html.Span("Addresses", style={
                        "font-weight": 700, "font-size": "12px", "color": "#c9d1d9",
                    }),
                    html.Span(f" ({len(sorted_addrs)})", style={
                        "font-size": "10px", "color": "#8b949e", "margin-left": 4,
                    }),
                ], style={"cursor": "pointer", "outline": "none",
                          "display": "flex", "align-items": "center"}),
                html.Div(children=legend_rows, style={"margin-top": 6}),
            ], open=True),
        ]),
        # Detail panel
        html.Div(id="detail-panel-addr", children=[
            html.Div([
                html.Span("Address Details", id="detail-title-addr",
                          style={"font-weight": 700, "font-size": "13px",
                                 "color": "#58a6ff"}),
                html.Span("×", id="detail-close-addr", n_clicks=0,
                          style={"cursor": "pointer", "font-size": "18px",
                                 "color": "#8b949e", "user-select": "none",
                                 "padding": "0 4px"}),
            ], style={
                "display": "flex", "justify-content": "space-between",
                "align-items": "center", "margin-bottom": 10,
                "border-bottom": "1px solid #21262d", "padding-bottom": 6,
            }),
            html.Div("Click a node", id="detail-body-addr",
                     style={"color": "#8b949e", "font-size": "11px"}),
        ], style={
            **panel_style, "position": "fixed", "top": 0, "right": 0,
            "height": "100vh", "width": 340,
            "border-left": "1px solid #30363d", "display": "none",
        }),
    ])

    # ── callbacks ───────────────────────────────────────────────────
    @app.callback(
        dash.Output("detail-body-addr", "children"),
        dash.Output("detail-title-addr", "children"),
        dash.Output("detail-panel-addr", "style"),
        dash.Input("cytoscape-address", "tapNodeData"),
        dash.Input("detail-close-addr", "n_clicks"),
        dash.State("detail-panel-addr", "style"),
        prevent_initial_call=True,
    )
    def show_detail(tap_data, n_clicks, current_style):
        ctx = dash.callback_context
        tr = ctx.triggered[0]["prop_id"].split(".")[0] if ctx.triggered else ""

        if tr == "detail-close-addr":
            return (dash.no_update, dash.no_update,
                    {**current_style, "display": "none"})
        if tap_data is None:
            return ("Click a node", "Address Details",
                    {**current_style, "display": "block"})

        cex_name = tap_data.get("cex", "")
        address = tap_data.get("address", "")
        address_type = tap_data.get("address_type", "unknown")
        ada = float(tap_data.get("total_ada", 0))
        net_ada = float(tap_data.get("net_ada", 0))
        incoming_ada = float(tap_data.get("incoming_ada", 0))
        outgoing_ada = float(tap_data.get("outgoing_ada", 0))
        tx_count = int(tap_data.get("tx_count", 0))
        is_target = tap_data.get("is_target", "false") == "true"

        title = "TARGET ADDRESS" if is_target else "Address"
        if cex_name:
            title += f" [{cex_name}]"

        net_color = "#3fb950" if net_ada > 0 else "#f85149" if net_ada < 0 else "#8b949e"
        net_sign = "+" if net_ada > 0 else ""

        children = [
            html.Div([html.Div("ADDRESS"), html.Div(address, style={
                "font-size": "11px", "word-break": "break-all", "margin-top": 2,
            })], style={"margin-bottom": 6}),
            html.Div([
                html.Div([
                    html.Span(f"{ada:.6f}", style={
                        "color": "#f0883e", "font-size": "16px", "font-weight": "bold",
                    }),
                    html.Span(" ADA", style={
                        "color": "#8b949e", "font-size": "10px", "margin-left": 4,
                    }),
                ]),
                html.Div([
                    html.Span("Net: ", style={"color": "#8b949e", "font-size": "10px"}),
                    html.Span(f"{net_sign}{net_ada:.6f}", style={
                        "color": net_color, "font-size": "12px", "font-weight": "bold",
                    }),
                    html.Span("  In: ", style={"color": "#8b949e", "font-size": "10px", "margin-left": 8}),
                    html.Span(f"{incoming_ada:.0f}", style={"color": "#3fb950", "font-size": "11px"}),
                    html.Span("  Out: ", style={"color": "#8b949e", "font-size": "10px", "margin-left": 4}),
                    html.Span(f"{outgoing_ada:.0f}", style={"color": "#f85149", "font-size": "11px"}),
                ], style={"margin-top": 4}),
            ], style={"margin-bottom": 8, "padding": "6px 8px",
                       "background": "#21262d40", "border-radius": 6}),
            html.Div([
                html.Span("TX count: ", style={"color": "#8b949e"}),
                html.Span(str(tx_count), style={"color": "#c9d1d9"}),
            ], style={"margin-bottom": 8}),
            html.Div([
                html.Span("Type: ", style={"color": "#8b949e"}),
                html.Span(address_type, style={"color": "#c9d1d9"}),
            ], style={"margin-bottom": 8}),
        ]

        if cex_name:
            children.insert(1, html.Div([
                html.Span("CEX ", style={"color": "#f85149"}),
                html.Span(cex_name, style={"color": "#c9d1d9"}),
            ], style={"margin-bottom": 8, "padding": "6px 8px",
                       "background": "#f8514915",
                       "border-left": "3px solid #f85149", "border-radius": 4}))

        return (children, title, {**current_style, "display": "block"})

    return app


def start_address_server(
    result: AddressTraceResult,
    target_address: Optional[str] = None,
    port: int = 8050,
    debug: bool = False,
) -> None:
    """Create Dash app and start server (blocking)."""
    app = create_address_app(result, target_address)
    print(f"\n  Address Graph → http://127.0.0.1:{port}")
    print("  Press Ctrl+C to stop\n")
    app.run(host="127.0.0.1", port=port, debug=debug)
