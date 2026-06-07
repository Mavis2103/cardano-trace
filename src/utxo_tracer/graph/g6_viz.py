"""Self-contained WebGL graph visualization (Sigma.js v3).

Renders trace graphs with Sigma.js v3 (WebGL) + graphology +
graphology-layout-forceatlas2, loaded as ES modules from a CDN. WebGL draws
tens of thousands of nodes at interactive frame-rates with NO node cap and no
clustering — every node is shown. The force-directed layout runs ONCE (chunked,
behind a progress bar) and the resulting node positions are persisted to the
SQLite cache via ``/viz-state``, so re-opening the same trace is instant.

Serving model — a tiny stdlib ``http.server`` exposes three routes:

* ``/``          → small static HTML shell (no inlined data)
* ``/data``      → the graph payload as JSON, fetched async by the client so
                   multi-MB / 50k-node graphs never block HTML parsing
* ``/viz-state`` → GET loads / POST saves camera + node positions in SQLite

Public entry points keep the old signatures so ``cli.py`` is a drop-in swap:

* ``start_server(result, start_out_ref=..., cache_key=..., cashflow_summary=...)``
* ``start_address_server(result, target_address=..., cache_key=...)``
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from utxo_tracer.cex.registry import identify_cex
from utxo_tracer.models import AddressTraceResult, OutRef, TraceResult
from utxo_tracer.utils import AddressType, classify_address

logger = logging.getLogger(__name__)


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

    # O(1) node lookup — a linear scan per edge here was O(E·N), the dominant
    # cost when building payloads for large traces (tens of thousands of edges).
    node_by_id = {n.id: n for n in result.nodes}

    edges = []
    for e in result.edges:
        source_node = node_by_id.get(e.source)
        amount = source_node.ada if source_node else 0

        # Build multi-asset label components and detail data
        asset_labels: list[str] = []
        native_assets: list[dict] = []
        if source_node:
            for asset in source_node.assets:
                if asset.is_lovelace:
                    continue
                aname = (
                    asset.asset_name
                    if len(asset.asset_name) <= 8
                    else asset.asset_name[:6] + "…"
                )
                asset_labels.append(f"{asset.quantity:,} {aname}")
                policy_display = (
                    asset.policy_id[:16] + "…"
                    if len(asset.policy_id) > 16
                    else asset.policy_id
                )
                native_assets.append(
                    {
                        "name": asset.asset_name,
                        "quantity": asset.quantity,
                        "policy": policy_display,
                    }
                )

        arrow = "→" if e.direction == "output" else "←"
        if asset_labels:
            assets_str = " + ".join(asset_labels[:2])
            if len(asset_labels) > 2:
                assets_str += f" +{len(asset_labels) - 2}"
            edge_label = f"{arrow} {amount:.2f} ADA + {assets_str}"
        else:
            edge_label = f"{arrow} {amount:.2f} ADA"

        edges.append(
            {
                "source": e.source,
                "target": e.target,
                "direction": e.direction,
                "tx_hash": e.tx_hash or "",
                "amount": amount,
                "native_assets": native_assets,
                "label": edge_label,
                "width": 2.5,
            }
        )

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

    # O(1) lookups — two linear scans per edge here was 2·O(E·N), the dominant
    # cost when building large address-interaction payloads.
    node_by_addr = {n.address: n for n in result.addresses}

    edges = []
    for e in result.edges:
        source_node = node_by_addr.get(e.source)
        target_node = node_by_addr.get(e.target)
        net_ada = (
            source_node.net_ada - target_node.net_ada
            if (source_node and target_node)
            else 0
        )
        edge_label = f"net {net_ada:+.2f} ADA ({e.interaction_count} tx)"
        edges.append(
            {
                "source": e.source,
                "target": e.target,
                "direction": e.direction_relative_to_target,
                "interaction_count": e.interaction_count,
                "tx_hashes": e.tx_hashes[:25],
                "net_ada": net_ada,
                "label": edge_label,
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
    # The graph payload is served separately from ``/data`` and fetched async by
    # the client, so multi-MB / 50k-node traces don't bloat the HTML, block the
    # parser, or get held twice in memory. The HTML shell stays tiny.
    title = payload.get("title", "Trace")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        "<style>" + _CSS + "</style></head><body>"
        + _BODY
        + "<script>" + _JS + "</script></body></html>"
    )


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
  align-items:center;padding:6px 10px;flex-wrap:wrap;max-width:60vw}
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
#overlay{position:fixed;inset:0;z-index:50;display:flex;align-items:center;
  justify-content:center;flex-direction:column;gap:12px;background:rgba(13,17,23,.92);
  color:#c9d1d9;font-size:13px}
#bar{width:240px;height:6px;background:#21262d;border-radius:4px;overflow:hidden}
#barfill{height:100%;width:0;background:#58a6ff;transition:width .12s}
#err{position:fixed;inset:0;z-index:60;display:none;align-items:center;
  justify-content:center;color:#f85149;padding:30px;text-align:center;font-size:13px}
"""

