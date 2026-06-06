"""Click CLI entrypoint."""

from __future__ import annotations

import asyncio
import csv
import json as jsonlib
import logging
import os
import sys
from collections import Counter
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, NoReturn, Optional

from . import cache as cache_mod

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.tree import Tree

from .cex.registry import identify_cex, load_cex_from_file
from .config import clear_config, load_config, save_config, set_provider_config
from .models import Asset, OutRef, TraceResult, TraceStep, TransactionEdge, UTxONode
from .providers import build_provider
from .providers.base import Provider
from .providers.fallback import FallbackProvider as _FallbackProvider
from .tracing import apply_cex_filter, build_graph_from_steps, trace_backward, trace_forward
from .utils import lovelace_to_ada, parse_out_ref, shorten

from ._rich_tty import LiveProgress

logger = logging.getLogger(__name__)

console = Console()
err_console = Console(stderr=True)

# ── Provider option vocabulary (single source of truth) ───────────────
# Every command that talks to a provider reuses these so the CLI surface
# (choices, names) stays identical across `trace`, `trace-address`,
# `health`, `assets`, and `config set`.
PROVIDER_CHOICES = ["blockfrost", "koios", "maestro", "kupmios", "utxorpc", "minibf"]
AUTH_TYPE_CHOICES = ["project_id", "bearer", "dmtr-api-key"]

# Default provider chain tried when --fallback is on (the default).
# kupmios (self-hosted Kupo+Ogmios) and minibf (local Dolos) are intentionally
# excluded: they need explicit local URLs, so auto-falling-back to them would
# only add guaranteed-failing attempts. Select them explicitly via --provider.
FALLBACK_ORDER = ["blockfrost", "koios", "maestro", "utxorpc"]


def _fatal(msg: str, exit_code: int = 1) -> NoReturn:
    err_console.print(f"[bold red]Error:[/bold red] {msg}")
    sys.exit(exit_code)


def _fmt_ts(ts: int) -> str:
    """Format a Unix timestamp as a readable date string."""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OSError, ValueError, OverflowError):
        return str(ts)


def _dataclass_to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        d = asdict(obj)
        return d
    if isinstance(obj, list):
        return [_dataclass_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    return obj


def connection_options(func):
    """Attach the shared provider-connection options to a command.

    Every command that builds a provider stacks the *same* connection flags
    (provider selection, credentials, endpoint/URL overrides, proxy). Defining
    them once here keeps the option surface identical across commands and means
    a new flag only has to be added in one place.

    Note: ``--fallback/--no-fallback`` is NOT included — it is command-specific
    (``trace``/``trace-address``/``health`` opt in; ``assets`` does not).
    """
    options = [
        click.option(
            "--provider",
            type=click.Choice(PROVIDER_CHOICES),
            default=None,
            help="Data provider. Defaults to config/env, else 'utxorpc'.",
        ),
        click.option(
            "--api-key",
            type=str,
            default=None,
            help="Provider API key. Comma-separate several to rotate on 429.",
        ),
        click.option("--base-url", type=str, default=None, help="Override provider base URL."),
        click.option(
            "--auth-type",
            type=click.Choice(AUTH_TYPE_CHOICES),
            default=None,
            help="Auth scheme for blockfrost/minibf.",
        ),
        click.option(
            "--endpoint-url", type=str, default=None, help="Demeter/UTxORPC endpoint URL."
        ),
        click.option(
            "--kupo-url", type=str, default=None, help="Kupo URL(s) for kupmios (comma-separate)."
        ),
        click.option(
            "--ogmios-url",
            type=str,
            default=None,
            help="Ogmios URL(s) for kupmios (comma-separate).",
        ),
        click.option(
            "--use-proxy/--no-proxy", default=False, help="Route HTTP providers through a proxy."
        ),
        click.option(
            "--proxy-url",
            type=str,
            default="http://localhost:3001",
            help="Proxy base URL when --use-proxy is set.",
        ),
    ]
    for option in reversed(options):
        func = option(func)
    return func


def _collect_overrides(
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    auth_type: Optional[str] = None,
    endpoint_url: Optional[str] = None,
    kupo_url: Optional[str] = None,
    ogmios_url: Optional[str] = None,
) -> dict:
    """Build a provider-override dict from CLI flags, dropping unset (None) values."""
    return {
        k: v
        for k, v in {
            "api_key": api_key,
            "base_url": base_url,
            "auth_type": auth_type,
            "endpoint_url": endpoint_url,
            "kupo_url": kupo_url,
            "ogmios_url": ogmios_url,
        }.items()
        if v is not None
    }


def _resolve_provider_name(cli_provider: Optional[str], cfg: dict) -> str:
    if cli_provider:
        return cli_provider
    default = cfg.get("default_provider")
    if default:
        return default
    logger.warning(
        "No provider specified. Defaulting to 'utxorpc'. "
        "Set UTXO_TRACER_PROVIDER env var or run `utxo-tracer config set --provider <name>`."
    )
    return "utxorpc"


def _build_providers(
    name: str,
    cfg: dict,
    *,
    use_fallback: bool,
    api_key: Optional[str],
    base_url: Optional[str],
    auth_type: Optional[str],
    endpoint_url: Optional[str],
    kupo_url: Optional[str],
    ogmios_url: Optional[str],
    use_proxy: bool,
    proxy_url: str,
) -> Provider:
    provider_cfg = (cfg.get("providers") or {}).get(name, {}) or {}
    overrides = _collect_overrides(
        api_key=api_key,
        base_url=base_url,
        auth_type=auth_type,
        endpoint_url=endpoint_url,
        kupo_url=kupo_url,
        ogmios_url=ogmios_url,
    )

    if not use_fallback:
        return build_provider(
            name,
            provider_cfg,
            use_proxy=use_proxy,
            proxy_url=proxy_url,
            overrides=overrides,
        )

    order = FALLBACK_ORDER[:]
    if name in order:
        order.remove(name)
    order.insert(0, name)

    providers: list[tuple[str, Provider]] = []
    for pname in order:
        p_cfg = (cfg.get("providers") or {}).get(pname, {}) or {}
        try:
            p = build_provider(
                pname,
                p_cfg,
                use_proxy=use_proxy,
                proxy_url=proxy_url,
                overrides=overrides,
            )
            providers.append((pname, p))
        except Exception as e:
            logging.getLogger(__name__).warning("Skipping provider %s: %s", pname, e)
            continue
    if not providers:
        _fatal("No providers could be built for fallback chain")
    return _FallbackProvider(providers)


def _print_summary(result: TraceResult) -> None:
    n_cex = sum(1 for n in result.nodes if identify_cex(n.address) is not None)
    total_ada = sum(n.ada for n in result.nodes)
    panel = Panel.fit(
        "\n".join(
            [
                f"[bold]Start:[/bold] {result.start_out_ref}",
                f"[bold]Direction:[/bold] {result.direction}",
                f"[bold]Max depth:[/bold] {result.max_depth}",
                f"[bold]Provider:[/bold] {getattr(result, 'provider_name', '') or 'fallback'}",
                f"[bold]Nodes:[/bold] {len(result.nodes)}",
                f"[bold]Edges:[/bold] {len(result.edges)}",
                f"[bold]CEX hits:[/bold] {n_cex}",
                f"[bold]Total ADA across nodes:[/bold] {total_ada:,.6f}",
            ]
        ),
        title="UTXO Trace Summary",
        border_style="cyan",
    )
    console.print(panel)


def _print_nodes_table(result: TraceResult) -> None:
    nodes = result.nodes
    n_total = len(nodes)
    cex_cache: dict[str, str] = {}
    type_cache: dict[str, str] = {}
    for n in nodes:
        addr = n.address
        if addr not in cex_cache:
            ci = identify_cex(addr)
            cex_cache[addr] = f"{ci.name} ({ci.confidence})" if ci else ""
            type_cache[addr] = n.address_type
    SHOW_TOP = 100 if n_total <= 150 else 50
    show_nodes = nodes[:SHOW_TOP]
    _TYPE_LABEL_MAP = {
        "wallet": "[blue]W[/blue]",
        "script": "[yellow]S[/yellow]",
        "byron": "[magenta]B[/magenta]",
        "stake": "[green]K[/green]",
        "unknown": "[dim]?[/dim]",
    }
    table = Table(
        title=f"Nodes ({n_total})"
        + ("" if n_total == len(show_nodes) else f" — showing top {SHOW_TOP}"),
        header_style="bold magenta",
    )
    table.add_column("Node", style="dim", overflow="fold")
    table.add_column("Address", overflow="fold")
    table.add_column("Type", justify="center")
    table.add_column("ADA", justify="right")
    table.add_column("Assets", overflow="fold")
    table.add_column("CEX")
    cex_seen: set[str] = set()
    for n in show_nodes:
        addr = n.address
        cex_label = cex_cache[addr]
        if cex_label:
            row_style = "bold red"
            cex_seen.add(cex_label)
        elif n.ada >= 100:
            row_style = "yellow"
        else:
            row_style = "green"
        non_ada = [a for a in n.assets if not a.is_lovelace]
        if non_ada:
            asset_strs = [f"{a.unit}: {a.quantity:,}" for a in non_ada]
            assets_display = asset_strs[0]
            if len(asset_strs) > 1:
                assets_display += f" [dim](+{len(asset_strs) - 1})[/dim]"
        else:
            assets_display = "-"
        type_display = _TYPE_LABEL_MAP.get(type_cache[addr], "[dim]?[/dim]")
        table.add_row(
            shorten(n.id, 18, 6),
            shorten(addr, 14, 8),
            type_display,
            f"{n.ada:,.6f}",
            assets_display,
            cex_label if cex_label else "-",
            style=row_style,
        )
    n_hidden = n_total - len(show_nodes)
    if n_hidden > 0:
        hidden_cex = sum(1 for n in nodes[SHOW_TOP:] if cex_cache.get(n.address, ""))
        hidden_ada = sum(n.ada for n in nodes[SHOW_TOP:])
        table.add_row(
            f"[dim]... {n_hidden} more[/dim]",
            "",
            "",
            f"[dim]{hidden_ada:,.0f}[/dim]",
            f"[dim]({hidden_cex} CEX)[/dim]",
            "",
            style="dim",
            end_section=True,
        )
    console.print(table)


def _print_depth_tree(result: TraceResult, steps: list[TraceStep]) -> None:
    tree = Tree(f"[bold cyan]{result.start_out_ref}[/bold cyan]")
    by_parent: dict[str, list[TraceStep]] = {}
    for s in steps:
        parent = s.parent_out_ref.node_id() if s.parent_out_ref else "__root__"
        by_parent.setdefault(parent, []).append(s)

    def _attach(node, parent_key: str) -> None:
        for child in by_parent.get(parent_key, []):
            label = f"d={child.depth} {shorten(child.out_ref.node_id(), 16, 6)}"
            if child.error:
                label += f" [red]({child.error[:40]})[/red]"
            elif child.utxo:
                label += f" [green]{child.utxo.ada:.2f} ADA[/green]"
                _atype = child.utxo.address_type
                if _atype == "script":
                    label += " [yellow]S[/yellow]"
                elif _atype == "byron":
                    label += " [magenta]B[/magenta]"
                elif _atype == "stake":
                    label += " [green]K[/green]"
                elif _atype == "unknown":
                    label += " [dim]?[/dim]"
                cex = identify_cex(child.utxo.address)
                if cex:
                    label += f" [bold red]CEX:{cex.name}[/bold red]"
            sub = node.add(label)
            _attach(sub, child.out_ref.node_id())

    _attach(tree, "__root__")
    console.print(tree)


def _print_depth_report(steps: list[TraceStep]) -> None:
    depths = Counter(s.depth for s in steps if s.utxo)
    errors = Counter(s.depth for s in steps if s.error)
    table = Table(title="Depth Report", header_style="bold yellow")
    table.add_column("Depth", justify="right")
    table.add_column("Nodes", justify="right")
    table.add_column("Errors", justify="right")
    all_depths = sorted(set(list(depths.keys()) + list(errors.keys())))
    for d in all_depths:
        table.add_row(str(d), str(depths.get(d, 0)), str(errors.get(d, 0)))
    console.print(table)


def _print_cex_findings(result: TraceResult) -> None:
    findings = []
    for n in result.nodes:
        cex = identify_cex(n.address)
        if cex:
            findings.append((n, cex))
    if not findings:
        return
    table = Table(title="CEX Findings", header_style="bold red")
    table.add_column("Node")
    table.add_column("CEX")
    table.add_column("Confidence")
    table.add_column("ADA", justify="right")
    table.add_column("Address", overflow="fold")
    for n, cex in findings:
        table.add_row(
            shorten(n.id, 18, 6),
            cex.name,
            cex.confidence,
            f"{n.ada:,.6f}",
            n.address,
        )
    console.print(table)


def _export_json(result: TraceResult, path: str) -> None:
    payload = {
        "start_out_ref": str(result.start_out_ref),
        "direction": result.direction,
        "max_depth": result.max_depth,
        "error": result.error,
        "nodes": [
            {
                "id": n.id,
                "tx_hash": n.out_ref.tx_hash,
                "output_index": n.out_ref.output_index,
                "address": n.address,
                "lovelace": n.lovelace,
                "ada": round(n.ada, 6),
                "assets": [
                    {
                        "policy_id": a.policy_id,
                        "asset_name": a.asset_name,
                        "quantity": a.quantity,
                        "unit": a.unit,
                    }
                    for a in n.assets
                ],
                "datum_hash": n.datum_hash,
                "inline_datum": n.inline_datum,
                "script_ref": n.script_ref,
            }
            for n in result.nodes
        ],
        "edges": [
            {
                "id": e.id,
                "source": e.source,
                "target": e.target,
                "direction": e.direction,
                "tx_hash": e.tx_hash,
                "fee": e.fee,
            }
            for e in result.edges
        ],
        "traced_path": result.traced_path,
        "cex_findings": result.cex_findings,
        "steps": [
            {
                "out_ref": str(s.out_ref),
                "direction": s.direction,
                "depth": s.depth,
                "error": s.error,
                "parent_out_ref": str(s.parent_out_ref) if s.parent_out_ref else None,
                "visited_at": s.visited_at,
                "has_utxo": s.utxo is not None,
            }
            for s in result.steps
        ],
    }
    Path(path).write_text(
        jsonlib.dumps(payload, indent=2, default=str), encoding="utf-8"
    )


def _export_csv(result: TraceResult, base_path: str) -> tuple[str, str]:
    base = Path(base_path)
    if base.suffix.lower() == ".csv":
        stem = base.with_suffix("")
        nodes_path = str(stem) + "_nodes.csv"
        edges_path = str(stem) + "_edges.csv"
    else:
        base.mkdir(parents=True, exist_ok=True)
        nodes_path = str(base / "nodes.csv")
        edges_path = str(base / "edges.csv")
    with open(nodes_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "id",
                "tx_hash",
                "output_index",
                "address",
                "lovelace",
                "ada",
                "asset_count",
                "datum_hash",
                "has_inline_datum",
                "has_script_ref",
                "is_cex",
                "cex_name",
            ]
        )
        for n in result.nodes:
            cex = identify_cex(n.address)
            w.writerow(
                [
                    n.id,
                    n.out_ref.tx_hash,
                    n.out_ref.output_index,
                    n.address,
                    n.lovelace,
                    n.ada,
                    len([a for a in n.assets if not a.is_lovelace]),
                    n.datum_hash or "",
                    n.inline_datum is not None,
                    n.script_ref is not None,
                    cex is not None,
                    cex.name if cex else "",
                ]
            )
    with open(edges_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "source", "target", "direction", "tx_hash", "fee"])
        for e in result.edges:
            w.writerow(
                [e.id, e.source, e.target, e.direction, e.tx_hash or "", e.fee or ""]
            )
    return nodes_path, edges_path


