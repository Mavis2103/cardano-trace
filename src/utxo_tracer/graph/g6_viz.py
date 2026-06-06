"""Self-contained AntV G6 (v5) graph visualization.

Replaces the dash-cytoscape renderer. G6 runs a real client-side physics
layout (d3-force / force-atlas2) plus hierarchical (dagre), radial and
concentric layouts, so large traces render as a readable network instead of
the overlapping mess the old Python-side Fruchterman-Reingold + ``preset``
positions produced.

The whole UI is one static HTML page (G6 loaded from CDN) served by a tiny
stdlib ``http.server``.  A ``/viz-state`` endpoint persists camera + node
positions back into the SQLite cache, keeping parity with the old
``save_viz_state``/``load_viz_state`` feature.

Public entry points keep the old signatures so ``cli.py`` is a drop-in swap:

* ``start_server(result, start_out_ref=..., cache_key=..., cashflow_summary=...)``
* ``start_address_server(result, target_address=..., cache_key=...)``
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from utxo_tracer.cex.registry import identify_cex
from utxo_tracer.models import AddressTraceResult, OutRef, TraceResult
from utxo_tracer.utils import AddressType, classify_address

logger = logging.getLogger(__name__)

# G6 v5 UMD bundle. Pinned to the 5.x line; exposes the global ``G6``.
_G6_CDN = "https://unpkg.com/@antv/g6@5/dist/g6.min.js"

# address-type → G6 node shape (G6 v5 built-in node types)
_SHAPE = {
    AddressType.SCRIPT: "diamond",
    AddressType.BYRON: "triangle",
    AddressType.STAKE: "hexagon",
    AddressType.UNKNOWN: "rect",
    AddressType.WALLET: "circle",
}

_TYPE_COLOR = {
    "wallet": "#58a6ff",
    "script": "#d29922",
    "byron": "#bc8cff",
    "stake": "#3fb950",
    "unknown": "#8b949e",
}


# ---------------------------------------------------------------------------
# colour / formatting helpers (ported from dash_app so colours are identical)
# ---------------------------------------------------------------------------


def _hsl_to_hex(h: int, s: float, lum: float) -> str:
    c = (1 - abs(2 * lum - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = lum - c / 2
    if h < 60:
        r0, g0, b0 = c, x, 0
    elif h < 120:
        r0, g0, b0 = x, c, 0
    elif h < 180:
        r0, g0, b0 = 0, c, x
    elif h < 240:
        r0, g0, b0 = 0, x, c
    elif h < 300:
        r0, g0, b0 = x, 0, c
    else:
        r0, g0, b0 = c, 0, x
    r = int((r0 + m) * 255.999)
    g = int((g0 + m) * 255.999)
    b = int((b0 + m) * 255.999)
    return f"#{r:02x}{g:02x}{b:02x}"


def _address_colour(address: str) -> str:
    h = int(hashlib.sha256(address.encode()).hexdigest()[:8], 16)
    return _hsl_to_hex(h % 360, 0.55, 0.48)


def _short_addr(address: str) -> str:
    if not address:
        return "?"
    if len(address) <= 18:
        return address
    if address[:4] in ("addr", "Ae2"):
        return address[:8] + "…" + address[-8:]
    return "cred:" + address[:6] + "…" + address[-4:]


def _short_id(node_id: str) -> str:
    if "#" in node_id:
        h, idx = node_id.rsplit("#", 1)
    elif node_id.count(":"):
        h, idx = node_id.rsplit(":", 1)
    else:
        return node_id[:12]
    return f"{h[:8]}…{h[-4:]}#{idx}" if len(h) > 14 else node_id


def _short_unit(unit: str) -> str:
    if len(unit) <= 16:
        return unit
    return unit[:10] + "…" + unit[-4:]


# ---------------------------------------------------------------------------
# payload builders
# ---------------------------------------------------------------------------


def build_utxo_payload(
    result: TraceResult,
    start_out_ref: Optional[OutRef] = None,
    cashflow_summary: Any = None,
) -> dict:
    addr_colour: dict[str, str] = {}
    addr_type: dict[str, AddressType] = {}
    addr_cex: dict[str, str] = {}
    addr_ada: dict[str, float] = {}
    all_assets: dict[str, int] = {}

    for node in result.nodes:
        a = node.address
        if a not in addr_colour:
            addr_colour[a] = _address_colour(a)
            addr_type[a] = classify_address(a)
            c = identify_cex(a)
            addr_cex[a] = c.name if c else ""
        addr_ada[a] = addr_ada.get(a, 0.0) + node.ada
        for asset in node.assets:
            if getattr(asset, "is_lovelace", False) or asset.unit in ("", "lovelace"):
                continue
            all_assets[asset.unit] = all_assets.get(asset.unit, 0) + asset.quantity

    start_id = start_out_ref.node_id() if start_out_ref else None
    nodes = []
    for node in result.nodes:
        a = node.address
        atype = addr_type[a]
        cex = addr_cex[a]
        is_start = start_id is not None and node.id == start_id
        size = max(30, min(80, 20 + int(10 * (node.ada**0.30))))
        if is_start:
            size = int(size * 1.6) + 14  # root noticeably larger
            stroke, sw = "#ffd700", 5
        elif cex:
            stroke, sw = "#f85149", 4
        elif atype == AddressType.SCRIPT:
            stroke, sw = "#d29922", 2
        elif atype == AddressType.BYRON:
            stroke, sw = "#bc8cff", 2
        elif atype == AddressType.STAKE:
            stroke, sw = "#3fb950", 2
        else:
            stroke, sw = "rgba(255,255,255,.35)", 2
        native = [
            {"unit": asset.unit, "qty": asset.quantity}
            for asset in node.assets
            if not (
                getattr(asset, "is_lovelace", False) or asset.unit in ("", "lovelace")
            )
        ]
        nodes.append(
            {
                "id": node.id,
                "label": _short_id(node.id),
                "address": a,
                "address_type": atype.value,
                "ada": round(node.ada, 6),
                "lovelace": node.lovelace,
                "tx_hash": node.out_ref.tx_hash,
                "output_idx": node.out_ref.output_index,
                "assets": native,
                "n_assets": len(native),
                "cex": cex,
                "is_start": is_start,
                # always show the label for root + CEX nodes regardless of the
                # global labels toggle / zoom LOD (they are the key landmarks)
                "always_label": bool(is_start or cex),
                "color": addr_colour[a],
                "size": size,
                "shape": _SHAPE.get(atype, "circle"),
                "stroke": stroke,
                "strokeWidth": sw,
            }
        )

    edges = [
        {
            "source": e.source,
            "target": e.target,
            "direction": e.direction,
            "tx_hash": e.tx_hash or "",
            "width": 2.5,
        }
        for e in result.edges
    ]

    legend_addrs = [
        {
            "addr": a,
            "short": _short_addr(a),
            "color": addr_colour[a],
            "type": addr_type[a].value,
        }
        for a, _ in sorted(addr_ada.items(), key=lambda kv: kv[1], reverse=True)[:20]
    ]
    assets = [
        _short_unit(u)
        for u, _ in sorted(all_assets.items(), key=lambda kv: kv[1], reverse=True)[:30]
        if u not in (".", "")
    ]

    return {
        "kind": "utxo",
        "title": "UTXO Trace",
        "start_id": start_id,
        "nodes": nodes,
        "edges": edges,
        "legend_addrs": legend_addrs,
        "assets": assets,
        "cashflow": _cashflow_rows(cashflow_summary),
        "stats": {"nodes": len(nodes), "edges": len(edges)},
    }


def _cashflow_rows(cashflow_summary: Any) -> list[dict]:
    rows: list[dict] = []
    if cashflow_summary is None or not hasattr(cashflow_summary, "matches"):
        return rows
    seen: dict[str, dict] = {}
    for m in cashflow_summary.matches:
        for oc in getattr(m, "onchain_records", []):
            seen[oc.address] = {
                "cex": m.cex_record.exchange,
                "amount": m.cex_record.amount,
                "record_type": m.cex_record.tx_type,
                "confidence": f"{m.confidence * 100:.0f}%",
            }
    for _addr, info in sorted(
        seen.items(), key=lambda kv: kv[1]["amount"], reverse=True
    )[:15]:
        rows.append(info)
    return rows


def build_address_payload(
    result: AddressTraceResult,
    target_address: str = "",
) -> dict:
    target = target_address or result.target_address
    addr_colour: dict[str, str] = {}
    addr_type: dict[str, AddressType] = {}
    for n in result.addresses:
        addr_colour[n.address] = _address_colour(n.address)
        addr_type[n.address] = classify_address(n.address)

    nodes = []
    for n in result.addresses:
        a = n.address
        atype = addr_type[a]
        cex = n.cex_name if n.is_cex else ""
        if not cex:
            c = identify_cex(a)
            cex = c.name if c else ""
        cex_user = "" if cex else getattr(n, "cex_user", "")
        is_target = n.is_target or a == target
        size = max(35, min(90, 40 + int(8 * (n.tx_count**0.35))))
        if is_target:
            size = int(size * 1.6) + 14  # root noticeably larger
            stroke, sw = "#ffd700", 5
        elif cex:
            stroke, sw = "#f85149", 4
        elif cex_user:
            # CEX user (directly transacts with an exchange) — orange ring
            stroke, sw = "#f0883e", 3
        else:
            stroke, sw = "rgba(255,255,255,.35)", 2
        nodes.append(
            {
                "id": a,
                "label": (f"{cex_user} User" if cex_user else _short_addr(a)),
                "address": a,
                "address_type": atype.value,
                "total_ada": round(n.total_ada, 6),
                "net_ada": round(n.net_ada, 6),
                "incoming_ada": round(n.total_incoming_ada, 6),
                "outgoing_ada": round(n.total_outgoing_ada, 6),
                "tx_count": n.tx_count,
                "depth": n.depth,
                "is_target": is_target,
                "is_start": is_target,
                # always-on label for root + CEX + CEX-users (key landmarks)
                "always_label": bool(is_target or cex or cex_user),
                "cex": cex,
                "cex_user": cex_user,
                "color": addr_colour[a],
                "size": size,
                "shape": _SHAPE.get(atype, "ellipse"),
                "stroke": stroke,
                "strokeWidth": sw,
            }
        )

    edges = []
    for e in result.edges:
        edges.append(
            {
                "source": e.source,
                "target": e.target,
                "direction": e.direction_relative_to_target,
                "interaction_count": e.interaction_count,
                "tx_hashes": e.tx_hashes[:25],
                "width": min(8, 1.5 + e.interaction_count * 0.3),
            }
        )

    legend_addrs = [
        {
            "addr": n.address,
            "short": _short_addr(n.address),
            "color": addr_colour[n.address],
            "type": addr_type[n.address].value,
            "tx_count": n.tx_count,
            "is_target": n.is_target or n.address == target,
            "cex": n.cex_name if n.is_cex else "",
            "cex_user": "" if n.is_cex else getattr(n, "cex_user", ""),
        }
        for n in sorted(result.addresses, key=lambda n: n.tx_count, reverse=True)[:20]
    ]

    return {
        "kind": "address",
        "title": "Address Interaction Graph",
        "direction": getattr(result, "direction", "both"),
        "start_id": target,
        "target": target,
        "nodes": nodes,
        "edges": edges,
        "legend_addrs": legend_addrs,
        "assets": [],
        "cashflow": [],
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "transactions": result.total_transactions,
        },
        "error": result.error or "",
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def _render_html(payload: dict) -> str:
    data_json = json.dumps(payload, default=str)
    head = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{payload.get('title', 'Trace')}</title>"
        f"<script src='{_G6_CDN}'></script>"
        "<style>" + _CSS + "</style></head><body>"
    )
    boot = (
        "<script>window.__PAYLOAD__=" + data_json + ";</script>"
    )
    return head + _BODY + boot + "<script>" + _JS + "</script></body></html>"


_CSS = """
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#0d1117;color:#c9d1d9;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
#graph{position:absolute;inset:0}
.panel{position:fixed;background:rgba(13,17,23,.96);border:1px solid #30363d;
  border-radius:10px;padding:10px 12px;font-size:12px;z-index:10;max-height:84vh;
  overflow:auto}
