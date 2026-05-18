"""Dash Cytoscape — simple UTXO circles + asset legend panel."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

import dash
import dash_cytoscape as cyto
from dash import html

from utxo_tracer.cex.registry import identify_cex
from utxo_tracer.graph.layout import layout_fr, layout_overlap_remove
from utxo_tracer.models import Asset, OutRef, TraceResult
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
    # Show prefix + "…" + postfix (e.g. addr1x89…rsg0g63z)
    pref = 8
    post = 8
    if address[:4] in ("addr", "Ae2"):
        return address[:pref] + "…" + address[-post:]
    return "cred:" + address[:6] + "…" + address[-4:]


def _short_unit(unit: str) -> str:
    """Short asset unit for legend display."""
    if not unit or unit == ".":
        return "ADA"
    parts = unit.split(".")
    if len(parts) < 2:
        return unit[:12]
    policy, name = parts[0], parts[1]
    # If asset name contains non-hex chars → readable name → short form
    if any(c not in "0123456789abcdefABCDEF" for c in name):
        return name[:12] + ("…" if len(name) > 12 else "")
    # Unreadable hex → show policy[:8]…name[:6]
    return policy[:8] + "…" + name[:6]


def _address_type_badge(addr_type: str) -> html.Div:
    """Render a coloured badge for address type."""
    _BADGE = {
        "wallet":  ("W", "#58a6ff"),
        "script":  ("S", "#d29922"),
        "byron":   ("B", "#bc8cff"),
        "stake":   ("K", "#3fb950"),
        "unknown": ("?", "#8b949e"),
    }
    label, color = _BADGE.get(addr_type, ("?", "#8b949e"))
    return html.Div([
        html.Span(label, style={
            "display": "inline-block", "padding": "2px 7px",
            "border-radius": 4, "background": color + "22",
            "color": color, "border": f"1px solid {color}55",
            "font-size": "10px", "font-weight": 700,
            "text-transform": "uppercase",
        }),
        html.Span(addr_type, style={
            "margin-left": 6, "font-size": "10px", "color": "#8b949e",
        }),
    ])


# ---------------------------------------------------------------------------
# aggregate helpers
# ---------------------------------------------------------------------------

def _aggregate_assets(result: TraceResult) -> dict[str, int]:
    """Aggregate all assets across UTXOs.  Returns {unit: total_qty}."""
    agg: dict[str, int] = {}
    for node in result.nodes:
        for a in node.assets:
            u = a.unit
            if u == "lovelace":
                continue  # ADA tracked separately
            agg[u] = agg.get(u, 0) + a.quantity
    return agg


def create_app(result: TraceResult, start_out_ref: Optional[OutRef] = None,
               cashflow_summary: Optional[Any] = None) -> dash.Dash:
    app = dash.Dash(__name__, title="UTXO Trace")

    # ── cashflow data (if provided) ─────────────────────────────────
    cashflow_cex_addrs: dict[str, dict] = {}  # address → {match_type, cex}
    cashflow_rows: list[html.Div] = []
    if cashflow_summary is not None and hasattr(cashflow_summary, 'matches'):
        from utxo_tracer.cex.models import CashflowSummary
        if isinstance(cashflow_summary, CashflowSummary):
            for m in cashflow_summary.matches:
                for oc in m.onchain_records:
                    addr = oc.address
                    cashflow_cex_addrs[addr] = {
                        "cex": m.cex_record.exchange,
                        "match_type": m.match_type,
                        "confidence": f"{m.confidence*100:.0f}%",
                        "amount": m.cex_record.amount,
                        "record_type": m.cex_record.tx_type,
                    }
            total = len(cashflow_summary.matches)
            unmatched = cashflow_summary.total_unmatched_cex
            rate = cashflow_summary.match_rate
            cashflow_rows = [
                html.Div([
                    html.Span("CexFlow", style={"font-weight": 700, "font-size": "12px", "color": "#f0883e"}),
                    html.Span(f" ({total} matches, {rate*100:.0f}%)",
                              style={"font-size": "10px", "color": "#8b949e", "margin-left": 4}),
                ], style={"margin-bottom": 6}),
            ]
            for addr, info in sorted(cashflow_cex_addrs.items(), key=lambda kv: kv[1]["amount"], reverse=True)[:15]:
                color = "#3fb950" if info["record_type"] == "withdrawal" else "#d29922"
                label = f"{info['cex']} →" if info["record_type"] == "withdrawal" else f"→ {info['cex']}"
                cashflow_rows.append(html.Div([
                    html.Span("●", style={"color": color, "margin-right": 4, "font-size": "8px"}),
                    html.Span(label, style={"font-size": "9px", "font-weight": 600, "color": color}),
                    html.Span(f" {info['amount']:.0f} ADA", style={"font-size": "9px", "color": "#e6edf3", "margin-left": 2}),
                    html.Code(f" {info['confidence']}", style={"font-size": "8px", "color": "#8b949e"}),
                ], style={"padding": "1px 0", "font-size": "9px"}))
            if len(cashflow_cex_addrs) > 15:
                cashflow_rows.append(html.Div(
                    f"... {len(cashflow_cex_addrs) - 15} more",
                    style={"font-size": "8px", "color": "#8b949e", "padding": "2px 0"},
                ))
    else:
        cashflow_rows = [html.Div("No cashflow data", style={"font-size": "9px", "color": "#8b949e"})]


    # ── aggregate data ──────────────────────────────────────────────
    addr_colours: dict[str, str] = {}
    addr_ada: dict[str, float] = {}
    # Pre-compute address-level data (many nodes can share the same address)
    addr_type_cache: dict[str, AddressType] = {}
    addr_cex_cache: dict[str, str] = {}

    for node in result.nodes:
        addr = node.address
        if addr not in addr_colours:
            addr_colours[addr] = _address_colour(addr)
            addr_type_cache[addr] = classify_address(addr)
            c = identify_cex(addr)
            addr_cex_cache[addr] = c.name if c else ""
        addr_ada[addr] = addr_ada.get(addr, 0.0) + node.ada

    all_assets = _aggregate_assets(result)

    # ── Cytoscape elements ─────────────────────────────────────────
    elements: list[dict] = []

    for node in result.nodes:
        nid = node.id
        addr = node.address
        hex_c = addr_colours[addr]
        cex_name = addr_cex_cache[addr]
        is_start = start_out_ref is not None and node.out_ref == start_out_ref
        psize = max(30, min(80, 20 + int(10 * (node.ada ** 0.30))))

        addr_type = addr_type_cache[addr]
        native = [a for a in node.assets if not a.is_lovelace]

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
            shape = "circle"

        # Border colour priority: start > CEX > script/byron > default
        if is_start:
            border_color = "#ffd700"  # gold
            border_width = 5
        elif cex_name:
            border_color = "#f85149"  # red
            border_width = 4
        elif addr_type == AddressType.SCRIPT:
            border_color = "#d29922"  # amber
            border_width = 2
        elif addr_type == AddressType.BYRON:
            border_color = "#bc8cff"  # purple
            border_width = 2
        elif addr_type == AddressType.STAKE:
            border_color = "#3fb950"  # green
            border_width = 2
        else:
            border_color = "rgba(255,255,255,.35)"
            border_width = 2

        elements.append({
            "data": {
                "id": nid,
                "bg_color": hex_c,
                "address": addr,
                "address_type": addr_type.value,
                "ada": node.ada,
                "lovelace": node.lovelace,
                "tx_hash": node.out_ref.tx_hash,
                "output_idx": node.out_ref.output_index,
                "n_assets": len(native),
                "assets": json.dumps([{"unit": a.unit, "qty": a.quantity} for a in native]),
                "is_start": str(is_start).lower(),
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
        elements.append({
            "data": {
                "source": edge.source, "target": edge.target,
                "direction": edge.direction, "tx_hash": edge.tx_hash or "",
            },
        })

    # ── extract layout data ────────────────────────────────────────
    utxo_ids = [e["data"]["id"] for e in elements if "address" in e.get("data", {})]
    utxo_sizes = {e["data"]["id"]: e["style"]["width"] for e in elements
                  if "address" in e.get("data", {})}
    edge_pairs = [(e["data"]["source"], e["data"]["target"])
                  for e in elements if "source" in e.get("data", {})]

    n = len(utxo_ids)
    W, H = 1200, 900
    positions = layout_fr(utxo_ids, utxo_sizes, edge_pairs, n, W, H)
    layout_overlap_remove(positions, utxo_ids, utxo_sizes, edge_pairs, n)

    for el in elements:
        eid = el["data"].get("id")
        if eid and eid in positions:
            el["position"] = positions[eid]

    # ── initial zoom/pan: focus on Start node, don't fit-all ──
    start_pos = None
    for el in elements:
        if el["data"].get("is_start") == "true":
            start_pos = el.get("position")
            break

    zoom = None
    pan = None
    if start_pos:
        # Assume viewport ~1200x800 (minus right detail panel 340px → ~860 wide)
        VIEW_W, VIEW_H = 860, 800
        zoom = 0.75
        pan = {"x": VIEW_W / 2 - start_pos["x"] * zoom,
               "y": VIEW_H / 2 - start_pos["y"] * zoom}

    # ── stylesheet ──────────────────────────────────────────────────
    stylesheet = [
        {"selector": "node", "style": {
            "background-color": "data(bg_color)",
            "border-opacity": 1,
        }},
        {"selector": "edge", "style": {
            "width": 2.5, "line-color": "#3fb95077",
            "target-arrow-color": "#3fb95077", "target-arrow-shape": "triangle",
            "curve-style": "straight",
            "arrow-scale": 1.2, "opacity": 0.85,
        }},
        {"selector": '[direction = "input"]', "style": {
            "line-color": "#f8514977", "target-arrow-color": "#f8514977",
        }},
    ]

    # ── panels ──────────────────────────────────────────────────────
    bg = "#0d1117"
    panel: dict = {
        "background": "rgba(13,17,23,.98)",
        "font-family": "monospace", "font-size": "12px",
        "color": "#c9d1d9", "padding": "14px 16px",
        "overflow-y": "auto", "z-index": 9999,
    }

    # --- address type legend ---
    _TYPE_LEGEND_ITEMS = [
        ("W", "#58a6ff", "Wallet — key hash payment"),
        ("S", "#d29922", "Script — script hash payment"),
        ("B", "#bc8cff", "Byron — legacy bootstrap addr"),
        ("K", "#3fb950", "Stake — reward account"),
        ("?", "#8b949e", "Unknown"),
    ]
    type_legend_rows: list[html.Div] = []
    for _tl, _tc, _tdesc in _TYPE_LEGEND_ITEMS:
        type_legend_rows.append(html.Div([
            html.Span(_tl, style={
                "display": "inline-block", "padding": "1px 6px",
                "border-radius": 3, "background": _tc + "33",
                "color": _tc, "border": f"1px solid {_tc}66",
                "font-size": "9px", "font-weight": 700,
                "margin-right": 6, "line-height": "1.4",
            }),
            html.Span(_tdesc, style={"font-size": "9px", "color": "#8b949e"}),
        ], style={"padding": "2px 0"}))

    # --- address legend ---
    sorted_addrs = sorted(addr_ada.items(), key=lambda kv: kv[1], reverse=True)
    legend_rows: list[html.Div] = []

    _TYPE_LABEL = {
        AddressType.WALLET: ("W", "#58a6ff"),
        AddressType.SCRIPT: ("S", "#d29922"),
        AddressType.BYRON: ("B", "#bc8cff"),
        AddressType.STAKE: ("K", "#3fb950"),
        AddressType.UNKNOWN: ("?", "#8b949e"),
    }

    for addr, _ in sorted_addrs[:20]:
        c = addr_colours[addr]
        _type = addr_type_cache[addr]
        _tlabel, _tcolor = _TYPE_LABEL.get(_type, ("?", "#8b949e"))
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
        ], style={"padding": "2px 0"}))

    # --- asset legend (no per-asset colour dots) ---
    asset_rows: list[html.Div] = []
    for unit, _ in sorted(all_assets.items(), key=lambda kv: kv[1], reverse=True)[:30]:
        if unit == "." or unit == "":
            # ADA is shown in the detail panel, skip from asset list
            continue
        asset_rows.append(html.Div(
            html.Code(_short_unit(unit), style={"font-size": "10px", "color": "#e6edf3"}),
            style={"padding": "2px 0", "clear": "both"},
        ))

    app.layout = html.Div([
        # Cytoscape
        cyto.Cytoscape(
            id="cytoscape", elements=elements,
            layout={"name": "preset"},
            zoom=zoom, pan=pan,
            stylesheet=stylesheet,
            style={"width": "100%", "height": "100vh", "background": bg},
            userZoomingEnabled=True, userPanningEnabled=True,
            minZoom=0.15, maxZoom=6, boxSelectionEnabled=False,
        ),

        # Legend panels (collapsible, type > address > assets)
        html.Div(style={
            **panel, "position": "fixed", "top": 10, "left": 10,
            "width": 260, "border": "1px solid #30363d", "border-radius": 10,
            "max-height": "80vh", "overflow-y": "auto",
        }, children=[
            html.Details([
                html.Summary([
                    html.Span("Type", style={"font-weight": 700, "font-size": "12px",
                                             "color": "#c9d1d9"}),
                ], style={
                    "cursor": "pointer", "outline": "none",
                    "display": "flex", "align-items": "center",
                }),
                html.Div(children=type_legend_rows, style={"margin-top": 6}),
            ], open=True, style={"margin-bottom": 6}),
            html.Details([
                html.Summary([
                    html.Span("Address", style={"font-weight": 700, "font-size": "12px",
                                                 "color": "#58a6ff"}),
                    html.Span(f" ({len(sorted_addrs)})", style={"font-size": "10px",
                              "color": "#8b949e", "margin-left": 4}),
                ], style={
                    "cursor": "pointer", "outline": "none",
                    "display": "flex", "align-items": "center",
                }),
                html.Div(children=legend_rows, style={"margin-top": 6}),
            ], open=True, style={"margin-bottom": 6}),
            html.Details([
                html.Summary([
                    html.Span("Assets", style={"font-weight": 700, "font-size": "12px",
                                                "color": "#f0883e"}),
                    html.Span(f" ({len(all_assets)})", style={"font-size": "10px",
                              "color": "#8b949e", "margin-left": 4}),
                ], style={
                    "cursor": "pointer", "outline": "none",
                    "display": "flex", "align-items": "center",
                }),
                html.Div(children=asset_rows, style={"margin-top": 6}),
            ], open=True, style={"margin-top": 4}),
            # Cashflow section
            html.Details([
                html.Summary([
                    html.Span("Cashflow", style={"font-weight": 700, "font-size": "12px",
                                                  "color": "#f0883e"}),
                    html.Span(f" ({len(cashflow_cex_addrs)})", style={"font-size": "10px",
                              "color": "#8b949e", "margin-left": 4}),
                ], style={
                    "cursor": "pointer", "outline": "none",
                    "display": "flex", "align-items": "center",
                }),
                html.Div(children=cashflow_rows, style={"margin-top": 6}),
            ], open=bool(cashflow_cex_addrs), style={"margin-top": 4}),
        ]),

        # Detail panel (hidden by default)
        html.Div(id="detail-panel-outer", children=[
            html.Div([
                html.Span("UTXO Details", id="detail-title",
                          style={"font-weight": 700, "font-size": "13px",
                                 "color": "#58a6ff"}),
                html.Span("×", id="detail-close", n_clicks=0,
                          style={"cursor": "pointer", "font-size": "18px",
                                 "color": "#8b949e", "user-select": "none",
                                 "padding": "0 4px"}),
            ], style={
                "display": "flex", "justify-content": "space-between",
                "align-items": "center", "margin-bottom": 10,
                "border-bottom": "1px solid #21262d", "padding-bottom": 6,
            }),
            html.Div("Click a node", id="detail-body",
                     style={"color": "#8b949e", "font-size": "11px"}),
        ], style={
            **panel, "position": "fixed", "top": 0, "right": 0,
            "height": "100vh", "width": 340,
            "border-left": "1px solid #30363d", "display": "none",
        }),
    ])

    # ── callbacks ───────────────────────────────────────────────────
    @app.callback(
        dash.Output("detail-body", "children"),
        dash.Output("detail-title", "children"),
        dash.Output("detail-panel-outer", "style"),
        dash.Input("cytoscape", "tapNodeData"),
        dash.Input("detail-close", "n_clicks"),
        dash.State("detail-panel-outer", "style"),
        prevent_initial_call=True,
    )
    def show_detail(tap_data, n_clicks, current_style):
        ctx = dash.callback_context
        tr = ctx.triggered[0]["prop_id"].split(".")[0] if ctx.triggered else ""

        if tr == "detail-close":
            return (dash.no_update, dash.no_update,
                    {**current_style, "display": "none"})
        if tap_data is None:
            return ("Click a node", "UTXO Details",
                    {**current_style, "display": "block"})

        cex_name = tap_data.get("cex", "")
        address = tap_data.get("address", "")
        address_type = tap_data.get("address_type", "unknown")
        ada = float(tap_data.get("ada", 0))
        lovelace = int(tap_data.get("lovelace", 0))
        tx_hash = tap_data.get("tx_hash", "")
        output_idx = tap_data.get("output_idx", "")
        n_assets = int(tap_data.get("n_assets", 0))
        assets_str = tap_data.get("assets", "[]")

        title = "START UTXO" if tap_data.get("is_start") == "true" else "UTXO"
        if cex_name:
            title += f" [{cex_name}]"

        children = [
            html.Div(["ADDRESS", html.Div(address, style={
                "font-size": "11px", "word-break": "break-all", "margin-top": 2,
            })], style={"margin-bottom": 6}),
            html.Div(
                _address_type_badge(address_type),
                style={"margin-bottom": 8},
            ),
            html.Div([
                html.Span(f"{ada:.6f}", style={"color": "#f0883e",
                          "font-size": "16px", "font-weight": "bold"}),
                html.Span(" ADA", style={"color": "#8b949e",
                          "font-size": "10px", "margin-left": 4}),
                html.Br(),
                html.Span(f"{lovelace:,} lovelace",
                          style={"color": "#8b949e", "font-size": "9px"}),
            ], style={"margin-bottom": 8, "padding": "6px 8px",
                       "background": "#21262d40", "border-radius": 6}),
            html.Div([
                "OUTPUT", html.Div(f"{tx_hash}#{output_idx}", style={
                    "font-size": "10px", "word-break": "break-all", "margin-top": 2,
                }),
            ], style={"margin-bottom": 8}),
        ]

        if cex_name:
            children.append(html.Div([
                html.Span("CEX ", style={"color": "#f85149"}),
                html.Span(cex_name, style={"color": "#c9d1d9"}),
            ], style={"margin-bottom": 8, "padding": "6px 8px",
                       "background": "#f8514915",
                       "border-left": "3px solid #f85149", "border-radius": 4}))

        try:
            if assets_str and isinstance(assets_str, str):
                assets_list = json.loads(assets_str)
            else:
                assets_list = []
        except (json.JSONDecodeError, Exception):
            assets_list = []

        if assets_list:
            children.append(html.Div(f"ASSETS ({n_assets})", style={
                "color": "#8b949e", "font-size": "9px", "margin-bottom": 4}))
            divs = []
            for a in assets_list:
                q = a.get("qty", 0)
                qs = f"{q:,}"
                divs.append(html.Div([
                    html.Div(a.get("unit", ""), style={"font-size": "10px",
                             "word-break": "break-all", "color": "#8b949e"}),
                    html.Div(qs, style={"font-size": "12px", "font-weight": "bold"}),
                ], style={"padding": "4px 6px", "margin-bottom": 2,
                          "background": "#21262d40", "border-radius": 4}))
            children.append(html.Div(divs, style={"max-height": "40vh",
                                                  "overflow-y": "auto"}))

        return (children, title, {**current_style, "display": "block"})

    return app


def start_server(result: TraceResult, start_out_ref: Optional[OutRef] = None,
                 port: int = 8050, debug: bool = False,
                 cache_key: str = "",
                 cashflow_summary: Any = None) -> None:
    """Create Dash app and start server (blocking).
    
    If cache_key is provided, loads saved visualization state (node positions,
    zoom, pan) and saves state on exit.
    """
    app = create_app(result, start_out_ref, cashflow_summary=cashflow_summary)

    # ── restore saved viz state ───────────────────────────────────
    if cache_key:
        from utxo_tracer import cache as _cache
        saved = _cache.load_viz_state(cache_key)
        cyto_el = app.layout.children[0]
        if saved.get("node_positions"):
            for el in cyto_el.elements:
                eid = el["data"].get("id")
                if eid and eid in saved["node_positions"]:
                    el["position"] = saved["node_positions"][eid]
        # Restore saved zoom/pan (overrides initial focus-on-start)
        if saved.get("zoom") is not None:
            cyto_el.zoom = saved["zoom"]
            cyto_el.pan = saved.get("pan", {"x": 0, "y": 0})

    print(f"\n  Dash Cytoscape → http://127.0.0.1:{port}")
    print("  Press Ctrl+C to stop\n")

    # ── auto-save viz state on exit ────────────────────────────────
    import atexit
    def _save_viz() -> None:
        if not cache_key:
            return
        from utxo_tracer import cache as _cache
        cyto = app.layout.children[0]
        pos = {}
        for el in cyto.elements:
            eid = el["data"].get("id")
            if eid and "position" in el:
                pos[eid] = el["position"]
        _cache.save_viz_state(cache_key, {
            "node_positions": pos,
            "zoom": getattr(cyto, "zoom", None),
            "pan": getattr(cyto, "pan", None),
        })
    atexit.register(_save_viz)

    app.run(host="127.0.0.1", port=port, debug=debug)