async def _run_trace(
    provider: Provider,
    start: OutRef,
    direction: str,
    max_depth: int,
    progress: Progress,
    task_id,
    cached_nodes: Optional[dict[str, UTxONode]] = None,
    cached_inputs: Optional[dict[str, list[str]]] = None,
    cached_spend_map: Optional[dict[str, str]] = None,
    store: Optional[dict] = None,
    store_module=None,
    trace_key: str = "",
) -> tuple[list[TraceStep], Optional[str], int]:
    steps: list[TraceStep] = []
    err: Optional[str] = None
    nodes_found = 0
    errors_found = 0
    try:
        if direction == "forward":
            gen = trace_forward(
                provider,
                start,
                max_depth=max_depth,
                cached_nodes=cached_nodes,
                cached_spend_map=cached_spend_map,
            )
        else:
            gen = trace_backward(
                provider,
                start,
                max_depth=max_depth,
                cached_nodes=cached_nodes,
                cached_inputs=cached_inputs,
            )
        async for step in gen:
            steps.append(step)
            if step.utxo:
                nodes_found += 1
            if step.error:
                errors_found += 1

            # Check if this step was served from cache
            is_cached = bool(
                cached_nodes and step.utxo and step.out_ref.node_id() in cached_nodes
            )
            source_tag = (
                getattr(provider, "current_provider", "")
                or getattr(provider, "provider_type", "")
                or ""
            )
            if is_cached:
                source_tag = ""
            elif source_tag:
                source_tag = f"[dim]{source_tag}[/dim]"
            else:
                # Emergency fallback — should never reach here
                source_tag = "[red]?[/red]"

            # Per-step cache: save immediately (even failed steps)
            # SQLite handles persistence; trace_key controls whether caching is active
            if trace_key and store_module is not None:
                store_module.save_trace_step(
                    trace_key,
                    step.out_ref.node_id(),
                    step.depth,
                    step.error,
                    step.parent_out_ref.node_id() if step.parent_out_ref else None,
                    step.utxo,
                    None,  # store — unused with SQLite
                    start=f"{start.tx_hash}#{start.output_index}",
                    direction=direction,
                )

            node_label = shorten(step.out_ref.node_id(), 12, 6)
            progress.update(
                task_id,
                advance=1,
                description=f"[cyan]{direction}[/cyan] depth={step.depth} node={node_label} errors={errors_found} {source_tag}",
            )
        # Summary: count cached vs live
        _cached_count = sum(
            1
            for s in steps
            if s.utxo and cached_nodes and s.out_ref.node_id() in cached_nodes
        )
        _live_count = nodes_found - _cached_count
        if _cached_count:
            console.print(
                f"[dim]{direction}: {_cached_count} cached + "
                f"{_live_count} new = {nodes_found} nodes, "
                f"{errors_found} errors[/dim]"
            )
        elif nodes_found:
            console.print(
                f"[dim]{direction}: {nodes_found} nodes, {errors_found} errors[/dim]"
            )
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return steps, err, errors_found


async def _do_trace(
    provider: Provider,
    start: OutRef,
    direction: str,
    max_depth: int,
    trace_key: str = "",
) -> TraceResult:
    from . import cache as _cache_mod

    (
        _cached_nodes,
        _cached_inputs,
        _cached_outputs,
        _cached_spend_map,
    ) = _cache_mod._store_to_models(None)
    if _cached_nodes:
        console.print(
            f"[dim]Store: {len(_cached_nodes)} cached nodes, "
            f"{sum(len(v) for v in _cached_inputs.values())} backward edges, "
            f"{len(_cached_spend_map)} forward spends[/dim]"
        )
    store = None  # SQLite handles persistence directly
    with LiveProgress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed} steps"),
        TimeElapsedColumn(),
    ) as progress:
        all_steps: list[TraceStep] = []
        err: Optional[str] = None
        if direction in ("backward", "both"):
            task = progress.add_task(f"[cyan]backward[/cyan] tracing...", total=None)
            bsteps, berr, berrors = await _run_trace(
                provider,
                start,
                "backward",
                max_depth,
                progress,
                task,
                cached_nodes=_cached_nodes,
                cached_inputs=_cached_inputs,
                store=store,
                store_module=_cache_mod,
                trace_key=trace_key,
            )
            all_steps.extend(bsteps)
            err = berr
        if direction in ("forward", "both"):
            task = progress.add_task(f"[cyan]forward[/cyan] tracing...", total=None)
            fsteps, ferr, _ferrors = await _run_trace(
                provider,
                start,
                "forward",
                max_depth,
                progress,
                task,
                cached_nodes=_cached_nodes,
                cached_spend_map=_cached_spend_map,
                store=store,
                store_module=_cache_mod,
                trace_key=trace_key,
            )
            all_steps.extend(fsteps)
            if ferr:
                err = (err + "; " if err else "") + ferr
    primary_direction = "backward" if direction != "forward" else "forward"
    nodes, edges, traced_path = build_graph_from_steps(all_steps, primary_direction)
    pname = getattr(provider, "current_provider", "") or ""
    provider_name = pname if pname else getattr(provider, "provider_type", "fallback")
    if direction == "both":
        fwd_steps = [s for s in all_steps if s.direction == "forward"]
        _, fwd_edges, _ = build_graph_from_steps(fwd_steps, "forward")
        existing = {e.id for e in edges}
        for fe in fwd_edges:
            if fe.id not in existing:
                edges.append(fe)
                existing.add(fe.id)
    cex_findings: list[dict] = []
    for n in nodes:
        cex = identify_cex(n.address)
        if cex:
            cex_findings.append(
                {
                    "node_id": n.id,
                    "address": n.address,
                    "name": cex.name,
                    "type": cex.type,
                    "confidence": cex.confidence,
                    "ada": round(n.ada, 6),
                }
            )
    if trace_key:
        from . import cache as _cache_mod

        _cache_mod.finalize_trace(trace_key)
    return TraceResult(
        nodes=nodes,
        edges=edges,
        traced_path=traced_path,
        start_out_ref=start,
        direction=direction,
        max_depth=max_depth,
        cex_findings=cex_findings,
        error=err,
        steps=all_steps,
        provider_name=provider_name,
    )


_MAIN_HELP = """\
Cardano UTXO chain tracer — trace funds through Cardano blockchain.

PROVIDERS
  blockfrost   Blockfrost API (mainnet/testnet). Backward + forward + address.
  koios        Koios public API. Backward + forward + address.
  maestro      Maestro API. Backward + address (no forward).
  kupmios      Kupo + Ogmios (self-hosted). Backward + forward + address.
  utxorpc      UTxORPC gRPC. Backward only (forward depends on DumpHistory).

  Backward = trace UTXO inputs (cash-in side).
  Forward  = trace UTXO spends (cash-out side).
  Address  = trace address interactions (all addresses that shared a tx).

MULTI-KEY (rate-limit avoidance)
  Pass multiple API keys comma-separated: --api-key key1,key2,key3
  Auto-rotates on HTTP 429. Supported for: blockfrost, koios, maestro.
  Kupmios also supports comma-separated --kupo-url / --ogmios-url
    for multi-instance rotation (e.g. multiple Kupo+Ogmios pairs).
  UTxORPC rate-limit depends on endpoint (Demeter.run, self-hosted, etc.).

FALLBACK
  --fallback (on)   tries primary, then utxorpc -> blockfrost -> koios -> maestro
  --no-fallback     single provider only

CONFIG PRIORITY  CLI flags > shell env > .env file > config.json

EXAMPLES
  utxo-tracer trace-utxo abc123...#0 --provider blockfrost --api-key mainnet_XXX
  utxo-tracer trace-utxo abc123...#0
  utxo-tracer trace-utxo abc123...#0 --provider kupmios --direction forward
  utxo-tracer trace-address addr1...        (trace an address's interactions)
"""


@click.group(help=_MAIN_HELP)
@click.version_option(package_name="utxo-tracer")
def main() -> None:
    pass