_BODY = """
<div id='graph'></div>
<div id='overlay'><div id='ostat'>loading graph engine…</div>
  <div id='bar'><div id='barfill'></div></div>
  <div class='muted' id='ocount'></div></div>
<div id='err'></div>
<div id='toolbar' class='panel'>
  <input id='search' placeholder='search addr / hash' size='16'>
  <button id='relayout'>re-layout</button>
  <button id='labels'>labels</button>
  <button id='fit'>fit</button>
  <button id='png'>PNG</button>
  <button id='clear'>clear</button>
</div>
<div id='left' class='panel'></div>
<div id='detail' class='panel'><span class='close' id='dclose'>✕</span>
  <h3 id='dtitle'>Details</h3><div id='dbody'></div></div>
"""

# Static JS (NOT an f-string — JS braces stay literal). The renderer is
# Sigma.js v3 (WebGL) + graphology + graphology-layout-forceatlas2, loaded as
# ESM from esm.sh. WebGL keeps tens of thousands of nodes at interactive FPS;
# the force layout runs ONCE (chunked, with a progress bar) and the resulting
# positions are cached to SQLite via /viz-state, so re-opening a trace is
# instant. There is no node cap and no clustering — every node is drawn.
_JS = r"""
(async function(){
  const ESM = 'https://esm.sh/';
  const $ = (id)=>document.getElementById(id);
  function fatal(msg){
    try{ $('overlay').style.display='none';
      const e=$('err'); e.style.display='flex';
      e.textContent='Visualization failed to load: '+msg+
        '\n\n(The graph engine is loaded from esm.sh — check your network.)';
    }catch(_){}
  }
  window.addEventListener('error', (ev)=>{ /* keep going; surfaced per-stage */ });

  // ---- load engine (ESM) ----
  let graphologyMod, fa2Mod, sigmaMod;
  try {
    [graphologyMod, fa2Mod, sigmaMod] = await Promise.all([
      import(ESM+'graphology@0.25.4'),
      import(ESM+'graphology-layout-forceatlas2@0.10.1'),
      import(ESM+'sigma@3.0.0'),
    ]);
  } catch(err){ fatal('engine import: '+err.message); return; }
  const Graph = graphologyMod.MultiDirectedGraph ||
                (graphologyMod.default && graphologyMod.default.MultiDirectedGraph) ||
                graphologyMod.default;
  const FA2 = fa2Mod.default || fa2Mod;
  const Sigma = sigmaMod.default || sigmaMod;
  if(!Graph || !FA2 || !FA2.assign || !Sigma){ fatal('engine exports missing'); return; }

  // ---- fetch payload (served separately so huge graphs don't block parse) ----
  let P;
  try { P = await (await fetch('/data')).json(); }
  catch(err){ P = window.__PAYLOAD__; }
  if(!P){ fatal('no graph data'); return; }

  const KIND = P.kind;
  const CACHE_KEY = (new URLSearchParams(location.search)).get('k') || P.cache_key || '';
  const TYPE_COLOR = {wallet:'#58a6ff',script:'#d29922',byron:'#bc8cff',
                      stake:'#3fb950',unknown:'#8b949e'};
  const N = P.nodes.length;
  const BIG = N > 400;

  // ---- build graphology graph ----
  const g = new Graph();
  // size: payload uses ~30..90 px (G6 scale); Sigma wants smaller units.
  function nsize(n){ return Math.max(3, (n.size||30)/5); }
  P.nodes.forEach(n=>{
    if(g.hasNode(n.id)) return;
    g.addNode(n.id, {
      x: Math.cos(g.order) * (1 + g.order*0.001),   // deterministic spread seed
      y: Math.sin(g.order) * (1 + g.order*0.001),
      size: nsize(n),
      color: n.color || '#8b949e',
      label: n.label || '',
      atype: n.address_type || 'unknown',
      always_label: !!n.always_label,
      is_start: !!n.is_start,
      cex: n.cex || '', cex_user: n.cex_user || '',
      raw: n,
    });
  });
  // edge colour: CEX magenta > cex-user orange > direction (in red / out green)
  const cexNodes = new Set(P.nodes.filter(n=>n.cex).map(n=>n.id));
  const cexUserNodes = new Set(P.nodes.filter(n=>n.cex_user).map(n=>n.id));
  P.edges.forEach((e,i)=>{
    if(!g.hasNode(e.source) || !g.hasNode(e.target)) return;
    const touchesCex = cexNodes.has(e.source)||cexNodes.has(e.target);
    const touchesUser = !touchesCex && (cexUserNodes.has(e.source)||cexUserNodes.has(e.target));
    let col, w=(e.width||2)/2, pre='';
    if(touchesCex){ col='#d26cff'; w*=1.8; pre='CEX: '; }
    else if(touchesUser){ col='#ffa657'; w*=1.4; pre='USER: '; }
    else { const isIn=(e.direction==='input'||e.direction==='incoming');
           col=isIn?'#f85149':'#3fb950'; }
    try{
      g.addEdgeWithKey('e'+i, e.source, e.target, {
        size: Math.max(0.6, w),
        color: col,
        type: 'arrow',
        label: pre + (e.label||''),
        baseColor: col, baseSize: Math.max(0.6, w),
        raw: e,
      });
    }catch(_){ /* parallel/dup edge key collisions: skip safely */ }
  });

  // ---- force layout: run ONCE, chunked with progress, unless cached ----
  function showOverlay(t){ $('overlay').style.display='flex'; if(t)$('ostat').textContent=t; }
  function setBar(p,label){ $('barfill').style.width=p+'%'; if(label)$('ocount').textContent=label; }
  function hideOverlay(){ $('overlay').style.display='none'; }

  async function applyCachedOrLayout(){
    // try cached positions first → instant open
    let cached=null;
    try{ cached = await (await fetch('/viz-state'+(CACHE_KEY?('?k='+encodeURIComponent(CACHE_KEY)):''))).json(); }catch(_){}
    const pos = cached && cached.node_positions;
    let applied=0;
    if(pos){
      g.forEachNode(id=>{ const p=pos[id]; if(p && isFinite(p.x) && isFinite(p.y)){ g.setNodeAttribute(id,'x',p.x); g.setNodeAttribute(id,'y',p.y); applied++; } });
    }
    if(applied >= g.order*0.8 && g.order>0){ return; }  // enough cached → skip FA2

    showOverlay('computing layout…');
    const total = N>6000?100 : N>2000?160 : N>500?260 : 360;
    const settings = Object.assign(FA2.inferSettings(g), {
      barnesHutOptimize: N>500, adjustSizes: true, gravity: 1.2, scalingRatio: 12,
    });
    const batch = 10; let done=0;
    while(done<total){
      const it = Math.min(batch, total-done);
      try{ FA2.assign(g, {iterations: it, settings}); }catch(err){ break; }
      done+=it;
      setBar(Math.round(done/total*100), N.toLocaleString()+' nodes · '+P.edges.length.toLocaleString()+' edges');
      await new Promise(r=>requestAnimationFrame(r));
    }
    // guard against NaN (isolated nodes)
    g.forEachNode((id,a)=>{ if(!isFinite(a.x)||!isFinite(a.y)){ g.setNodeAttribute(id,'x',Math.random()*100-50); g.setNodeAttribute(id,'y',Math.random()*100-50); } });
  }
  try{ await applyCachedOrLayout(); }catch(err){ /* render anyway with seed positions */ }

  // ---- interaction state ----
  let hoveredNode=null, selectedNode=null;
  let pathNodes=null, pathEdges=null;   // null = no highlight
  const hiddenTypes=new Set();

  // ---- Sigma renderer (WebGL) ----
  let renderer;
  try {
    renderer = new Sigma(g, $('graph'), {
      renderLabels: true,
      renderEdgeLabels: true,
      labelDensity: BIG?0.06:1,
      labelGridCellSize: 130,
      labelRenderedSizeThreshold: BIG?15:6,
      labelColor: {color:'#c9d1d9'},
      labelFont: 'ui-monospace, Menlo, monospace',
      defaultEdgeType: 'arrow',
      enableEdgeEvents: true,
      zIndex: true,
      minCameraRatio: 0.02,
      maxCameraRatio: 50,
      nodeReducer: (node, data)=>{
        const res = Object.assign({}, data);
        if(hiddenTypes.has(data.atype)){ res.hidden=true; return res; }
        if(data.always_label) res.forceLabel=true;
        if(pathNodes){
          if(pathNodes.has(node)){ res.zIndex=2; res.forceLabel = data.always_label || node===selectedNode; }
          else { res.color='#262b33'; res.label=''; res.forceLabel=false; res.size=Math.max(1.5,(data.size||3)*0.55); res.zIndex=0; }
        }
        if(node===hoveredNode || node===selectedNode){ res.zIndex=3; res.forceLabel=true; res.size=(res.size||3)*1.18; }
        return res;
      },
      edgeReducer: (edge, data)=>{
        const res = Object.assign({}, data);
        const s=g.source(edge), t=g.target(edge);
        if(hiddenTypes.size && (hiddenTypes.has(g.getNodeAttribute(s,'atype')) || hiddenTypes.has(g.getNodeAttribute(t,'atype')))){ res.hidden=true; return res; }
        if(pathEdges){
          if(!pathEdges.has(edge)){ res.hidden=true; return res; }
          res.color=data.baseColor; res.size=(data.baseSize||1)*1.7; res.zIndex=2;
        }
        // edge labels declutter: only the hovered node's incident edges show them
        res.label = (hoveredNode && (s===hoveredNode||t===hoveredNode)) ? data.label : '';
        return res;
      },
    });
  } catch(err){ fatal('renderer init: '+err.message); return; }
  window.__sigma__ = renderer;
  hideOverlay();
  try{ renderer.getCamera().animatedReset({duration:1}); }catch(_){}

  // ---- full-path highlight (all ancestors + descendants through hubs) ----
  function walk(start, dir, nodeSet, edgeSet){
    const stack=[start];
    const fn = dir==='out' ? 'forEachOutboundEdge' : 'forEachInboundEdge';
    while(stack.length){
      const cur=stack.pop();
      g[fn](cur, (edge, attr, src, tgt)=>{
        edgeSet.add(edge);
        const nb = dir==='out' ? tgt : src;
        if(!nodeSet.has(nb)){ nodeSet.add(nb); stack.push(nb); }
      });
    }
  }
  function computePath(start){
    pathNodes=new Set([start]); pathEdges=new Set();
    walk(start,'out',pathNodes,pathEdges);
    walk(start,'in',pathNodes,pathEdges);
  }
  function clearHighlight(){ selectedNode=null; pathNodes=null; pathEdges=null; }

  // ---- detail panel ----
  const detail=$('detail');
  $('dclose').onclick=()=>{ detail.style.display='none'; };
  function fmt(n){ return (typeof n==='number') ? n.toLocaleString() : n; }
  function showDetail(d){
    const t=$('dtitle'), b=$('dbody');
    let title = KIND==='utxo' ? (d.is_start?'START UTXO':'UTXO')
                              : (d.is_target?'TARGET ADDRESS':'ADDRESS');
    if(d.cex) title+=' ['+d.cex+']'; else if(d.cex_user) title+=' ['+d.cex_user+' User]';
    t.textContent=title;
    const tc=TYPE_COLOR[d.address_type]||'#8b949e';
    let h="<div class='kv'>"+d.address+"</div>";
    h+="<div class='row'><span class='badge' style='background:"+tc+"33;color:"+tc+";border:1px solid "+tc+"66'>"+d.address_type+"</span></div>";
    if(KIND==='utxo'){
      h+="<div class='kv'><b style='color:#f0883e;font-size:16px'>"+fmt(d.ada)+"</b> <span class='muted'>ADA</span><br><span class='muted'>"+fmt(d.lovelace)+" lovelace</span></div>";
      h+="<div class='muted'>OUTPUT</div><div class='kv'>"+d.tx_hash+"#"+d.output_idx+"</div>";
      if(d.cex) h+="<div class='kv' style='border-left:3px solid #f85149'>CEX: "+d.cex+"</div>";
      if(d.assets && d.assets.length){
        h+="<div class='sec'>ASSETS ("+d.n_assets+")</div>";
        d.assets.forEach(a=>{ h+="<div class='kv'><code>"+a.unit+"</code><br><b>"+fmt(a.qty)+"</b></div>"; });
      }
    } else {
      h+="<div class='kv'>net <b style='color:"+(d.net_ada>=0?'#3fb950':'#f85149')+"'>"+fmt(d.net_ada)+"</b> ADA<br><span class='muted'>in "+fmt(d.incoming_ada)+" / out "+fmt(d.outgoing_ada)+" / gross "+fmt(d.total_ada)+"</span></div>";
      h+="<div class='kv'>"+fmt(d.tx_count)+" tx · depth "+d.depth+"</div>";
      if(d.cex) h+="<div class='kv' style='border-left:3px solid #f85149'>CEX: "+d.cex+"</div>";
      else if(d.cex_user) h+="<div class='kv' style='border-left:3px solid #f0883e'>"+d.cex_user+" User <span class='muted'>(direct CEX counterparty)</span></div>";
    }
    b.innerHTML=h; detail.style.display='block';
  }
  function showEdgeDetail(e){
    const t=$('dtitle'), b=$('dbody');
    t.textContent='Transaction Edge';
    let h="<div class='kv'>";
    h+="<b>From:</b> "+e.source+"<br><b>To:</b> "+e.target+"<br>";
    if(e.tx_hash) h+="<b>TX:</b> <code>"+e.tx_hash+"</code><br>";
    if(e.amount!=null) h+="<b>Amount:</b> "+Number(e.amount).toFixed(6)+" ADA<br>";
    if(e.interaction_count) h+="<b>Interactions:</b> "+e.interaction_count+"<br>";
    if(e.net_ada!=null && KIND==='address') h+="<b>Net ADA:</b> "+Number(e.net_ada).toFixed(6)+" ADA<br>";
    if(e.direction) h+="<b>Direction:</b> "+e.direction;
    if(e.native_assets && e.native_assets.length){
      h+="<br><b>Native Assets:</b>";
      e.native_assets.forEach(a=>{ h+="<br>&nbsp;&nbsp;• "+Number(a.quantity).toLocaleString()+" "+a.name+" ("+a.policy+")"; });
    }
    h+="</div>"; b.innerHTML=h; detail.style.display='block';
  }

  // ---- events ----
  renderer.on('enterNode', ({node})=>{ hoveredNode=node; renderer.refresh(); });
  renderer.on('leaveNode', ()=>{ hoveredNode=null; renderer.refresh(); });
  renderer.on('clickNode', ({node})=>{ selectedNode=node; computePath(node); showDetail(g.getNodeAttribute(node,'raw')); renderer.refresh(); });
  renderer.on('clickEdge', ({edge})=>{ showEdgeDetail(g.getEdgeAttribute(edge,'raw')); });
  renderer.on('clickStage', ()=>{ clearHighlight(); detail.style.display='none'; renderer.refresh(); });
  document.addEventListener('keydown', (e)=>{ if(e.key==='Escape'){ clearHighlight(); detail.style.display='none'; renderer.refresh(); } });

  // ---- left panel ----
  const left=$('left');
  const TYPES=['wallet','script','byron','stake','unknown'];
  let html="<div class='sec' style='color:#58a6ff'>"+P.title+"</div>";
  html+="<div class='muted'>"+P.stats.nodes+" nodes · "+P.stats.edges+" edges"+
    (P.stats.transactions!=null?(" · "+P.stats.transactions+" tx"):"")+
    (P.direction&&P.direction!=='both'?(" · "+P.direction):"")+"</div>";
  if(P.error) html+="<div class='kv' style='border-left:3px solid #d29922;color:#d29922'>"+P.error+"</div>";
  html+="<div class='sec'>Landmarks</div>";
  html+="<div class='row'><span class='dot' style='background:#ffd700;box-shadow:0 0 6px #ffd700'></span><span>★ "+(KIND==='utxo'?'start UTXO':'root wallet')+"</span></div>";
  html+="<div class='row'><span class='dot' style='background:#f85149;box-shadow:0 0 6px #f85149'></span><span>CEX address</span></div>";
  if(KIND!=='utxo') html+="<div class='row'><span class='dot' style='background:#f0883e;box-shadow:0 0 5px #f0883e'></span><span>CEX user (direct counterparty)</span></div>";
  html+="<div class='row'><span class='muted'>green = outflow · red = inflow · magenta = CEX</span></div>";
  html+="<div class='sec' style='color:#58a6ff'>Knowledge Graph</div>";
  html+="<div class='muted'>Nodes represent "+(KIND==='utxo'?"UTXOs (unspent transaction outputs)":"Addresses (wallets/scripts)")+". Click a node to trace its full flow chain; click an edge for transaction details. Hover a node to reveal edge labels.</div>";
  html+="<div class='sec'>Type filter</div><div id='types'>";
  TYPES.forEach(t=>{ html+="<label class='f'><input type='checkbox' checked value='"+t+"'><span class='badge' style='background:"+TYPE_COLOR[t]+"33;color:"+TYPE_COLOR[t]+";border:1px solid "+TYPE_COLOR[t]+"66'>"+t+"</span></label>"; });
  html+="</div>";
  html+="<div class='sec'>Addresses</div>";
  (P.legend_addrs||[]).forEach(a=>{
    const star=a.is_target?' ★':''; const cx=a.cex?(' ['+a.cex+']'):'';
    const cu=(!a.cex && a.cex_user)?(' ['+a.cex_user+' User]'):'';
    html+="<div class='row'><span class='dot' style='background:"+a.color+"'></span><code>"+a.short+"</code><span class='muted'>"+(a.tx_count!=null?(a.tx_count+'tx'):'')+star+cx+cu+"</span></div>";
  });
  if(P.assets && P.assets.length){ html+="<div class='sec'>Assets</div>"; P.assets.forEach(u=>{ html+="<div class='row'><code>"+u+"</code></div>"; }); }
  if(P.cashflow && P.cashflow.length){
    html+="<div class='sec' style='color:#f0883e'>CexFlow</div>";
    P.cashflow.forEach(c=>{ const col=c.record_type==='withdrawal'?'#3fb950':'#d29922';
      html+="<div class='row'><span style='color:"+col+"'>●</span><span>"+c.cex+"</span><span class='muted'>"+Math.round(c.amount)+" ADA "+c.confidence+"</span></div>"; });
  }
  left.innerHTML=html;
  $('types').addEventListener('change', ()=>{
    hiddenTypes.clear();
    [...document.querySelectorAll('#types input:not(:checked)')].forEach(i=>hiddenTypes.add(i.value));
    renderer.refresh();
  });

  // ---- toolbar ----
  $('fit').onclick=()=>{ try{ renderer.getCamera().animatedReset({duration:300}); }catch(_){} };
  $('labels').onclick=()=>{ const v=!renderer.getSetting('renderLabels'); renderer.setSetting('renderLabels',v); };
  $('clear').onclick=()=>{ clearHighlight(); detail.style.display='none'; renderer.refresh(); };
  $('relayout').onclick=async ()=>{ try{
    showOverlay('re-computing layout…');
    const settings=Object.assign(FA2.inferSettings(g),{barnesHutOptimize:N>500,adjustSizes:true,gravity:1.2,scalingRatio:12});
    let done=0; const total=N>2000?160:300;
    while(done<total){ FA2.assign(g,{iterations:10,settings}); done+=10; setBar(Math.round(done/total*100)); await new Promise(r=>requestAnimationFrame(r)); }
    hideOverlay(); renderer.refresh(); renderer.getCamera().animatedReset({duration:300});
  }catch(err){ hideOverlay(); } };
  $('png').onclick=()=>{ try{
    const canvases=renderer.getCanvases(); const {width,height}=renderer.getDimensions();
    const out=document.createElement('canvas'); out.width=width; out.height=height;
    const ctx=out.getContext('2d'); ctx.fillStyle='#0d1117'; ctx.fillRect(0,0,width,height);
    ['edges','nodes','edgeLabels','labels'].forEach(k=>{ const c=canvases[k]; if(c) ctx.drawImage(c,0,0,width,height); });
    const a=document.createElement('a'); a.href=out.toDataURL('image/png'); a.download='trace.png'; a.click();
  }catch(err){ console.warn('png',err); } };
  const search=$('search');
  search.addEventListener('keydown', (e)=>{
    if(e.key!=='Enter') return;
    const q=search.value.trim().toLowerCase(); if(!q) return;
    let hit=null;
    g.forEachNode((id,a)=>{ if(hit) return; const r=a.raw||{};
      if((r.address||'').toLowerCase().includes(q) || (id||'').toLowerCase().includes(q) || (r.tx_hash||'').toLowerCase().includes(q)) hit=id; });
    if(hit){ try{
      selectedNode=hit; computePath(hit); showDetail(g.getNodeAttribute(hit,'raw'));
      const a=g.getNodeAttribute(hit,'x'), b=g.getNodeAttribute(hit,'y');
      const disp=renderer.graphToViewport({x:a,y:b});
      renderer.getCamera().animate(renderer.viewportToFramedGraph(disp), {duration:400, ratio:0.4});
      renderer.refresh();
    }catch(err){ console.warn(err); } }
  });

  // ---- persist positions + camera so re-opens are instant ----
  if(CACHE_KEY){
    window.addEventListener('beforeunload', ()=>{
      try{
        const positions={}; g.forEachNode((id,a)=>{ positions[id]={x:a.x,y:a.y}; });
        const cam=renderer.getCamera().getState();
        const body=JSON.stringify({zoom:cam.ratio, node_positions:positions});
        navigator.sendBeacon('/viz-state?k='+encodeURIComponent(CACHE_KEY), new Blob([body],{type:'application/json'}));
      }catch(_){}
    });
  }
})();
"""