#left{top:10px;left:10px;width:266px}
#toolbar{top:10px;left:50%;transform:translateX(-50%);display:flex;gap:6px;
  align-items:center;padding:6px 10px}
#detail{top:10px;right:10px;width:320px;display:none}
#detail h3{margin:0 0 8px;font-size:13px;color:#58a6ff}
.close{float:right;cursor:pointer;color:#8b949e}
.sec{font-weight:700;font-size:12px;margin:8px 0 4px}
.row{padding:2px 0;display:flex;align-items:center;gap:6px}
.dot{width:11px;height:11px;border-radius:50%;border:1.5px solid rgba(255,255,255,.35);
  flex:none}
.badge{padding:0 5px;border-radius:3px;font-size:9px;font-weight:700}
.kv{padding:6px 8px;background:#21262d55;border-radius:6px;margin-bottom:8px;
  word-break:break-all}
select,input,button{background:#161b22;color:#c9d1d9;border:1px solid #30363d;
  border-radius:6px;padding:4px 8px;font:inherit;font-size:11px}
button{cursor:pointer}
label.f{display:flex;align-items:center;gap:6px;padding:2px 0;cursor:pointer}
code{font-size:10px;color:#e6edf3}
.muted{color:#8b949e;font-size:10px}
"""

_BODY = """
<div id='graph'></div>
<div id='toolbar' class='panel'>
  <span class='sec' style='margin:0'>Layout</span>
  <select id='layout'>
    <option value='d3-force'>force (network)</option>
    <option value='force-atlas2'>force-atlas2</option>
    <option value='antv-dagre'>dagre (flow →)</option>
    <option value='radial'>radial</option>
    <option value='concentric'>concentric</option>
    <option value='circular'>circular</option>
    <option value='grid'>grid</option>
  </select>
  <input id='search' placeholder='search addr / hash' size='16'>
  <button id='labels'>labels</button>
  <button id='fit'>fit</button>
  <button id='png'>PNG</button>
</div>
<div id='left' class='panel'></div>
<div id='detail' class='panel'><span class='close' id='dclose'>✕</span>
  <h3 id='dtitle'>Details</h3><div id='dbody'></div></div>
"""

# Static JS — NOT an f-string, so JS braces stay literal.
_JS = r"""
(function(){
  const P = window.__PAYLOAD__;
  const TYPE_COLOR = {wallet:'#58a6ff',script:'#d29922',byron:'#bc8cff',
                      stake:'#3fb950',unknown:'#8b949e'};
  const KIND = P.kind;
  const CACHE_KEY = (new URLSearchParams(location.search)).get('k') || P.cache_key || '';

  // ---- scale tuning by graph size ----
  // Large graphs need more repulsion + bigger link distance to spread out,
  // and labels hidden by default so the network stays readable.
  const N = P.nodes.length;
  const BIG = N > 120;
  // labels shown only when sparse, or on hover/zoom-in (see LOD below)
  let SHOW_LABELS = !BIG;
  // force strengths grow with N so dense graphs don't collapse into a blob.
  // Stronger repulsion + longer links + bigger collision padding keep large
  // graphs from overlapping into an unreadable hairball.
  const REPULSE = -(220 + 40 * Math.sqrt(N));
  const LINKLEN = Math.min(560, 150 + 8 * Math.sqrt(N));
  const COLLIDE_PAD = BIG ? 30 : 16;

  // collision radius per node so preventOverlap respects actual node size
  const sizeById = {};
  P.nodes.forEach(n => { sizeById[n.id] = n.size || 30; });

  // ---- build G6 data ----
  // root + CEX nodes keep their label on at all times (always_label) so the
  // key landmarks stay readable when labels are toggled off / zoomed out.
  function labelText(n){
    return (SHOW_LABELS || n.always_label) ? (n.label || '') : '';
  }
  function labelStyle(n){
    const on = n.always_label;
    return {
      labelText: labelText(n),
      labelFill: on ? '#fff' : '#c9d1d9',
      labelFontSize: on ? 11 : 9,
      labelFontWeight: on ? 700 : 400,
      labelBackground: true,
      labelBackgroundFill: 'rgba(13,17,23,.7)',
      labelBackgroundRadius: 3,
      labelPlacement: 'bottom',
      labelOffsetY: 3,
    };
  }
  // gold glow/halo for the root wallet, red glow/halo for CEX nodes — a fill
  // shadow stays visible at low zoom where a thin stroke disappears.
  function haloStyle(n){
    if(n.is_start) return {
      halo: true, haloStroke: '#ffd700', haloStrokeWidth: 10, haloOpacity: 0.45,
      shadowColor: '#ffd700', shadowBlur: 24,
      badge: true, badges: [{text:'★', placement:'top-right',
        backgroundFill:'#ffd700', fill:'#0d1117', fontSize:11}],
    };
    if(n.cex) return {
      halo: true, haloStroke: '#f85149', haloStrokeWidth: 9, haloOpacity: 0.42,
      shadowColor: '#f85149', shadowBlur: 20,
      badge: true, badges: [{text:'CEX', placement:'top-right',
        backgroundFill:'#f85149', fill:'#fff', fontSize:8}],
    };
    if(n.cex_user) return {
      halo: true, haloStroke: '#f0883e', haloStrokeWidth: 6, haloOpacity: 0.34,
      shadowColor: '#f0883e', shadowBlur: 12,
      badge: true, badges: [{text:'USER', placement:'top-right',
        backgroundFill:'#f0883e', fill:'#0d1117', fontSize:7}],
    };
    return {};
  }
  const nodes = P.nodes.map(n => ({
    id: n.id,
    type: n.shape || 'circle',
    data: n,
    style: Object.assign({
      size: n.size,
      fill: n.color,
      stroke: n.stroke,
      lineWidth: n.strokeWidth,
    }, labelStyle(n), haloStyle(n)),
  }));
  const edges = P.edges.map((e, i) => {
    // Direction is the single most important signal, so encode it REDUNDANTLY:
    // colour (red=in/green=out) PLUS a non-colour cue (inbound = dashed) so the
    // graph stays readable for red/green colour-blind users.
    const isIn = e.direction === 'input' || e.direction === 'incoming';
    return {
      id: 'e' + i,
      source: e.source,
      target: e.target,
      data: e,
      // quadratic (curved) edges separate parallel/overlapping links so a dense
      // hub doesn't render as one solid bar of lines
      type: 'quadratic',
      style: {
        stroke: isIn ? '#f85149' : '#3fb950',
        lineDash: isIn ? [6, 4] : [0],
        strokeOpacity: BIG ? 0.32 : 0.6,
        lineWidth: e.width || 2,
        endArrow: true,
        endArrowSize: BIG ? 5 : 8,
        curveOffset: 18,
      },
    };
  });

  function layoutOpts(t){
    switch(t){
      case 'd3-force': return {type:'d3-force', preventOverlap:true,
        nodeSize:(d)=> (sizeById[d.id]||30),
        collide:{strength:1, radius:(d)=> (sizeById[d.id]||30)/2 + COLLIDE_PAD},
        link:{distance:LINKLEN}, manyBody:{strength:REPULSE},
        center:{},
        // Live tick animation gives a smooth settle on SMALL graphs. On big
        // graphs continuous ticking murders FPS, so compute the layout in one
        // pass (snap) and force convergence so the simulation stops quickly.
        animation: !BIG,
        alphaMin: BIG ? 0.1 : 0.02,
        alphaDecay: BIG ? 0.06 : 0.028};
      case 'force-atlas2': return {type:'force-atlas2', preventOverlap:true,
        kr: Math.max(40, 10 + N/6), kg:6, nodeSize:(d)=> (sizeById[d.id]||30)};
      case 'antv-dagre': return {type:'antv-dagre', rankdir:'LR',
        nodesep:26, ranksep:Math.max(70, 110 - N/20)};
      case 'radial': return {type:'radial', unitRadius:LINKLEN, linkDistance:LINKLEN,
        preventOverlap:true, nodeSize:(d)=> (sizeById[d.id]||30),
        focusNode: P.start_id || undefined};
      case 'concentric': return {type:'concentric', preventOverlap:true,
        nodeSize:(d)=> (sizeById[d.id]||30)};
      case 'circular': return {type:'circular'};
      case 'grid': return {type:'grid'};
      default: return {type:'d3-force'};
    }
  }

  // Default layout: UTXO traces read best as a left→right flow (dagre); address
  // interaction graphs are hub-and-spoke around the target, so a force network
  // (or radial) reads better. Big UTXO graphs fall back to force to avoid huge
  // dagre rank sprawl.
  const DEFAULT_LAYOUT = KIND === 'utxo' ? (BIG ? 'd3-force' : 'antv-dagre')
                                         : 'd3-force';

  const {Graph} = G6;
  const graph = new Graph({
    container: 'graph',
    autoResize: true,
    background: '#0d1117',
    data: {nodes, edges},
    layout: layoutOpts(DEFAULT_LAYOUT),
    node: {state: {selected: {lineWidth: 6, stroke: '#58a6ff'},
                   inactive: {opacity: 0.18},
                   active: {lineWidth: 4}}},
    edge: {style: {endArrow: true},
           state: {inactive: {strokeOpacity: 0.06},
                   active: {strokeOpacity: 0.9, lineWidth: 3}}},
    behaviors: ['zoom-canvas','drag-canvas','drag-element',
                {type:'click-select', multiple:false},
                {type:'hover-activate', degree: 1, state:'active',
                 inactiveState:'inactive'}],
    // Smooth fade on hover / path-highlight for small graphs. Disabled on big
    // graphs: tweening every node on each mousemove (hover-activate) is the
    // other major FPS sink.
    animation: BIG ? false : {duration: 200, easing: 'ease-in-out'},
  });
  // Fit the viewport AFTER the layout settles, not when render() resolves:
  // force layouts keep moving nodes for several ticks past render, so an
  // immediate fitView() framed the pre-settle positions and left nodes
  // drifting out of view. Fit once on the first 'afterlayout' (with a
  // timeout fallback for instant layouts that fire before this handler).
  let _fittedOnce = false;
  function _fitOnce(){
    if(_fittedOnce) return; _fittedOnce = true;
    try{ graph.fitView({padding: 30}); }catch(e){ try{ graph.fitView(); }catch(_){} }
  }
  graph.on('afterlayout', _fitOnce);
  graph.render().then(()=>{ setTimeout(_fitOnce, 450); });
  window.__graph__ = graph;
  // reflect the chosen default in the layout selector
  try{ document.getElementById('layout').value = DEFAULT_LAYOUT; }catch(e){}

  // ---- label level-of-detail: reveal labels when zoomed in past 1.4x ----
  function setLabels(show){
    if(show === SHOW_LABELS) return;
    SHOW_LABELS = show;
    try{
      graph.updateData({nodes: P.nodes.map(n => ({id:n.id, style:{
        labelText: show ? (n.label||'') : ''}}))});
      graph.draw();
    }catch(e){}
  }
  graph.on('viewportchange', ()=>{
    try{ if(BIG) setLabels(graph.getZoom() >= 1.4); }catch(e){}
  });

  // ---- detail panel ----
  const detail = document.getElementById('detail');
  document.getElementById('dclose').onclick = ()=> detail.style.display='none';
  function fmt(n){ return (typeof n==='number') ? n.toLocaleString() : n; }
  function showDetail(d){
    const t = document.getElementById('dtitle');
    const b = document.getElementById('dbody');
    let title = KIND==='utxo' ? (d.is_start?'START UTXO':'UTXO')
                              : (d.is_target?'TARGET ADDRESS':'ADDRESS');
    if(d.cex) title += ' ['+d.cex+']';
    else if(d.cex_user) title += ' ['+d.cex_user+' User]';
    t.textContent = title;
    const tc = TYPE_COLOR[d.address_type]||'#8b949e';
    let h = "<div class='kv'>"+d.address+"</div>";
    h += "<div class='row'><span class='badge' style='background:"+tc+"33;color:"+tc+
         ";border:1px solid "+tc+"66'>"+d.address_type+"</span></div>";
    if(KIND==='utxo'){
      h += "<div class='kv'><b style='color:#f0883e;font-size:16px'>"+fmt(d.ada)+
           "</b> <span class='muted'>ADA</span><br><span class='muted'>"+
           fmt(d.lovelace)+" lovelace</span></div>";
      h += "<div class='muted'>OUTPUT</div><div class='kv'>"+d.tx_hash+"#"+d.output_idx+"</div>";
      if(d.cex) h += "<div class='kv' style='border-left:3px solid #f85149'>CEX: "+d.cex+"</div>";
      if(d.assets && d.assets.length){
        h += "<div class='sec'>ASSETS ("+d.n_assets+")</div>";
        d.assets.forEach(a=>{ h += "<div class='kv'><code>"+a.unit+"</code><br><b>"+
          fmt(a.qty)+"</b></div>"; });
      }
    } else {
      h += "<div class='kv'>net <b style='color:"+(d.net_ada>=0?'#3fb950':'#f85149')+
        "'>"+fmt(d.net_ada)+"</b> ADA<br>"+
        "<span class='muted'>in "+fmt(d.incoming_ada)+" / out "+fmt(d.outgoing_ada)+
        " / gross "+fmt(d.total_ada)+"</span></div>";
      h += "<div class='kv'>"+fmt(d.tx_count)+" tx · depth "+d.depth+"</div>";
      if(d.cex) h += "<div class='kv' style='border-left:3px solid #f85149'>CEX: "+d.cex+"</div>";
      else if(d.cex_user) h += "<div class='kv' style='border-left:3px solid #f0883e'>"+d.cex_user+" User <span class='muted'>(direct CEX counterparty)</span></div>";
    }
    b.innerHTML = h;
    detail.style.display = 'block';
  }
  // ---- full-path highlight: trace a node's whole flow chain (all ancestors
  //      + descendants), not just its immediate neighbours, so a flow is
  //      followable end-to-end through hub nodes. ----
  const outAdj = {};   // src -> [{t, eid}]
  const inAdj  = {};   // tgt -> [{s, eid}]
  P.edges.forEach((e, i)=>{
    const eid = 'e' + i;
    (outAdj[e.source] = outAdj[e.source] || []).push({n: e.target, eid});
    (inAdj[e.target]  = inAdj[e.target]  || []).push({n: e.source, eid});
  });
  function walk(start, adj, nodeSet, edgeSet){
    const stack=[start];
    while(stack.length){
      const cur=stack.pop();
      (adj[cur]||[]).forEach(({n, eid})=>{
        edgeSet.add(eid);
        if(!nodeSet.has(n)){ nodeSet.add(n); stack.push(n); }
      });
    }
  }
  function clearHighlight(){
    const states={};
    P.nodes.forEach(n=> states[n.id]='');
    P.edges.forEach((e,i)=> states['e'+i]='');
    try{ graph.setElementState(states); }catch(err){}
  }
  function highlightPath(id){
    const nodeSet=new Set([id]), edgeSet=new Set();
    walk(id, outAdj, nodeSet, edgeSet);   // descendants (downstream flow)
    walk(id, inAdj,  nodeSet, edgeSet);   // ancestors (upstream flow)
    const states={};
    P.nodes.forEach(n=> states[n.id] = nodeSet.has(n.id) ? 'active' : 'inactive');
    P.edges.forEach((e,i)=> states['e'+i] = edgeSet.has('e'+i) ? 'active' : 'inactive');
    states[id]='selected';
    try{ graph.setElementState(states); }catch(err){}
  }
  graph.on('node:click', (e)=>{
    try{
      const id=e.target.id;
      const nd=graph.getNodeData(id);
      if(nd) showDetail(nd.data);
      highlightPath(id);
    }
    catch(err){ console.warn(err); }
  });
  graph.on('canvas:click', ()=>{ detail.style.display='none'; clearHighlight(); });

  // ---- left panel: stats + type filter + legends ----
  const left = document.getElementById('left');
  const TYPES = ['wallet','script','byron','stake','unknown'];
  let html = "<div class='sec' style='color:#58a6ff'>"+P.title+"</div>";
  html += "<div class='muted'>"+P.stats.nodes+" nodes · "+P.stats.edges+" edges"+
    (P.stats.transactions!=null?(" · "+P.stats.transactions+" tx"):"")+
    (P.direction&&P.direction!=='both'?(" · "+P.direction):"")+"</div>";
  if(P.error) html += "<div class='kv' style='border-left:3px solid #d29922;color:#d29922'>"+P.error+"</div>";
  html += "<div class='sec'>Landmarks</div>";
  html += "<div class='row'><span class='dot' style='background:#ffd700;box-shadow:0 0 6px #ffd700'></span>"+
    "<span>★ "+(KIND==='utxo'?'start UTXO':'root wallet')+"</span></div>";
  html += "<div class='row'><span class='dot' style='background:#f85149;box-shadow:0 0 6px #f85149'></span>"+
    "<span>CEX address</span></div>";
  if(KIND!=='utxo') html += "<div class='row'><span class='dot' style='background:#f0883e;box-shadow:0 0 5px #f0883e'></span>"+
    "<span>CEX user (direct counterparty)</span></div>";
  html += "<div class='row'><span class='muted'>solid = outflow · dashed = inflow</span></div>";
  html += "<div class='sec'>Type filter</div><div id='types'>";
  TYPES.forEach(t=>{ html += "<label class='f'><input type='checkbox' checked value='"+t+
    "'><span class='badge' style='background:"+TYPE_COLOR[t]+"33;color:"+TYPE_COLOR[t]+
    ";border:1px solid "+TYPE_COLOR[t]+"66'>"+t+"</span></label>"; });
  html += "</div>";
  html += "<div class='sec'>Addresses</div>";
  (P.legend_addrs||[]).forEach(a=>{
    const star = a.is_target?' ★':''; const cx = a.cex?(' ['+a.cex+']'):'';
    const cu = (!a.cex && a.cex_user)?(' ['+a.cex_user+' User]'):'';
    html += "<div class='row'><span class='dot' style='background:"+a.color+"'></span>"+
      "<code>"+a.short+"</code><span class='muted'>"+
      (a.tx_count!=null?(a.tx_count+'tx'):'')+star+cx+cu+"</span></div>";
  });
  if(P.assets && P.assets.length){
    html += "<div class='sec'>Assets</div>";
    P.assets.forEach(u=>{ html += "<div class='row'><code>"+u+"</code></div>"; });
  }
  if(P.cashflow && P.cashflow.length){
    html += "<div class='sec' style='color:#f0883e'>CexFlow</div>";
    P.cashflow.forEach(c=>{ const col=c.record_type==='withdrawal'?'#3fb950':'#d29922';
      html += "<div class='row'><span style='color:"+col+"'>●</span><span>"+c.cex+
        "</span><span class='muted'>"+Math.round(c.amount)+" ADA "+c.confidence+"</span></div>"; });
  }
  left.innerHTML = html;

  function applyFilter(){
    const active = new Set(
      [...document.querySelectorAll('#types input:checked')].map(i=>i.value));
    // hidden node set drives BOTH node + edge visibility — an edge whose
    // source or target is filtered out must hide too, otherwise dangling
    // arrows point at nothing.
    const hidden = new Set();
    P.nodes.forEach(n=>{
      const show = active.has(n.address_type);
      if(!show) hidden.add(n.id);
      try{ graph.setElementVisibility(n.id, show ? 'visible' : 'hidden'); }catch(err){}
    });
    P.edges.forEach((e, i)=>{
      const vis = (hidden.has(e.source) || hidden.has(e.target)) ? 'hidden' : 'visible';
      try{ graph.setElementVisibility('e'+i, vis); }catch(err){}
    });
    // re-run the current layout so the remaining visible nodes recompact
    // instead of leaving holes where filtered nodes used to sit
    try{ graph.draw(); graph.layout(); }catch(err){}
  }
  document.getElementById('types').addEventListener('change', applyFilter);

  // ---- toolbar ----
  document.getElementById('layout').addEventListener('change', (e)=>{
    try{
      graph.setLayout(layoutOpts(e.target.value));
      graph.layout().then(()=>{ try{ graph.fitView({padding: 30}); }catch(_){} });
    }
    catch(err){ console.warn('layout', err); }
  });
  document.getElementById('fit').onclick = ()=>{ try{ graph.fitView(); }catch(err){} };
  document.getElementById('labels').onclick = ()=>{ setLabels(!SHOW_LABELS); };
  document.getElementById('png').onclick = async ()=>{
    try{
      const url = await graph.toDataURL({mode:'overall'});
      const a=document.createElement('a'); a.href=url; a.download='trace.png'; a.click();
    }catch(err){ console.warn('png', err); }
  };
  const search = document.getElementById('search');
  search.addEventListener('keydown', (e)=>{
    if(e.key!=='Enter') return;
    const q = search.value.trim().toLowerCase(); if(!q) return;
    const hit = P.nodes.find(n=> (n.address||'').toLowerCase().includes(q) ||
      (n.id||'').toLowerCase().includes(q) || (n.tx_hash||'').toLowerCase().includes(q));
    if(hit){ try{ graph.focusElement(hit.id);
      graph.setElementState(hit.id,'selected'); showDetail(hit); }catch(err){} }
  });

  // ---- viz-state persistence (best effort, never breaks rendering) ----
  // NOTE: we intentionally do NOT re-apply a saved absolute zoom on load. The
  // old code called graph.zoomTo(savedZoom) WITHOUT restoring the matching pan,
  // which stranded the camera off the graph (blank/empty viewport) and also
  // raced the initial fitView. The auto-fit above always frames the graph
  // correctly; state is still saved below for potential future full restore.
  if(CACHE_KEY){
    window.addEventListener('beforeunload', ()=>{
      try{
        const positions = {};
        graph.getNodeData().forEach(n=>{
          const p = graph.getElementPosition(n.id);
          if(p) positions[n.id] = {x:p[0], y:p[1]};
        });
        const body = JSON.stringify({zoom: graph.getZoom(), node_positions: positions});
        navigator.sendBeacon('/viz-state?k='+encodeURIComponent(CACHE_KEY),
          new Blob([body], {type:'application/json'}));
      }catch(err){}
    });
  }
})();
"""


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------


def _serve(html: str, port: int, cache_key: str) -> None:
    from utxo_tracer import cache as _cache

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # silence access log
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def do_GET(self) -> None:
            if self.path.startswith("/viz-state"):
                key = cache_key
                try:
                    state = _cache.load_viz_state(key) if key else {}
                except Exception:
                    state = {}
                self._send(200, json.dumps(state).encode(), "application/json")
                return
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self) -> None:
            if not self.path.startswith("/viz-state"):
                self._send(404, b"", "text/plain")
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if cache_key:
                try:
                    _cache.save_viz_state(cache_key, json.loads(raw or b"{}"))
                except Exception:
                    logger.debug("viz-state save failed", exc_info=True)
            self._send(204, b"", "text/plain")

    httpd: Optional[ThreadingHTTPServer] = None
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        raise RuntimeError(f"No free port in {port}..{port + 20}")

    url = f"http://127.0.0.1:{port}/"
    print(f"\n  G6 graph → {url}")
    print("  Press Ctrl+C to stop\n")
    threading.Thread(
        target=lambda: webbrowser.open(url), daemon=True
    ).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# public entry points (drop-in for the old dash server functions)
# ---------------------------------------------------------------------------


def start_server(
    result: TraceResult,
    start_out_ref: Optional[OutRef] = None,
    port: int = 8050,
    debug: bool = False,
    cache_key: str = "",
    cashflow_summary: Any = None,
) -> None:
    payload = build_utxo_payload(result, start_out_ref, cashflow_summary)
    payload["cache_key"] = cache_key
    _serve(_render_html(payload), port, cache_key)


def start_address_server(
    result: AddressTraceResult,
    target_address: str = "",
    port: int = 8050,
    debug: bool = False,
    cache_key: str = "",
) -> None:
    payload = build_address_payload(result, target_address)
    payload["cache_key"] = cache_key
    _serve(_render_html(payload), port, cache_key)