@main.command("trace-utxo", help="Trace a UTXO backwards/forwards through the chain.")
@click.argument("utxo", type=str)
@connection_options
@click.option(
    "--direction", type=click.Choice(["backward", "forward", "both"]), default=None
)
@click.option("--max-depth", type=int, default=None)
@click.option("--output", type=click.Choice(["table", "json", "csv"]), default="table")
@click.option("--export-json", type=click.Path(dir_okay=False), default=None)
@click.option("--export-csv", type=click.Path(), default=None)
@click.option("--fallback/--no-fallback", default=True)
@click.option("--cex-file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--depth-report", is_flag=True, default=False)
@click.option("--dash/--no-dash", default=True, hidden=True)
@click.option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="Skip local cache; always query providers.",
)
def trace_cmd(
    utxo,
    provider,
    api_key,
    base_url,
    auth_type,
    endpoint_url,
    kupo_url,
    ogmios_url,
    direction,
    max_depth,
    output,
    export_json,
    export_csv,
    fallback,
    cex_file,
    depth_report,
    dash,
    use_proxy,
    proxy_url,
    no_cache,
):
    cfg = load_config()
    defaults = cfg.get("defaults", {}) or {}
    direction = direction or defaults.get("direction") or "backward"
    max_depth = (
        max_depth if max_depth is not None else int(defaults.get("max_depth") or 5)
    )
    for _var in ["UTXO_TRACER_PROVIDER", "UTXORPC_ENDPOINT_URL"]:
        if _var in os.environ:
            logger.info("Env %s=%s", _var, os.environ[_var][:30])
    if cex_file:
        try:
            count = load_cex_from_file(cex_file)
            console.print(f"[green]Loaded {count} CEX entries from {cex_file}[/green]")
        except Exception as e:
            err_console.print(f"[yellow]Warning loading CEX file:[/yellow] {e}")
    try:
        start = parse_out_ref(utxo)
    except ValueError as e:
        _fatal(str(e))

    trace_key = ""
    if not no_cache:
        # 1. Try partial cache (per-step manifest, depth-adaptive)
        cached_partial = cache_mod.load_trace_partial(start, direction, max_depth)
        if cached_partial is not None:
            if cached_partial.completed and not cached_partial.failed_nodes:
                if cached_partial.cached_max_depth >= max_depth:
                    # Full hit via per-step cache — use v2 snapshot if available
                    cached_result = cache_mod.load_trace(start, direction, max_depth)
                    if cached_result is not None:
                        console.print("[green]Loaded from local cache[/green]")
                        from .graph.g6_viz import start_server

                        ck = cache_mod._cache_key(start, direction, max_depth)
                        start_server(cached_result, start_out_ref=start, cache_key=ck)
                        return
                    # No v2 snapshot (e.g. old manifest from interrupted trace) — rebuild
                    console.print(
                        "[green]Cache: per-step manifest — rebuilding[/green]"
                    )

            if cached_partial.failed_nodes:
                msg = f"[yellow]Cache: {len(cached_partial.cached_steps)} OK, {len(cached_partial.failed_nodes)} failed — re-query"
                if cached_partial.cached_max_depth < max_depth:
                    msg += (
                        f" (depth {cached_partial.cached_max_depth}\u2192{max_depth})"
                    )
                console.print(msg + "[/yellow]")
            elif cached_partial.cached_max_depth < max_depth:
                console.print(
                    f"[yellow]Cache: depth {cached_partial.cached_max_depth} \u2192 {max_depth} — extending[/yellow]"
                )
        else:
            # 2. Try v2 snapshot (backward compat)
            cached_result = cache_mod.load_trace(start, direction, max_depth)
            if cached_result is not None:
                if getattr(cached_result, "errors_count", 0) > 0:
                    console.print(
                        f"[yellow]Cache: snapshot has {cached_result.errors_count} errors — re-tracing[/yellow]"
                    )
                else:
                    console.print("[green]Loaded from local cache[/green]")
                    from .graph.g6_viz import start_server

                    ck = cache_mod._cache_key(start, direction, max_depth)
                    start_server(cached_result, start_out_ref=start, cache_key=ck)
                    return

    if not no_cache:
        trace_key = cache_mod._cache_key(start, direction, max_depth)
    provider_name = _resolve_provider_name(provider, cfg)
    prov = _build_providers(
        provider_name,
        cfg,
        use_fallback=fallback,
        api_key=api_key,
        base_url=base_url,
        auth_type=auth_type,
        endpoint_url=endpoint_url,
        kupo_url=kupo_url,
        ogmios_url=ogmios_url,
        use_proxy=use_proxy,
        proxy_url=proxy_url,
    )
    # Gate forward tracing on the real provider CAPABILITY (the single source
    # of truth shared with the tracer in tracing/forward.py), not a hardcoded
    # name list — so minibf, rotating-key wrappers, and fallback chains (which
    # report the capability of what they wrap) are all judged correctly.
    if direction in ("forward", "both") and not getattr(
        prov, "supports_forward", False
    ):
        _fatal(
            f"Forward tracing is not supported by provider '{provider_name}'. "
            f"Use blockfrost, koios, or kupmios (optionally with --fallback)."
        )

    async def _runner() -> TraceResult:
        async with prov as p:
            return await _do_trace(p, start, direction, max_depth, trace_key=trace_key)

    try:
        result = asyncio.run(_runner())
        if not no_cache:
            cache_mod.save_trace(
                result, start, direction, max_depth, provider=provider_name
            )
    except RuntimeError as e:
        if "event loop" in str(e).lower():
            _fatal(
                "Cannot run: already inside an event loop (Jupyter/pytest-asyncio). Use `await` instead."
            )
        raise
    except KeyboardInterrupt:
        err_console.print("[yellow]Trace interrupted[/yellow]")
        sys.exit(130)
    if output == "json":
        click.echo(jsonlib.dumps(_dataclass_to_dict(result), indent=2, default=str))
    elif output == "csv":
        n_path, e_path = _export_csv(result, export_csv or "./trace_output")
        console.print(f"[green]Wrote nodes CSV:[/green] {n_path}")
        console.print(f"[green]Wrote edges CSV:[/green] {e_path}")
    else:
        _print_summary(result)
        _print_nodes_table(result)
        _print_cex_findings(result)
        _print_depth_tree(result, result.steps)
    if depth_report:
        _print_depth_report(result.steps)
    if export_json and output != "json":
        _export_json(result, export_json)
        console.print(f"[green]Exported JSON:[/green] {export_json}")
    if export_csv and output != "csv":
        n_path, e_path = _export_csv(result, export_csv)
        console.print(f"[green]Exported nodes CSV:[/green] {n_path}")
        console.print(f"[green]Exported edges CSV:[/green] {e_path}")
    from .graph.g6_viz import start_server

    if dash:
        console.print("[green]Starting graph visualization...[/green]")
        # browser is opened by the g6 viz server on its actual port
        ck = cache_mod._cache_key(start, direction, max_depth) if not no_cache else ""
        start_server(result, start_out_ref=start, cache_key=ck, cashflow_summary=None)
    else:
        console.print("[green]Trace complete — use --dash to view graph[/green]")
    if result.error:
        err_console.print(f"[red]\u2717 Trace failed: {result.error}[/red]")
        sys.exit(2)


# ── trace-address command ──────────────────────────────────────


def _present_cached_address_result(
    cached: "AddressTraceResult",
    *,
    address: str,
    direction: str,
    cex_filter: bool,
    dash: bool,
) -> None:
    """Render a cache-hit address-trace result: optional CEX filter, then the
    graph dashboard (``--dash``) or the summary tables.

    Shared by both cache-hit paths in ``trace-address`` — the per-step manifest
    full-hit and the legacy v2 snapshot — which were previously identical
    copy-pasted blocks.
    """
    console.print("[green]Loaded from local cache[/green]")
    result = cached
    if cex_filter and result.addresses:
        n_before = len(result.addresses)
        result = apply_cex_filter(result)
        _print_cex_filter_banner(n_before, len(result.addresses))
    if dash and result.addresses:
        from .graph.g6_viz import start_address_server

        console.print("[green]Starting graph visualization...[/green]")
        # browser is opened by the g6 viz server on its actual port
        start_address_server(
            result,
            target_address=address,
            cache_key=cache_mod._addr_cache_key(address, direction=direction),
        )
    else:
        _print_address_summary(result)
        _print_address_nodes_table(result)
        _print_address_interaction_edges(result)