# ---------------------------------------------------------------------------
# browser launch (Wayland/Vulkan-safe)
# ---------------------------------------------------------------------------


def _open_browser(url: str) -> None:
    import platform

    if platform.system() != "Linux":
        webbrowser.open(url)
        return

    for browser in ("chromium", "chromium-browser", "google-chrome"):
        path = shutil.which(browser)
        if path:
            try:
                subprocess.Popen(
                    [
                        path,
                        "--ozone-platform-hint=auto",
                        "--disable-vulkan",
                        "--no-sandbox",
                        url,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                continue

    webbrowser.open(url)


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------


def _serve(payload: dict, port: int, cache_key: str) -> None:
    from utxo_tracer import cache as _cache

    # Encode once, up front. The HTML shell is tiny; the (potentially multi-MB)
    # graph JSON is served separately at /data and fetched async by the client,
    # so it never bloats the HTML or gets re-encoded per request.
    html_bytes = _render_html(payload).encode("utf-8")
    data_bytes = json.dumps(payload, default=str).encode("utf-8")

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
            if self.path.startswith("/data"):
                self._send(200, data_bytes, "application/json")
                return
            self._send(200, html_bytes, "text/html; charset=utf-8")

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
    print(f"\n  Graph (Sigma.js WebGL) → {url}")
    print("  Press Ctrl+C to stop\n")
    threading.Thread(target=lambda: _open_browser(url), daemon=True).start()
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
    _serve(payload, port, cache_key)


def start_address_server(
    result: AddressTraceResult,
    target_address: str = "",
    port: int = 8050,
    debug: bool = False,
    cache_key: str = "",
) -> None:
    payload = build_address_payload(result, target_address)
    payload["cache_key"] = cache_key
    _serve(payload, port, cache_key)