@main.command(
    "trace-address",
    help="Trace all addresses that have interacted with a given Cardano address. Supports multiple API keys by comma-separating: --api-key key1,key2,key3",
)
@click.argument("address", type=str)
@connection_options
@click.option(
    "--tx-limit",
    type=int,
    default=None,
    help="Optional cap on transactions per address level. Default: no limit (fetch all pages).",
)
@click.option(
    "--max-depth",
    type=int,
    default=1,
    help="How many hops to trace (default: 1 = direct interactions only). "
    "Depth 2 traces interactors of interactors, etc.",
)
@click.option(
    "--direction",
    type=click.Choice(["backward", "forward", "both"]),
    default="both",
    help="Flow direction relative to the target: 'backward' = addresses that "
    "SENT funds to it (upstream), 'forward' = addresses that RECEIVED from it "
    "(downstream), 'both' = all (default).",
)
@click.option("--output", type=click.Choice(["table", "json", "csv"]), default="table")
@click.option("--export-json", type=click.Path(dir_okay=False), default=None)
@click.option("--export-csv", type=click.Path(), default=None)
@click.option("--fallback/--no-fallback", default=True)
@click.option("--cex-file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option(
    "--cex-filter/--no-cex-filter",
    default=False,
    help="Filter output to only addresses that interact with the target AND "
    "are reachable from a registered CEX address in the trace graph. "
    "Useful when the full trace is large; use a higher --max-depth if no "
    "CEXs are discovered (filter will keep only the target).",
)
@click.option("--dash/--no-dash", default=True, hidden=True)
@click.option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="Skip local cache; always query providers.",
)
def trace_address_cmd(
    address,
    provider,
    api_key,
    base_url,
    auth_type,
    endpoint_url,
    kupo_url,
    ogmios_url,
    tx_limit,
    max_depth,
    direction,
    output,
    export_json,
    export_csv,
    fallback,
    cex_file,
    cex_filter,
    dash,
    use_proxy,
    proxy_url,
    no_cache,
):
    """Trace all addresses that have interacted with the given address.

    Examines ALL transactions involving the address (or --tx-limit if set),
    then builds a directed interaction graph showing connected addresses,
    fund flow direction, and net ADA values.

    Use --max-depth N to trace multiple hops (default: 1 = direct only).
    At depth 2, each interactor's transactions are also examined.
    """
    cfg = load_config()
    if cex_file:
        try:
            count = load_cex_from_file(cex_file)
            console.print(f"[green]Loaded {count} CEX entries from {cex_file}[/green]")
        except Exception as e:
            err_console.print(f"[yellow]Warning loading CEX file:[/yellow] {e}")

    # --- cache check (manifest + v2 snapshot, depth-adaptive) ---
    # Normalize tx_limit: None (not specified) → 0 (all transactions)
    effective_tx_limit = tx_limit if tx_limit is not None else 0

    if not no_cache:
        # 1. Try manifest for partial progress
        cached_partial = cache_mod.load_address_trace_partial(
            address, max_depth, direction
        )
        if cached_partial is not None:
            cached_tx_limit = cached_partial.tx_limit
            needs_extend = (
                # User wants ALL but cache only covered some txs
                (effective_tx_limit == 0 and cached_tx_limit != 0)
                or
                # User wants MORE than cache covered
                (effective_tx_limit > cached_tx_limit > 0)
            )

            if cached_partial.completed and not needs_extend:
                # Full hit — load final v2 snapshot
                cached = cache_mod.load_address_trace(
                    address,
                    tx_limit=effective_tx_limit,
                    max_depth=max_depth,
                    direction=direction,
                )
                if cached is not None:
                    _present_cached_address_result(
                        cached,
                        address=address,
                        direction=direction,
                        cex_filter=cex_filter,
                        dash=dash,
                    )
                    return
                # No v2 snapshot — rebuild from manifest. Cache-serve replays
                # every already-fetched tx from the global store for free, so
                # the rebuilt result is complete (no provider re-query of the
                # cached txs).
                cache_mod.finalize_address_trace(address, max_depth, direction)
                n_ok = len(cached_partial.processed)
                n_fail = len(cached_partial.failed)
                if n_fail:
                    console.print(
                        f"[yellow]Cache: {n_ok} OK, {n_fail} failed — re-query failed[/yellow]"
                    )
                else:
                    console.print(
                        "[green]Cache: per-step manifest — rebuilding from cache[/green]"
                    )
            else:
                # Partial / extension — cached txs are served from the store,
                # only missing or previously-failed txs hit the provider.
                n_ok = len(cached_partial.processed)
                n_fail = len(cached_partial.failed)

                if needs_extend:
                    msg = (
                        f"[yellow]Cache: limit "
                        f"{cached_tx_limit or 'all'} \u2192 {effective_tx_limit or 'all'}"
                        f" — extending ({n_ok} cached + {n_fail} failed)[/yellow]"
                    )
                elif n_fail:
                    msg = (
                        f"[yellow]Cache: {n_ok} OK, {n_fail} failed — re-query[/yellow]"
                    )
                else:
                    msg = f"[yellow]Cache (interrupted): {n_ok} steps cached — resuming[/yellow]"
                console.print(msg)
        else:
            # 2. Try legacy v2 snapshot (backward compat)
            cached = cache_mod.load_address_trace(
                address, tx_limit=effective_tx_limit, max_depth=max_depth
            )
            if cached is not None:
                _present_cached_address_result(
                    cached,
                    address=address,
                    direction=direction,
                    cex_filter=cex_filter,
                    dash=dash,
                )
                return

    provider_name = _resolve_provider_name(provider, cfg)
    prov = _build_providers(
        provider_name,
        cfg,
        use_fallback=fallback,
        api_key=api_key,
        base_url=base_url,
        auth_type=auth_type,
        endpoint_url=endpoint_url,
        kupo_url=kupo_url,
        ogmios_url=ogmios_url,
        use_proxy=use_proxy,
        proxy_url=proxy_url,
    )

    from .tracing import trace_address_interactions

    async def _runner():
        async with prov as p:
            # Load store (identical to UTXO trace — _do_trace())
            _store = None
            (
                _cached_nodes,
                _cached_inputs,
                _cached_outputs,
                _cached_spend_map,
            ) = cache_mod._store_to_models(None)
            if _cached_nodes:
                console.print(
                    f"[dim]Store: {len(_cached_nodes)} cached nodes, "
                    f"{sum(len(v) for v in _cached_inputs.values())} backward edges, "
                    f"{sum(len(v) for v in _cached_outputs.values())} forward edges[/dim]"
                )

            # Per-step cache: save each tx's progress to manifest
            _processed_count = (
                0  # tracks live tx count (works for both batch + concurrent paths)
            )

            def _step_callback(
                source_address: str, tx_hash: str, error: Optional[str], depth: int
            ) -> None:
                nonlocal _processed_count, progress_task_id
                _processed_count += 1
                if not no_cache:
                    try:
                        cache_mod.save_address_trace_step(
                            address,
                            tx_hash,
                            error,
                            [],
                            total_count=0,
                            tx_limit=effective_tx_limit,
                            max_depth=max_depth,
                            source_address=source_address,
                            depth=depth,
                            direction=direction,
                        )
                    except Exception:
                        pass

                # Progress bar: show current tx hash + depth + source (like UTXO trace)
                short = (
                    tx_hash[:10] + "…" + tx_hash[-4:] if len(tx_hash) > 16 else tx_hash
                )
                err = error.split(":")[0] if error else ""
                err_mark = f" [red]\u2717 {err}[/red]" if error else ""
                pname = (
                    getattr(p, "current_provider", "")
                    or getattr(p, "provider_type", "")
                    or ""
                )
                if not error:
                    source_tag = ""
                elif pname:
                    source_tag = f"[dim]{pname}[/dim]"
                else:
                    source_tag = "[red]?[/red]"
                depth_tag = f"d={depth}" if max_depth > 1 else ""

                desc = f"[cyan]address[/cyan]"
                if depth_tag:
                    desc += f" {depth_tag}"
                desc += f" tx=#{_processed_count} {short}{err_mark} {source_tag}"

                if progress_task_id is None:
                    progress_task_id = progress.add_task(
                        desc,
                        total=None,
                    )
                else:
                    progress.update(progress_task_id, advance=1, description=desc)
                progress.refresh()

            def _status_callback(msg: str) -> None:
                """Show phase messages (e.g. paginating an address's tx list)
                so the bar shows life before the first tx lands."""
                nonlocal progress_task_id
                desc = f"[cyan]address[/cyan] [dim]{msg}[/dim]"
                if progress_task_id is None:
                    progress_task_id = progress.add_task(desc, total=None)
                else:
                    progress.update(progress_task_id, description=desc)
                progress.refresh()

            async def _progress_callback(completed: int, total: int) -> None:
                """Make the bar DETERMINATE per address level so it visibly fills
                as each tx lands (UTXO-trace parity), instead of an endless
                spinner that looks like it does nothing then dumps logs."""
                nonlocal progress_task_id
                if progress_task_id is None:
                    progress_task_id = progress.add_task(
                        "[cyan]address[/cyan]", total=total
                    )
                else:
                    progress.update(progress_task_id, total=total)
                progress.refresh()

            progress = LiveProgress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(),
                TextColumn("{task.completed} tx"),
                TimeElapsedColumn(),
            )
            progress_task_id = None

            with progress:
                # Serve already-cached txs from the global store; only
                # missing/failed txs hit the provider. This replaces the old
                # skip_tx_hashes approach (which dropped cached edges from the
                # rebuilt result and could overwrite a good snapshot with a
                # partial one). Now every tx is processed — cached or live — so
                # the result is always complete and safe to persist.
                result = await trace_address_interactions(
                    p,
                    address,
                    max_depth=max_depth,
                    tx_limit=tx_limit,
                    direction=direction,
                    progress_callback=_progress_callback,
                    step_callback=_step_callback,
                    status_callback=_status_callback,
                    tx_cache_get=(None if no_cache else cache_mod.get_transaction),
                    addr_txs_cache_get=(
                        None if no_cache else cache_mod.get_address_txns
                    ),
                    addr_txs_cache_save=(
                        None if no_cache else cache_mod.save_address_txns
                    ),
                )

            # End-of-run summary. _processed_count counts every tx handled
            # this run — cached (served instantly) + live (provider). The
            # cache/provider split is shown live via _status_callback.
            n_total = _processed_count
            if n_total:
                console.print(
                    f"[dim]address: {n_total} tx(s) processed"
                    f"{'' if not result.error else ', errors present'}[/dim]"
                )

            return result

    try:
        result = asyncio.run(_runner())
    except RuntimeError as e:
        if "event loop" in str(e).lower():
            _fatal(
                "Cannot run: already inside an event loop (Jupyter/pytest-asyncio). Use `await` instead."
            )
        raise
    except KeyboardInterrupt:
        err_console.print("[yellow]Trace interrupted[/yellow]")
        sys.exit(130)

    # Apply CEX filter AFTER the trace completes (so the unfiltered
    # result is what gets saved to cache). This way re-running with or
    # without --cex-filter on the same cache gives consistent results.
    if cex_filter and result.addresses:
        n_before = len(result.addresses)
        result = apply_cex_filter(result)
        _print_cex_filter_banner(n_before, len(result.addresses))

    if not no_cache:
        # Save when the trace produced real data — including a legitimate
        # zero-counterparty result (addresses present, no edges). Only skip on
        # an outright failure (error, or not even the target node resolved),
        # so a rate-limited extension doesn't clobber a good cache.
        if result.addresses and not result.error:
            cache_mod.save_address_trace(
                result,
                tx_limit=effective_tx_limit,
                max_depth=max_depth,
                direction=direction,
            )
            cache_mod.finalize_address_trace(address, max_depth, direction)
        else:
            logger.info(
                "Skipping v2 snapshot save (no edges) — keeping prior snapshot"
            )

    if output == "json":
        click.echo(jsonlib.dumps(_dataclass_to_dict(result), indent=2, default=str))
    elif output == "csv":
        _export_address_csv(result, export_csv or "./addr_trace_output")
    else:
        _print_address_summary(result)
        _print_address_nodes_table(result)
        _print_address_interaction_edges(result)

    if export_json and output != "json":
        _export_address_json(result, export_json)
        console.print(f"[green]Exported JSON:[/green] {export_json}")
    if export_csv and output != "csv":
        _export_address_csv(result, export_csv)

    if dash and result.addresses:
        from .graph.g6_viz import start_address_server

        console.print("[green]Starting graph visualization...[/green]")
        # browser is opened by the g6 viz server on its actual port
        start_address_server(
            result,
            target_address=address,
            cache_key=cache_mod._addr_cache_key(address, direction=direction),
        )
    else:
        console.print("[green]Trace complete — use --dash to view graph[/green]")

    if result.error:
        err_console.print(f"[yellow]\u26a0 Warnings: {result.error}[/yellow]")


def _print_address_summary(result: AddressTraceResult) -> None:
    from .models import AddressTraceResult

    n_cex = sum(1 for n in result.addresses if n.is_cex)
    n_cex_users = sum(1 for n in result.addresses if n.cex_user)
    net_flow = sum(n.net_ada for n in result.addresses)
    n_incoming = sum(
        1 for e in result.edges if e.direction_relative_to_target == "incoming"
    )
    n_outgoing = sum(
        1 for e in result.edges if e.direction_relative_to_target == "outgoing"
    )
    cex_user_line = (
        [f"[bold]CEX users (Binance User …):[/bold] {n_cex_users}"]
        if n_cex_users
        else []
    )
    panel = Panel.fit(
        "\n".join(
            [
                f"[bold]Target:[/bold] {shorten(result.target_address, 14, 8)}",
                f"[bold]Direction:[/bold] {getattr(result, 'direction', '') or 'both'}",
                f"[bold]Transactions examined:[/bold] {result.total_transactions}",
                f"[bold]Connected addresses:[/bold] {len(result.addresses)}",
                f"[bold]Interactions (edges):[/bold] {len(result.edges)} "
                f"([green]→{n_outgoing}[/green] [red]←{n_incoming}[/red] [dim]{len(result.edges) - n_incoming - n_outgoing} other[/dim])",
                f"[bold]CEX hits:[/bold] {n_cex}",
                *cex_user_line,
                f"[bold]Provider:[/bold] {result.provider_name or 'fallback'}",
                f"[bold]Net ADA flow:[/bold] {net_flow:+,.6f}",
            ]
        ),
        title="Address Interaction Summary",
        border_style="cyan",
    )
    console.print(panel)


def _print_cex_filter_banner(n_before: int, n_after: int) -> None:
    """Print one-line banner after CEX filter is applied.

    Called by the CLI right after ``apply_cex_filter()`` so the user sees
    both how much the graph was reduced and a hint if nothing useful was
    kept. The summary panel below will show the CEX count for the kept
    set, so this banner only needs the count delta.
    """
    if n_after == 0:
        console.print(
            "[yellow]CEX filter: kept 0 addresses (empty trace).[/yellow]"
        )
    elif n_after == 1:
        console.print(
            f"[yellow]CEX filter: kept 1/{n_before} address — only the "
            f"target. No CEX reachable in this graph; try a higher "
            f"--max-depth to discover exchanges.[/yellow]"
        )
    elif n_after < n_before:
        console.print(
            f"[cyan]CEX filter: kept {n_after}/{n_before} addresses "
            f"(hidden {n_before - n_after} non-CEX-related).[/cyan]"
        )
    else:
        console.print(
            f"[cyan]CEX filter: kept {n_after}/{n_before} addresses "
            f"(all reached a registered CEX).[/cyan]"
        )


def _print_address_nodes_table(result: AddressTraceResult) -> None:
    from .models import AddressTraceResult

    addrs = result.addresses
    n_total = len(addrs)
    SHOW_TOP = 100 if n_total <= 150 else 50
    show_addrs = addrs[:SHOW_TOP]
    _TYPE_LABEL = {
        "wallet": "[blue]W[/blue]",
        "script": "[yellow]S[/yellow]",
        "byron": "[magenta]B[/magenta]",
        "stake": "[green]K[/green]",
        "unknown": "[dim]?[/dim]",
    }
    table = Table(
        title=f"Connected Addresses ({n_total})"
        + ("" if n_total == len(show_addrs) else f" — showing top {SHOW_TOP}"),
        header_style="bold magenta",
    )
    table.add_column("", width=3)  # target indicator
    table.add_column("Address", overflow="fold")
    table.add_column("Type", justify="center")
    table.add_column("TXs", justify="right")
    table.add_column("In ADA", justify="right")
    table.add_column("Out ADA", justify="right")
    table.add_column("Net ADA", justify="right")
    table.add_column("CEX")
    for n in show_addrs:
        addr = n.address
        if n.is_target:
            row_style = "bold cyan"
            target_mark = "[★]"
        elif n.is_cex:
            row_style = "bold red"
            target_mark = " "
        elif n.cex_user:
            row_style = "bold dark_orange"
            target_mark = " "
        elif abs(n.net_ada) >= 100:
            row_style = "yellow"
            target_mark = " "
        else:
            row_style = ""
            target_mark = " "
        type_display = _TYPE_LABEL.get(n.address_type, "[dim]?[/dim]")
        if n.is_cex:
            cex_label = n.cex_name
        elif n.cex_user:
            cex_label = f"{n.cex_user} User"
        else:
            cex_label = ""
        net_label = (
            f"[green]+{n.net_ada:,.0f}[/green]"
            if n.net_ada > 0
            else f"[red]{n.net_ada:,.0f}[/red]"
            if n.net_ada < 0
            else "[dim]0[/dim]"
        )
        table.add_row(
            target_mark,
            shorten(addr, 14, 8),
            type_display,
            str(n.tx_count),
            f"{n.total_incoming_ada:,.0f}",
            f"{n.total_outgoing_ada:,.0f}",
            net_label,
            cex_label if cex_label else "-",
            style=row_style,
        )
    n_hidden = n_total - len(show_addrs)
    if n_hidden > 0:
        hidden_cex = sum(1 for n in addrs[SHOW_TOP:] if n.is_cex)
        hidden_in = sum(n.total_incoming_ada for n in addrs[SHOW_TOP:])
        hidden_out = sum(n.total_outgoing_ada for n in addrs[SHOW_TOP:])
        table.add_row(
            "",
            f"[dim]... {n_hidden} more[/dim]",
            "",
            "",
            f"[dim]{hidden_in:,.0f}[/dim]",
            f"[dim]{hidden_out:,.0f}[/dim]",
            "[dim]...[/dim]",
            f"[dim]({hidden_cex} CEX)[/dim]",
            style="dim",
            end_section=True,
        )
    console.print(table)


def _print_address_interaction_edges(result: AddressTraceResult) -> None:
    from .models import AddressTraceResult

    if not result.edges:
        return
    table = Table(
        title=f"Interactions ({len(result.edges)})", header_style="bold yellow"
    )
    table.add_column("#", justify="right")
    table.add_column("Direction", justify="center", width=2)
    table.add_column("Address A", overflow="fold")
    table.add_column("Address B", overflow="fold")
    table.add_column("Shared TXs", justify="right")
    sorted_edges = sorted(result.edges, key=lambda e: e.interaction_count, reverse=True)
    for i, e in enumerate(sorted_edges[:30]):
        dir_label = {
            "incoming": "[red]←[/red]",
            "outgoing": "[green]→[/green]",
            "both": "[yellow]↔[/yellow]",
            "unknown": "[dim]?[/dim]",
        }.get(e.direction_relative_to_target, "[dim]?[/dim]")
        table.add_row(
            str(i + 1),
            dir_label,
            shorten(e.source, 14, 8),
            shorten(e.target, 14, 8),
            str(e.interaction_count),
        )
    if len(sorted_edges) > 30:
        table.add_row(
            f"[dim]... {len(sorted_edges) - 30} more[/dim]",
            "",
            "",
            "",
            "",
            style="dim",
            end_section=True,
        )
    console.print(table)


def _export_address_json(result: AddressTraceResult, path: str) -> None:
    import json as _json
    from .models import AddressTraceResult

    payload = {
        "target_address": result.target_address,
        "total_transactions": result.total_transactions,
        "addresses": [
            {
                "address": n.address,
                "address_type": n.address_type,
                "total_ada": n.total_ada,
                "net_ada": n.net_ada,
                "total_incoming_ada": n.total_incoming_ada,
                "total_outgoing_ada": n.total_outgoing_ada,
                "tx_count": n.tx_count,
                "is_cex": n.is_cex,
                "cex_name": n.cex_name,
                "cex_user": n.cex_user,
                "is_target": n.is_target,
            }
            for n in result.addresses
        ],
        "edges": [
            {
                "source": e.source,
                "target": e.target,
                "tx_hashes": e.tx_hashes,
                "interaction_count": e.interaction_count,
                "direction_relative_to_target": e.direction_relative_to_target,
            }
            for e in result.edges
        ],
        "error": result.error,
        "provider_name": result.provider_name,
        "direction": getattr(result, "direction", "both"),
    }
    Path(path).write_text(_json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _export_address_csv(result: AddressTraceResult, base_path: str) -> tuple[str, str]:
    import csv as _csvmodule
    from .models import AddressTraceResult

    base = Path(base_path)
    if base.suffix.lower() == ".csv":
        stem = base.with_suffix("")
        nodes_path = str(stem) + "_addresses.csv"
        edges_path = str(stem) + "_edges.csv"
    else:
        base.mkdir(parents=True, exist_ok=True)
        nodes_path = str(base / "addresses.csv")
        edges_path = str(base / "edges.csv")
    with open(nodes_path, "w", newline="", encoding="utf-8") as f:
        w = _csvmodule.writer(f)
        w.writerow(
            [
                "address",
                "type",
                "total_ada",
                "tx_count",
                "is_cex",
                "cex_name",
                "cex_user",
                "is_target",
            ]
        )
        for n in result.addresses:
            w.writerow(
                [
                    n.address,
                    n.address_type,
                    n.total_ada,
                    n.tx_count,
                    n.is_cex,
                    n.cex_name,
                    n.cex_user,
                    n.is_target,
                ]
            )
    with open(edges_path, "w", newline="", encoding="utf-8") as f:
        w = _csvmodule.writer(f)
        w.writerow(["source", "target", "interaction_count", "tx_hashes"])
        for e in result.edges:
            w.writerow([e.source, e.target, e.interaction_count, ";".join(e.tx_hashes)])
    console.print(f"[green]Wrote addresses CSV:[/green] {nodes_path}")
    console.print(f"[green]Wrote edges CSV:[/green] {edges_path}")
    return nodes_path, edges_path


@main.group("config", help="Manage stored configuration.")
def config_group() -> None:
    pass


@config_group.command("set")
@click.option("--provider", type=click.Choice(PROVIDER_CHOICES), required=True)
@click.option("--api-key", type=str, default=None)
@click.option("--base-url", type=str, default=None)
@click.option("--auth-type", type=click.Choice(AUTH_TYPE_CHOICES), default=None)
@click.option("--endpoint-url", type=str, default=None)
@click.option("--kupo-url", type=str, default=None)
@click.option("--ogmios-url", type=str, default=None)
@click.option("--kupo-api-key", type=str, default=None)
@click.option("--ogmios-api-key", type=str, default=None)
@click.option("--make-default/--no-default", default=True)
def config_set(
    provider,
    api_key,
    base_url,
    auth_type,
    endpoint_url,
    kupo_url,
    ogmios_url,
    kupo_api_key,
    ogmios_api_key,
    make_default,
):
    cfg = set_provider_config(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        auth_type=auth_type,
        endpoint_url=endpoint_url,
        kupo_url=kupo_url,
        ogmios_url=ogmios_url,
        kupo_api_key=kupo_api_key,
        ogmios_api_key=ogmios_api_key,
        make_default=make_default,
    )
    console.print(f"[green]Saved config for provider '{provider}'.[/green]")
    console.print(f"[dim]Default provider: {cfg.get('default_provider')}[/dim]")


@config_group.command("show")
def config_show() -> None:
    cfg = load_config()

    def _redact(d: dict) -> dict:
        out = {}
        for k, v in d.items():
            if isinstance(v, dict):
                out[k] = _redact(v)
            elif "key" in k.lower() and isinstance(v, str) and v:
                out[k] = v[:4] + "***" + v[-2:]
            else:
                out[k] = v
        return out

    console.print(jsonlib.dumps(_redact(cfg), indent=2))


@config_group.command("clear")
def config_clear() -> None:
    if clear_config():
        console.print("[green]Config cleared.[/green]")
    else:
        console.print("[yellow]No config file to clear.[/yellow]")


@main.command(
    "health",
    help="Check provider connectivity. Without --provider, checks all configured providers.",
)
@connection_options
@click.option("--fallback/--no-fallback", default=True)
def health_cmd(
    provider,
    api_key,
    base_url,
    auth_type,
    endpoint_url,
    kupo_url,
    ogmios_url,
    fallback,
    use_proxy,
    proxy_url,
):
    cfg = load_config()
    overrides = _collect_overrides(
        api_key=api_key,
        base_url=base_url,
        auth_type=auth_type,
        endpoint_url=endpoint_url,
        kupo_url=kupo_url,
        ogmios_url=ogmios_url,
    )

    if provider:
        # ── Single provider mode ─────────────────────────────────
        from .providers.rotating import RotatingKeyProvider

        provider_name = provider
        prov = _build_providers(
            provider_name,
            cfg,
            use_fallback=fallback,
            api_key=api_key,
            base_url=base_url,
            auth_type=auth_type,
            endpoint_url=endpoint_url,
            kupo_url=kupo_url,
            ogmios_url=ogmios_url,
            use_proxy=use_proxy,
            proxy_url=proxy_url,
        )

        # Multi-key: unwrap FallbackProvider (from --fallback) to check per-key health
        from .providers.fallback import FallbackProvider

        check_prov = prov
        if isinstance(prov, FallbackProvider) and prov._providers:
            check_prov = prov._providers[0]
        if isinstance(check_prov, RotatingKeyProvider):
            key_infos: list[str] = []
            all_ok = True
            for i, (kname, kinst) in enumerate(check_prov._instances):

                async def _check_k(i=i, kinst=kinst):
                    async with kinst as kp:
                        return await kp.health_check()

                k_ok = asyncio.run(_check_k())
                label = f"k{i}" if kname.startswith(f"{provider_name}-") else kname
                if k_ok:
                    key_infos.append(f"[green]{label}:✓[/]")
                else:
                    key_infos.append(f"[red]{label}:✗[/]")
                    all_ok = False
            status = "[green]✓ OK[/green]" if all_ok else "[red]✗ FAIL[/red]"
            console.print(
                f"{provider_name} ({len(key_infos)} keys): {' '.join(key_infos)}  {status}"
            )
            if not all_ok:
                sys.exit(1)
            return

        async def _check_single():
            async with prov as p:
                return await p.health_check()

        ok = asyncio.run(_check_single())
        if ok:
            provider_type = getattr(prov, "current_provider", "") or provider_name
            primary_ok = provider_type == provider_name or provider_type.startswith(
                f"{provider_name}:"
            )
            if primary_ok or not fallback:
                console.print(f"[green]✓ {provider_type} is reachable[/green]")
                console.print("[green]OK[/green] provider is reachable.")
            else:
                console.print(f"[yellow]⚠ {provider_name} is not reachable[/yellow]")
                console.print(f"[green]✓ Fallback {provider_type} is reachable[/green]")
                console.print("[yellow]OK[/yellow] using fallback provider.")
        else:
            err_console.print(f"[red]✗ {provider_name} is not reachable[/red]")
            sys.exit(1)
        return

    # ── All-providers mode ───────────────────────────────────────
    _ALL_PROVIDERS = PROVIDER_CHOICES
    from .providers import build_provider as _build_single

    async def _check_one(pname: str) -> tuple[str, bool, str, str]:
        """Check a single provider. Returns (name, ok, detail, error)."""
        import httpx
        from utxo_tracer.providers.blockfrost import BlockfrostProvider
        from utxo_tracer.providers.koios import KoiosProvider
        from utxo_tracer.providers.maestro import MaestroProvider
        from utxo_tracer.providers.kupmios import KupmiosProvider
        from utxo_tracer.providers.utxorpc import UTxORPCProvider
        from utxo_tracer.providers.rotating import RotatingKeyProvider

        try:
            p_cfg = (cfg.get("providers") or {}).get(pname, {}) or {}
            p = _build_single(
                pname,
                p_cfg,
                use_proxy=use_proxy,
                proxy_url=proxy_url,
                overrides=overrides,
            )
        except Exception as e:
            msg = str(e).replace("\n", " ").strip()[:80]
            return (pname, False, pname, msg)

        # ── Multi-key providers: check ALL keys ──────────────────────
        if isinstance(p, RotatingKeyProvider):
            keys_ok = 0
            keys_total = len(p._instances)
            key_results: list[tuple[str, bool]] = []
            for kname, kinst in p._instances:
                try:
                    async with kinst as kp:
                        k_ok = await kp.health_check()
                except Exception:
                    k_ok = False
                if k_ok:
                    keys_ok += 1
                key_results.append((kname, k_ok))

            all_ok = keys_ok == keys_total
            detail = f"{pname} ({keys_ok}/{keys_total})"
            key_labels = " ".join(
                f"[{'green' if k_ok else 'red'}]{i}:{'✓' if k_ok else '✗'}[/]"
                for i, (_kn, k_ok) in enumerate(key_results)
            )

            if all_ok:
                return (pname, True, detail + " " + key_labels, "")
            else:
                fails = [
                    f"key-{i}" for i, (_kn, k_ok) in enumerate(key_results) if not k_ok
                ]
                return (pname, False, detail, ", ".join(fails))

        # ── Single-key / non-rotating providers ──────────────────────
        real = p

        async with p as prov:
            try:
                ok = await real.health_check()
            except Exception as e:
                msg = str(e).replace("\n", " ").strip()[:80]
                return (pname, False, pname, msg)

            if ok:
                detail = getattr(real, "current_provider", "") or pname
                # Check Ogmios status if Kupmios
                og_extra = ""
                if isinstance(real, KupmiosProvider):
                    og_ok = getattr(real, "_ogmios_ok", False)
                    og_extra = " (Ogmios ✓)" if og_ok else " (Ogmios ✗)"
                return (pname, True, detail + og_extra, "")

            # health_check returned False — probe real HTTP client for the error
            try:
                if isinstance(real, BlockfrostProvider):
                    r = await real._client.get("/blocks/latest")
                    e_msg = f"HTTP {r.status_code}: {r.text[:60]}"
                elif isinstance(real, KoiosProvider):
                    r = await real._client.post("/tip", json={})
                    e_msg = f"HTTP {r.status_code}: {r.text[:60]}"
                elif isinstance(real, MaestroProvider):
                    r = await real._client.get("/chain-tip")
                    e_msg = f"HTTP {r.status_code}: {r.text[:60]}"
                elif isinstance(real, KupmiosProvider):
                    r = await real._kupo.get("/health")
                    if r.status_code in (200, 204):
                        # Kupo OK — check Ogmios via GET /health
                        omsg = ""
                        try:
                            ro = await real._ogmios.get("/health")
                            if ro.status_code != 200:
                                omsg = f"HTTP {ro.status_code}"
                        except Exception as oe:
                            omsg = str(oe).replace("\n", " ").strip()[:40]
                        e_msg = f"Kupo OK, Ogmios fail: {omsg}" if omsg else ""
                    else:
                        e_msg = f"HTTP {r.status_code}: {r.text[:60]}"
                elif isinstance(real, UTxORPCProvider):
                    e_msg = "unreachable (gRPC)"
                    # Try a simple connection test to get the actual error
                    try:
                        from utxorpc.query import CardanoQueryClient

                        # Build the same URI the provider uses
                        uri = getattr(real, "_uri", "")
                        if uri:
                            md = real._metadata()
                            test_qc = CardanoQueryClient(uri=uri, metadata=md)
                            await test_qc.async_connect().__aenter__()
                            try:
                                await asyncio.wait_for(
                                    test_qc.async_read_params(), timeout=5.0
                                )
                                e_msg = "SDK connected, but health_check failed"
                            except asyncio.TimeoutError:
                                e_msg = "gRPC timeout (5s)"
                            except Exception as ge:
                                e_msg = str(ge).replace("\n", " ").strip()[:60]
                            finally:
                                await test_qc.async_connect().__aexit__(
                                    None, None, None
                                )
                    except Exception as ge:
                        e_msg = str(ge).replace("\n", " ").strip()[:60]
                else:
                    e_msg = "unreachable"
            except httpx.ConnectError as ce:
                e_msg = str(ce).replace("\n", " ").strip()[:80]
            except httpx.TimeoutException as te:
                e_msg = str(te).replace("\n", " ").strip()[:80]
            except Exception as pe:
                e_msg = str(pe).replace("\n", " ").strip()[:80]

            return (pname, False, pname, e_msg)

    async def _run_all() -> list[tuple[str, bool, str, str]]:
        return await asyncio.gather(*[_check_one(pn) for pn in _ALL_PROVIDERS])

    results = asyncio.run(_run_all())

    table = Table(title="Provider Health", header_style="bold cyan")
    table.add_column("Provider", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Detail", overflow="fold")
    table.add_column("Error", overflow="fold", max_width=40)

    any_ok = False
    for name, ok, detail, err in results:
        if ok:
            any_ok = True
            table.add_row(name, "[green]✓ OK[/green]", detail, "[dim]—[/dim]")
        else:
            display_name = detail if detail != name else name
            table.add_row(
                name, "[red]✗ FAIL[/red]", display_name, f"[red]{err[:50]}[/red]"
            )
    console.print(table)

    if not any_ok:
        err_console.print("[red]No providers are reachable.[/red]")
        sys.exit(1)


@main.command("assets", help="Show asset breakdown for a single UTXO.")
@click.argument("utxo", type=str)
@connection_options
def assets_cmd(
    utxo,
    provider,
    api_key,
    base_url,
    auth_type,
    endpoint_url,
    kupo_url,
    ogmios_url,
    use_proxy,
    proxy_url,
):
    cfg = load_config()
    try:
        out_ref = parse_out_ref(utxo)
    except ValueError as e:
        _fatal(str(e))
    provider_name = _resolve_provider_name(provider, cfg)
    provider_cfg = (cfg.get("providers") or {}).get(provider_name, {}) or {}
    overrides = _collect_overrides(
        api_key=api_key,
        base_url=base_url,
        auth_type=auth_type,
        endpoint_url=endpoint_url,
        kupo_url=kupo_url,
        ogmios_url=ogmios_url,
    )
    prov = build_provider(
        provider_name,
        provider_cfg,
        use_proxy=use_proxy,
        proxy_url=proxy_url,
        overrides=overrides,
    )

    async def _fetch():
        async with prov as p:
            return await p.get_utxo_by_out_ref(out_ref)

    node = asyncio.run(_fetch())
    if not node:
        _fatal(f"UTXO not found: {out_ref}")
    console.print(
        Panel.fit(
            "\n".join(
                [
                    f"[bold]UTXO:[/bold] {node.out_ref}",
                    f"[bold]Address:[/bold] {node.address}",
                    f"[bold]Lovelace:[/bold] {node.lovelace:,}",
                    f"[bold]ADA:[/bold] {lovelace_to_ada(node.lovelace)}",
                    f"[bold]Datum hash:[/bold] {node.datum_hash or '-'}",
                    f"[bold]Inline datum:[/bold] {'yes' if node.inline_datum else 'no'}",
                    f"[bold]Script ref:[/bold] {node.script_ref or '-'}",
                ]
            ),
            title="UTXO",
            border_style="cyan",
        )
    )
    table = Table(title="Assets", header_style="bold magenta")
    table.add_column("Policy ID", overflow="fold")
    table.add_column("Asset Name")
    table.add_column("Unit", overflow="fold")
    table.add_column("Quantity", justify="right")
    for a in node.assets:
        table.add_row(
            a.policy_id or "(lovelace)", a.asset_name or "-", a.unit, f"{a.quantity:,}"
        )
    console.print(table)


# cache commands


@main.group("cache", help="Manage local trace cache.")
def cache_group() -> None:
    pass


@cache_group.command("list", help="List cached traces.")
def cache_list() -> None:
    entries = cache_mod.list_traces()
    if not entries:
        Console().print("[yellow]No cached traces.[/yellow]")
        return
    table = Table(title=f"Cached Traces ({len(entries)})")
    table.add_column("Key", style="cyan")
    table.add_column("Start UTXO")
    table.add_column("Dir")
    table.add_column("Depth")
    table.add_column("Nodes")
    table.add_column("ADA")
    table.add_column("Provider")
    table.add_column("Exists")
    for e in entries:
        table.add_row(
            e.get("start", "?")[:18],
            e.get("start", "")[:24],
            str(e.get("direction", "?")),
            str(e.get("max_depth", "?")),
            str(e.get("nodes", "?")),
            str(e.get("total_ada", "?")),
            str(e.get("provider", "?")),
            "\u2713" if e.get("exists") else "\u2717",
        )
    Console().print(table)


@cache_group.command("clear", help="Remove all cached traces.")
def cache_clear() -> None:
    count = cache_mod.clear_cache()
    Console().print(f"[green]Cleared {count} cached traces.[/green]")


@cache_group.command("info", help="Show cache storage info.")
def cache_info() -> None:
    from pathlib import Path

    cache_dir = cache_mod.CACHE_DIR
    size = 0
    if cache_dir.exists():
        for f in cache_dir.rglob("*"):
            if f.is_file():
                size += f.stat().st_size
    summary = cache_mod.store_summary()
    store_ok = cache_mod.DB_PATH.exists()
    table = Table(title="Cache Info")
    table.add_column("Property")
    table.add_column("Value")
    table.add_row("Cache dir", str(cache_dir))
    table.add_row("Trace files", str(len(list(cache_dir.glob("traces/*.json")))))
    table.add_row(
        "Store nodes",
        str(summary["nodes"]) + (" \u2713" if store_ok else " (no store)"),
    )
    table.add_row("Store tx inputs", str(summary["inputs"]))
    table.add_row("Store transactions", str(summary["transactions"]))
    table.add_row("Size", f"{size / 1024:.1f} KB")
    Console().print(table)


@main.command("open", help="Open cached trace visualization.")
@click.argument("cache_key")
def open_cmd(cache_key: str) -> None:
    """Open a cached trace visualization from SQLite."""
    from .graph.g6_viz import start_server

    # Find trace in SQLite by scanning snapshots
    conn = cache_mod._get_db()
    row = conn.execute(
        "SELECT trace_key, trace_type, metadata, data FROM trace_snapshots WHERE trace_key = ? OR trace_key LIKE ?",
        (cache_key, f"{cache_key}%"),
    ).fetchone()
    if row is None:
        Console().print(f"[red]Cache key '{cache_key}' not found in SQLite.[/red]")
        Console().print(
            "Run [yellow]utxo-tracer cache list[/yellow] to see available keys."
        )
        return

    import json

    metadata = json.loads(row["metadata"])
    data = json.loads(row["data"])
    trace_type = row["trace_type"]

    if trace_type == "utxo":
        from . import cache as _cache_mod

        start_str = metadata.get("start", "#0")
        if "#" in start_str:
            tx_hash, idx_s = start_str.rsplit("#", 1)
            start_ref = OutRef(tx_hash, int(idx_s))
        else:
            start_ref = OutRef(start_str, 0)
        result = _cache_mod.load_trace(
            start_ref,
            metadata.get("direction", "backward"),
            metadata.get("max_depth", 0),
        )
        if result is None:
            Console().print("[red]Failed to parse cached trace data.[/red]")
            return
        start_server(result, start_out_ref=start_ref, cache_key=cache_key)
    elif trace_type == "address":
        from .graph.g6_viz import start_address_server
        from . import cache as _cache_mod

        address = metadata.get("target_address", "")
        if not address:
            Console().print("[red]No target address in cached trace.[/red]")
            return
        _addr_dir = data.get("direction", "both")
        result = _cache_mod.load_address_trace(
            address,
            tx_limit=data.get("tx_limit", 0),
            max_depth=data.get("max_depth", 1),
            direction=_addr_dir,
        )
        if result is None:
            Console().print("[red]Failed to parse cached address trace.[/red]")
            return
        start_address_server(
            result,
            target_address=address,
            cache_key=_cache_mod._addr_cache_key(address, direction=_addr_dir),
        )


# CEX cashflow commands


@main.group("cex", help="CEX address detection and cashflow reconciliation.")
def cex_group() -> None:
    pass


@cex_group.command(
    "cashflow",
    help="""Reconcile CEX deposit/withdrawal records around a specific UTXO's time.

The time window is auto-calculated from the UTXO's block time ± window hours.
This is more precise than a broad time range — it focuses on the exact moment
the on-chain transaction occurred.

Credentials loaded from env vars by default (BINANCE_API_KEY, etc.).
Override with --api-key / --api-secret flags.

Examples:
  # Uses BINANCE_API_KEY env var, resolves UTXO time, ±24h window
  utxo-tracer cex cashflow binance abc123...#0 --provider blockfrost --bf-api-key xxx

  # Custom window
  utxo-tracer cex cashflow binance abc123...#0 --window 48 --provider blockfrost --bf-api-key xxx

  # Export CSV
  utxo-tracer cex cashflow binance abc123...#0 --csv report.csv --provider blockfrost --bf-api-key xxx
""",
)
@click.argument("exchange", type=str)
@click.argument("utxo", type=str)
@click.option(
    "--api-key", type=str, default=None, help="Override API key (default: from env)"
)
@click.option(
    "--api-secret",
    type=str,
    default=None,
    help="Override API secret (default: from env)",
)
@click.option(
    "--api-passphrase",
    type=str,
    default=None,
    help="Override API passphrase (KuCoin/OKX)",
)
@click.option(
    "--window",
    type=int,
    default=24,
    help="Hours before+after UTXO time to search CEX records (default: 24)",
)
@click.option("--currency", type=str, default="ADA")
@click.option("--base-url", type=str, default=None)
@click.option(
    "--output", type=click.Choice(["table", "json", "summary"]), default="table"
)
@click.option("--csv", type=str, default=None, help="Export results to CSV file")
@click.option(
    "--provider",
    type=str,
    required=True,
    help="Cardano provider to resolve UTXO time (blockfrost/koios)",
)
@click.option("--bf-api-key", type=str, default=None)
@click.option(
    "--consolidation/--no-consolidation",
    default=False,
    help="Detect CEX consolidation patterns",
)
def cashflow_cmd(
    exchange,
    utxo,
    api_key,
    api_secret,
    api_passphrase,
    window,
    currency,
    base_url,
    output,
    csv,
    provider,
    bf_api_key,
    consolidation,
):
    from .cex.api import build_cex_client
    from .cex.flow import CashflowReconciler, resolve_utxo_time, format_time_window
    from .config import load_config

    cfg = load_config()

    # Build on-chain provider to resolve UTXO time
    prov_name = _resolve_provider_name(provider, cfg)
    prov_cfg = (cfg.get("providers") or {}).get(prov_name, {}) or {}
    if bf_api_key or prov_cfg.get("api_key"):
        from .providers import build_provider

        onchain_provider = build_provider(
            prov_name,
            prov_cfg,
            overrides={"api_key": bf_api_key} if bf_api_key else {},
        )
    else:
        console.print(
            "[red]Need --provider and --bf-api-key to resolve UTXO time.[/red]"
        )
        sys.exit(1)

    async def _run():
        # 1. Resolve UTXO → block time
        console.print(f"[cyan]Resolving time for UTXO {utxo}...[/cyan]")
        block_time, out_ref = await resolve_utxo_time(onchain_provider, utxo)
        start_ts, end_ts = format_time_window(block_time, window, window)
        console.print(f"  Block time: {block_time} ({_fmt_ts(block_time)})")
        console.print(f"  Window: {window}h before → {window}h after")
        console.print(f"  CEX query range: {start_ts} → {end_ts}")

        # 2. Reconcile CEX records in that window
        from .cex.matching.consolidation import detect_consolidations
        from .cex.matching.registry_populate import auto_register_from_matches

        async with CashflowReconciler(onchain_provider=onchain_provider) as reconciler:
            kwargs = {"api_key": api_key, "api_secret": api_secret}
            if api_passphrase:
                kwargs["api_passphrase"] = api_passphrase
            if base_url:
                kwargs["base_url"] = base_url
            summary = await reconciler.reconcile(
                exchange=exchange,
                start_time=start_ts,
                end_time=end_ts,
                currency=currency,
                **kwargs,
            )

            auto_reg_count = auto_register_from_matches(summary.matches)
            if auto_reg_count > 0:
                logger.info("Auto-registered %d CEX addresses", auto_reg_count)

            consolidation_patterns = []
            if consolidation and summary.unmatched_onchain_records:
                from .cex.registry import get_all_cex_addresses

                known_addrs = set(get_all_cex_addresses().keys())
                consolidation_patterns = detect_consolidations(
                    summary.unmatched_onchain_records, known_addrs
                )

            if output == "json":
                from dataclasses import asdict
                import json

                payload = asdict(summary)
                payload["block_time"] = block_time
                payload["utxo"] = utxo
                payload["consolidation_patterns"] = [
                    {
                        "tx_hash": c.tx_hash,
                        "input_count": c.input_count,
                        "total_input_ada": c.total_input_ada,
                        "hot_wallet_address": c.hot_wallet_address,
                    }
                    for c in consolidation_patterns
                ]
                click.echo(json.dumps(payload, default=str, indent=2))
            elif output == "summary":
                console.print(reconciler.format_summary(summary))
            else:
                console.print(reconciler.format_summary(summary))
                _print_cashflow_matches_table(summary)

            if csv:
                _export_cashflow_csv(summary, csv)

            if consolidation_patterns:
                _print_consolidation_report(consolidation_patterns, exchange)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("[yellow]Cancelled[/yellow]")
        sys.exit(130)
    finally:
        if onchain_provider is not None:
            asyncio.run(onchain_provider.aclose())


def _print_cashflow_matches_table(summary) -> None:
    if not summary.matches:
        return
    table = Table(
        title=f"Cashflow Matches ({len(summary.matches)})", header_style="bold green"
    )
    table.add_column("Type")
    table.add_column("Amount")
    table.add_column("CEX Address", overflow="fold")
    table.add_column("Confidence")
    table.add_column("Match")
    table.add_column("On-Chain Addr", overflow="fold")
    for m in summary.matches:
        type_str = (
            "[bold yellow]DEPOSIT[/bold yellow]"
            if m.cex_record.is_deposit
            else "[bold cyan]WITHDRAW[/bold cyan]"
        )
        amount_str = f"{m.cex_record.amount:.2f} ADA"
        addr = (
            m.cex_record.address[:16] + "\u2026"
            if len(m.cex_record.address) > 16
            else m.cex_record.address
        )
        mtype_str = {"txid": "[green]txid[/green]"}.get(
            m.match_type, f"[dim]{m.match_type}[/dim]"
        )
        onchain_addrs = ", ".join(
            oc.address[:16] + "\u2026" if len(oc.address) > 16 else oc.address
            for oc in m.onchain_records[:2]
        )
        if len(m.onchain_records) > 2:
            onchain_addrs += f" [+{len(m.onchain_records) - 2}]"
        table.add_row(
            type_str,
            amount_str,
            addr,
            f"{m.confidence * 100:.0f}%",
            mtype_str,
            onchain_addrs,
        )
    console.print(table)
    if summary.unmatched_cex_records:
        utable = Table(
            title=f"Unmatched CEX Records ({len(summary.unmatched_cex_records)})",
            header_style="bold red",
        )
        utable.add_column("Type")
        utable.add_column("Amount")
        utable.add_column("Address", overflow="fold")
        utable.add_column("Has TXID")
        utable.add_column("Timestamp")
        for r in summary.unmatched_cex_records[:15]:
            utable.add_row(
                "[yellow]DEPOSIT[/yellow]" if r.is_deposit else "[cyan]WITHDRAW[/cyan]",
                f"{r.amount:.2f} ADA",
                r.address[:16] + "\u2026" if len(r.address) > 16 else r.address,
                "[green]yes[/green]" if r.has_txid else "[red]no[/red]",
                str(r.timestamp),
            )
        if len(summary.unmatched_cex_records) > 15:
            utable.add_row(
                f"[dim]\u2026 {len(summary.unmatched_cex_records) - 15} more[/dim]",
                "",
                "",
                "",
                "",
            )
        console.print(utable)


def _export_cashflow_csv(summary, path: str) -> None:
    """Export cashflow matches to CSV file."""
    import csv
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "type",
                "amount",
                "cex_address",
                "cex_txid",
                "confidence",
                "match_type",
                "onchain_tx_hash",
                "onchain_address",
                "onchain_amount",
            ]
        )
        for m in summary.matches:
            for oc in m.onchain_records:
                w.writerow(
                    [
                        m.cex_record.tx_type,
                        m.cex_record.amount,
                        m.cex_record.address,
                        m.cex_record.txid or "",
                        f"{m.confidence:.4f}",
                        m.match_type,
                        oc.tx_hash,
                        oc.address,
                        oc.amount_ada,
                    ]
                )

    console.print(f"[green]Wrote cashflow CSV:[/green] {p}")


def _print_consolidation_report(patterns, exchange: str) -> None:
    """Print CEX consolidation patterns as a Rich table."""
    if not patterns:
        return

    from rich.table import Table

    table = Table(
        title=f"CEX Consolidation Patterns ({len(patterns)}) — {exchange.upper()}",
        header_style="bold magenta",
    )
    table.add_column("TX Hash", overflow="fold")
    table.add_column("Inputs")
    table.add_column("Total ADA", justify="right")
    table.add_column("Hot Wallet", overflow="fold")

    for c in patterns[:10]:
        table.add_row(
            c.tx_hash[:20] + "\u2026" if len(c.tx_hash) > 20 else c.tx_hash,
            str(c.input_count),
            f"{c.total_input_ada:,.2f}",
            c.hot_wallet_address[:16] + "\u2026"
            if len(c.hot_wallet_address) > 16
            else c.hot_wallet_address,
        )

    if len(patterns) > 10:
        table.add_row(f"[dim]\u2026 {len(patterns) - 10} more[/dim]", "", "", "")

    console.print(table)


@cex_group.command(
    "env", help="Show available CEX env var names and check which are set."
)
def cex_env_cmd():
    """Show available CEX configuration via environment variables."""
    import os

    env_vars = {
        "BINANCE_API_KEY": "Binance API key",
        "BINANCE_API_SECRET": "Binance API secret",
        "BYBIT_API_KEY": "Bybit API key",
        "BYBIT_API_SECRET": "Bybit API secret",
        "KUCOIN_API_KEY": "KuCoin API key",
        "KUCOIN_API_SECRET": "KuCoin API secret",
        "KUCOIN_API_PASSPHRASE": "KuCoin API passphrase",
        "OKX_API_KEY": "OKX API key",
        "OKX_API_SECRET": "OKX API secret",
        "OKX_API_PASSPHRASE": "OKX API passphrase",
    }

    table = Table(title="CEX Environment Variables", header_style="bold cyan")
    table.add_column("Env Var")
    table.add_column("Description")
    table.add_column("Status")

    for var, desc in sorted(env_vars.items()):
        val = os.environ.get(var)
        if val:
            redacted = val[:4] + "****" if len(val) > 6 else "****"
            status = f"[green]set[/green] ({redacted})"
        else:
            status = "[dim]not set[/dim]"
        table.add_row(var, desc, status)

    console.print(table)
    console.print()
    console.print("Add to [yellow]~/.utxo-tracer/.env[/yellow] or export in shell:")
    console.print("  [dim]# Example:[/dim]")
    console.print("  [green]BINANCE_API_KEY[/green]=your_api_key")
    console.print("  [green]BINANCE_API_SECRET[/green]=your_api_secret")
    console.print("  [green]KUCOIN_API_KEY[/green]=your_kucoin_key")
    console.print("  [green]KUCOIN_API_PASSPHRASE[/green]=your_passphrase")


# ── CEX import/report/cache subcommands ─────────────────────


@cex_group.command(
    "import", help="Import CEX records from CSV/JSON for offline reconciliation."
)
@click.argument("file", type=click.Path(exists=True))
@click.option(
    "--exchange",
    type=str,
    default=None,
    help="Override exchange name (auto-detected if in file)",
)
@click.option(
    "--start-ts", type=int, default=None, help="Filter: start time (Unix epoch)"
)
@click.option("--end-ts", type=int, default=None, help="Filter: end time (Unix epoch)")
@click.option("--output", type=click.Choice(["table", "summary"]), default="table")
@click.option(
    "--onchain",
    is_flag=True,
    default=False,
    help="Attempt on-chain cross-reference (requires Blockfrost)",
)
@click.option(
    "--bf-api-key",
    type=str,
    default=None,
    help="Blockfrost API key for on-chain queries",
)
def cex_import_cmd(file, exchange, start_ts, end_ts, output, onchain, bf_api_key):
    """Import CEX records from CSV/JSON and run reconciliation."""
    from .cex.flow import import_from_csv, import_from_json, CashflowReconciler

    path = str(file)
    if path.endswith(".json"):
        records = import_from_json(path, exchange)
    else:
        records = import_from_csv(path, exchange)

    if not records:
        console.print("[yellow]No records found in file.[/yellow]")
        return

    # Filter by time range
    if start_ts is not None:
        records = [r for r in records if r.timestamp >= start_ts]
    if end_ts is not None:
        records = [r for r in records if r.timestamp <= end_ts]

    console.print(f"[green]Loaded {len(records)} records from {path}[/green]")

    # On-chain provider
    onchain_provider = None
    if onchain and bf_api_key:
        from .providers import build_provider

        onchain_provider = build_provider(
            "blockfrost", {"api_key": bf_api_key, "auth_type": "project_id"}
        )

    async def _run():
        async with CashflowReconciler(onchain_provider=onchain_provider) as reconciler:
            summary = await reconciler.reconcile_with_records(
                exchange=records[0].exchange,
                cex_records=records,
                onchain_records=None,
            )
            if output == "summary":
                console.print(reconciler.format_summary(summary))
            else:
                console.print(reconciler.format_summary(summary))
                _print_cashflow_matches_table(summary)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("[yellow]Cancelled[/yellow]")
        sys.exit(130)
    finally:
        if onchain_provider is not None:
            asyncio.run(onchain_provider.aclose())


@cex_group.command("report", help="Generate HTML cashflow report from cached results.")
@click.argument("exchange", type=str)
@click.option("--start-ts", type=int, required=True)
@click.option("--end-ts", type=int, required=True)
@click.option("--output", type=click.Path(), default=None, help="Output HTML file path")
def cex_report_cmd(exchange, start_ts, end_ts, output):
    """Generate HTML report from cached cashflow."""
    from .cex.flow import load_cashflow, generate_html_report

    summary = load_cashflow(exchange, start_ts, end_ts)
    if summary is None:
        console.print(
            f"[red]No cached cashflow for '{exchange}' {start_ts}-{end_ts}.[/red]"
        )
        console.print(
            "Run [yellow]utxo-tracer cex cashflow[/yellow] first to create a cache."
        )
        return

    html = generate_html_report(summary, f"CEX Cashflow — {exchange.upper()}")
    out_path = output or f"cashflow_report_{exchange}_{start_ts}_{end_ts}.html"
    Path(out_path).write_text(html, encoding="utf-8")
    console.print(f"[green]Wrote HTML report:[/green] {out_path}")


@cex_group.group("cache", help="Manage cached cashflow results.")
def cex_cache_group() -> None:
    pass


@cex_cache_group.command("list")
def cex_cache_list():
    """List cached cashflow reconciliation results."""
    from .cex.flow import list_cached_cashflows

    entries = list_cached_cashflows()
    if not entries:
        console.print("[yellow]No cached cashflow results.[/yellow]")
        return

    table = Table(title=f"Cached Cashflows ({len(entries)})")
    table.add_column("Exchange")
    table.add_column("Start")
    table.add_column("End")
    table.add_column("Matches")
    table.add_column("Unmatched")
    table.add_column("Inflow")
    table.add_column("Outflow")
    for e in entries:
        table.add_row(
            e["exchange"],
            str(e["start"]),
            str(e["end"][:16] if len(str(e["end"])) > 16 else str(e["end"])),
            str(e["matches"]),
            str(e["unmatched"]),
            f"{e['inflow']:.0f}",
            f"{e['outflow']:.0f}",
        )
    console.print(table)


@cex_cache_group.command("clear")
@click.option("--exchange", type=str, default=None, help="Clear only specific exchange")
def cex_cache_clear(exchange):
    """Clear cached cashflow results."""
    from .cex.flow import clear_cashflow_cache

    count = clear_cashflow_cache(exchange)
    console.print(f"[green]Cleared {count} cached cashflow result(s).[/green]")


@cex_group.command("template", help="Write CSV template for manual CEX data entry.")
@click.argument("output", type=click.Path(), default="cex_template.csv")
def cex_template_cmd(output):
    """Write a CSV template file for manual CEX data entry."""
    from .cex.flow import write_csv_template

    write_csv_template(output)
    console.print(f"[green]Wrote CSV template:[/green] {output}")
    console.print("Fill in your CEX deposit/withdrawal data and use:")
    console.print(f"  [yellow]utxo-tracer cex import {output}[/yellow]")


@cex_group.command("reconcile-all", help="Run multi-CEX reconciliation.")
@click.option("--start-ts", type=int, required=True)
@click.option("--end-ts", type=int, required=True)
@click.option(
    "--exchanges",
    type=str,
    default=None,
    help="Comma-separated list of exchanges (default: all configured)",
)
def cex_reconcile_all_cmd(start_ts, end_ts, exchanges):
    """Run reconciliation across all configured (or specified) exchanges."""
    from .cex.flow import multi_cex_reconcile, format_multi_summary
    import os

    if exchanges:
        exchange_names = [e.strip().lower() for e in exchanges.split(",")]
    else:
        # Auto-detect available exchanges from env vars
        auto_exchanges = []
        if os.environ.get("BINANCE_API_KEY"):
            auto_exchanges.append("binance")
        if os.environ.get("BYBIT_API_KEY"):
            auto_exchanges.append("bybit")
        if os.environ.get("KUCOIN_API_KEY"):
            auto_exchanges.append("kucoin")
        if os.environ.get("OKX_API_KEY"):
            auto_exchanges.append("okx")

        # Also check config.json
        cfg = load_config()
        cex_cfg = cfg.get("cex", {})
        for name in cex_cfg:
            if name not in auto_exchanges:
                auto_exchanges.append(name)

        exchange_names = auto_exchanges

    if not exchange_names:
        console.print("[red]No exchanges configured. Set env vars like:[/red]")
        console.print(
            "  [green]BINANCE_API_KEY[/green]=xxx [green]BINANCE_API_SECRET[/green]=yyy"
        )
        console.print("Or see: [yellow]utxo-tracer cex env[/yellow]")
        sys.exit(1)

    configs = []
    for name in exchange_names:
        # Load creds from factory (env → config fallback)
        from .cex.api.factory import _load_cex_creds

        creds = _load_cex_creds(name)
        if not creds.get("api_key"):
            console.print(f"[yellow]No API key for '{name}', skipping.[/yellow]")
            continue
        configs.append(
            {
                "exchange": name,
                "api_key": creds["api_key"],
                "api_secret": creds.get("api_secret", ""),
                "api_passphrase": creds.get("api_passphrase"),
                "base_url": creds.get("base_url"),
            }
        )

    if not configs:
        console.print("[red]No usable exchange configurations found.[/red]")
        sys.exit(1)

    async def _run():
        results = await multi_cex_reconcile(
            configs,
            start_ts,
            end_ts,
            currency="ADA",
        )
        console.print(format_multi_summary(results))

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("[yellow]Cancelled[/yellow]")
        sys.exit(130)


@cex_group.command(
    "hacker-detect",
    help="Cross-reference UTXO trace with cashflow to find hacker's CEX addresses.\n\nTime window auto-calculated from UTXO block time ± window hours.\nCombines backward trace + CEX cashflow records in that window.",
)
@click.argument("utxo", type=str)
@click.option("--max-depth", type=int, default=None)
@click.option("--exchange", type=str, required=True, help="Exchange to match against")
@click.option(
    "--window", type=int, default=24, help="Hours before+after UTXO time (default: 24)"
)
@click.option("--api-key", type=str, default=None)
@click.option("--api-secret", type=str, default=None)
@click.option("--api-passphrase", type=str, default=None)
@click.option("--provider", type=str, default="blockfrost")
@click.option("--bf-api-key", type=str, default=None)
def cex_hacker_detect_cmd(
    utxo,
    max_depth,
    exchange,
    window,
    api_key,
    api_secret,
    api_passphrase,
    provider,
    bf_api_key,
):
    """Cross-reference UTXO trace with CEX cashflow to detect hacker's addresses."""
    from .cex.flow import CashflowReconciler, resolve_utxo_time, format_time_window
    from .cex.flow.hacker_detect import identify_hacker_cex_addresses
    from .cex.api.factory import _load_cex_creds

    cfg = load_config()

    # Load CEX creds from env/config
    creds = _load_cex_creds(exchange)
    if api_key:
        creds["api_key"] = api_key
    if api_secret:
        creds["api_secret"] = api_secret
    if api_passphrase:
        creds["api_passphrase"] = api_passphrase

    if not creds.get("api_key") or not creds.get("api_secret"):
        console.print(f"[red]No credentials for '{exchange}'.[/red]")
        console.print(
            f"Set env vars: [green]{exchange.upper()}_API_KEY[/green] [green]{exchange.upper()}_API_SECRET[/green]"
        )
        sys.exit(1)

    # Parse UTXO
    try:
        start_ref = parse_out_ref(utxo)
    except ValueError as e:
        _fatal(str(e))

    max_depth = max_depth or int((cfg.get("defaults") or {}).get("max_depth", 5))

    # Build providers
    onchain_provider = None
    if provider or bf_api_key:
        prov_name = _resolve_provider_name(provider, cfg)
        prov_cfg = (cfg.get("providers") or {}).get(prov_name, {}) or {}
        if bf_api_key or prov_cfg.get("api_key"):
            from .providers import build_provider

            onchain_provider = build_provider(
                prov_name,
                prov_cfg,
                overrides={"api_key": bf_api_key} if bf_api_key else {},
            )

    trace_provider = None
    if provider:
        trace_provider = onchain_provider

    async def _run():
        # Step 0: Resolve UTXO → block time → time window
        console.print(f"[cyan]Resolving time for UTXO {utxo}...[/cyan]")
        block_time, _ = await resolve_utxo_time(onchain_provider, utxo)
        st, et = format_time_window(block_time, window, window)
        console.print(f"  Block time: {block_time} ({_fmt_ts(block_time)})")
        console.print(f"  Window: ±{window}h → CEX query {st} → {et}")

        # Step 1: Run cashflow reconciliation
        async with CashflowReconciler(onchain_provider=onchain_provider) as reconciler:
            console.print(f"[cyan]Fetching CEX records from {exchange}...[/cyan]")
            summary = await reconciler.reconcile(
                exchange=exchange,
                api_key=creds.get("api_key", ""),
                api_secret=creds.get("api_secret", ""),
                api_passphrase=creds.get("api_passphrase"),
                start_time=st,
                end_time=et,
            )
            console.print(reconciler.format_summary(summary))

            # Step 2: Run UTXO trace if we have a provider
            if trace_provider:
                console.print(f"[cyan]Tracing UTXO {utxo}...[/cyan]")
                result = await _do_trace(
                    trace_provider,
                    start_ref,
                    "backward",
                    max_depth,
                )
                _print_summary(result)

                # Step 3: Cross-reference
                findings = identify_hacker_cex_addresses(result, summary)
                if findings:
                    ftable = Table(
                        title=f"Hacker CEX Addresses Found ({len(findings)})",
                        header_style="bold red",
                    )
                    ftable.add_column("Direction")
                    ftable.add_column("CEX")
                    ftable.add_column("Amount")
                    ftable.add_column("Address", overflow="fold")
                    for f in findings:
                        ftable.add_row(
                            f["direction"],
                            f["cex"],
                            f"{f['amount_ada']:.2f} ADA",
                            f["address"][:20] + "..."
                            if len(f["address"]) > 20
                            else f["address"],
                        )
                    console.print(ftable)
                else:
                    console.print(
                        "[yellow]No matching CEX addresses found in trace graph.[/yellow]"
                    )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("[yellow]Cancelled[/yellow]")
        sys.exit(130)
    finally:
        if onchain_provider is not None:
            asyncio.run(onchain_provider.aclose())


if __name__ == "__main__":
    main()
