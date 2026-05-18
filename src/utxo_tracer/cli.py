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
from threading import Timer
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
from .tracing import build_graph_from_steps, trace_backward, trace_forward
from .utils import lovelace_to_ada, parse_out_ref, shorten

logger = logging.getLogger(__name__)

console = Console()
err_console = Console(stderr=True)


def _fatal(msg: str, exit_code: int = 1) -> NoReturn:
    err_console.print(f"[bold red]Error:[/bold red] {msg}")
    sys.exit(exit_code)


def _open_browser() -> None:
    import webbrowser
    try:
        webbrowser.open("http://127.0.0.1:8050")
    except Exception:
        pass


def _dataclass_to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        d = asdict(obj)
        return d
    if isinstance(obj, list):
        return [_dataclass_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    return obj


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
    """Build a single provider or a FallbackProvider chain.

    When use_fallback=True, the specified provider is tried first, then
    utxorpc → blockfrost → koios → maestro in that order as fallback.
    """
    provider_cfg = (cfg.get("providers") or {}).get(name, {}) or {}
    overrides = {
        "api_key": api_key,
        "base_url": base_url,
        "auth_type": auth_type,
        "endpoint_url": endpoint_url,
        "kupo_url": kupo_url,
        "ogmios_url": ogmios_url,
    }
    overrides = {k: v for k, v in overrides.items() if v is not None}

    if not use_fallback:
        return build_provider(
            name,
            provider_cfg,
            use_proxy=use_proxy,
            proxy_url=proxy_url,
            overrides=overrides,
        )

    # Build fallback chain: primary first, then other providers
    _FALLBACK_ORDER = ["utxorpc", "blockfrost", "koios", "maestro"]
    order = _FALLBACK_ORDER[:]
    if name in order:
        order.remove(name)
    order.insert(0, name)  # primary first, then remaining chain

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
    table = Table(title="Nodes", header_style="bold magenta")
    table.add_column("Node", style="dim", overflow="fold")
    table.add_column("Address", overflow="fold")
    table.add_column("Type", justify="center")
    table.add_column("ADA", justify="right")
    table.add_column("Assets", overflow="fold")
    table.add_column("CEX")

    for n in result.nodes:
        cex = identify_cex(n.address)
        if cex:
            row_style = "bold red"
        elif n.ada >= 100:
            row_style = "yellow"
        else:
            row_style = "green"

        non_ada = [a for a in n.assets if not a.is_lovelace]
        if non_ada:
            asset_strs = [f"{a.unit}: {a.quantity:,}" for a in non_ada]
            # First asset on same line, rest joined
            assets_display = asset_strs[0]
            if len(asset_strs) > 1:
                assets_display += f" [dim](+{len(asset_strs)-1})[/dim]"
        else:
            assets_display = "-"

        _type_label_map = {
            "wallet": "[blue]W[/blue]",
            "script": "[yellow]S[/yellow]",
            "byron":  "[magenta]B[/magenta]",
            "stake":  "[green]K[/green]",
            "unknown": "[dim]?[/dim]",
        }
        type_display = _type_label_map.get(n.address_type, "[dim]?[/dim]")

        table.add_row(
            shorten(n.id, 18, 6),
            shorten(n.address, 14, 8),
            type_display,
            f"{n.ada:,.6f}",
            assets_display,
            f"{cex.name} ({cex.confidence})" if cex else "-",
            style=row_style,
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
                # Address type badge
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
    """Export nodes + edges as CSV files.

    If base_path ends with ``.csv``, the stem is used as a file prefix:
    ``<stem>_nodes.csv`` and ``<stem>_edges.csv``.

    Otherwise, base_path is treated as a directory (created if needed):
    ``<base_path>/nodes.csv`` and ``<base_path>/edges.csv``.
    """
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
    store: Optional[dict] = None,
    store_module=None,
) -> tuple[list[TraceStep], Optional[str], int]:
    steps: list[TraceStep] = []
    err: Optional[str] = None
    nodes_found = 0
    errors_found = 0
    try:
        if direction == "forward":
            gen = trace_forward(provider, start, max_depth=max_depth,
                                cached_nodes=cached_nodes)
        else:
            gen = trace_backward(provider, start, max_depth=max_depth,
                                  cached_nodes=cached_nodes,
                                  cached_inputs=cached_inputs)
        async for step in gen:
            steps.append(step)
            if step.utxo:
                nodes_found += 1
                # Incremental cache: save to in-memory store immediately
                if store is not None and store_module is not None:
                    store_module.add_node_to_store(step.utxo, store)
                    if step.parent_out_ref:
                        # Store UTXO input direction: parent[output] → [child(input)]
                        store_module.add_input_to_store(
                            step.parent_out_ref.node_id(),  # output (closer to start)
                            step.out_ref.node_id(),         # input (deeper chain)
                            store,
                        )
            if step.error:
                errors_found += 1
            # Periodic flush: save to disk at step 1 (immediate) + every 5 steps
            if store is not None and store_module is not None:
                n = len(steps)
                if n == 1 or (n > 0 and n % 5 == 0):
                    store_module.save_store_file(store)
            progress.update(
                task_id,
                advance=1,
                description=(
                    f"[cyan]{direction}[/cyan] "
                    f"depth={step.depth} nodes={nodes_found} errors={errors_found}"
                    f" [dim]{getattr(provider, 'current_provider', '') or ''}[/dim]"
                ),
            )
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return steps, err, errors_found


async def _do_trace(
    provider: Provider,
    start: OutRef,
    direction: str,
    max_depth: int,
    skip_store: bool = False,
) -> TraceResult:
    # Load store once (1 file read) — keep in memory for incremental updates
    from . import cache as _cache_mod

    if not skip_store and _cache_mod.STORE_FILE.exists():
        store = _cache_mod.load_store_file()
        _cached_nodes, _cached_inputs = _cache_mod._store_to_models(store)
    else:
        store = None
        _cached_nodes, _cached_inputs = {}, {}

    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed} steps"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        all_steps: list[TraceStep] = []
        err: Optional[str] = None

        if direction in ("backward", "both"):
            pname = getattr(provider, "current_provider", "") or ""
            task = progress.add_task(
                f"[cyan]backward[/cyan] tracing... [dim]{pname}[/dim]", total=None
            )
            bsteps, berr, berrors = await _run_trace(
                provider, start, "backward", max_depth, progress, task,
                cached_nodes=_cached_nodes,
                cached_inputs=_cached_inputs,
                store=store,
                store_module=_cache_mod,
            )
            all_steps.extend(bsteps)
            err = berr
            # Flush store to disk after backward trace completes
            if store is not None:
                _cache_mod.save_store_file(store)

        if direction in ("forward", "both"):
            pname = getattr(provider, "current_provider", "") or ""
            task = progress.add_task(
                f"[cyan]forward[/cyan] tracing... [dim]{pname}[/dim]", total=None
            )
            fsteps, ferr, _ferrors = await _run_trace(
                provider, start, "forward", max_depth, progress, task,
                cached_nodes=_cached_nodes,
                store=store,
                store_module=_cache_mod,
            )
            all_steps.extend(fsteps)
            if ferr:
                err = (err + "; " if err else "") + ferr
            # Flush store after forward trace too
            if store is not None:
                _cache_mod.save_store_file(store)

    primary_direction = "backward" if direction != "forward" else "forward"
    nodes, edges, traced_path = build_graph_from_steps(all_steps, primary_direction)

    # Set provider name for display
    pname = getattr(provider, "current_provider", "") or ""
    provider_name = (
        pname
        if pname
        else getattr(provider, "provider_type", "fallback")
    )

    # If 'both', overlay forward edges
    if direction == "both":
        # rebuild forward separately to also include forward edges
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


# --------------- CLI --------------- #


_MAIN_HELP = """\
Cardano UTXO chain tracer — trace funds through Cardano blockchain.

\\b
PROVIDERS
  blockfrost   Blockfrost API (mainnet/testnet). Backward tracing only.
               Auth: project_id (default) | bearer | dmtr-api-key (Demeter.run)
  koios        Koios public API. Backward tracing only.
               Auth: optional API key
  maestro      Maestro API. Backward tracing only.
               Auth: x-api-key header (required)
  kupmios      Kupo + Ogmios (local node). Supports BOTH backward AND forward tracing.
               Auth: separate optional keys per service

\\b
FALLBACK
  By default the tool uses fallback across multiple providers for reliability.
  --fallback (on)   tries primary, then utxorpc -> blockfrost -> koios -> maestro
  --no-fallback     single provider only (original behavior)

\\b
CONFIG PRIORITY  CLI flags > shell env > .env file (auto-discovered) > config.json

\\b
EXAMPLES
  \\b
  # Single provider
  utxo-tracer trace abc123...#0 --provider blockfrost --api-key mainnet_XXX
  \\b
  # Default: auto-fallback across all configured providers
  utxo-tracer trace abc123...#0
  \\b
  # Forward trace (requires kupmios)
  utxo-tracer trace abc123...#0 --provider kupmios
    --kupo-url http://localhost:1442 --ogmios-url http://localhost:1337
    --direction forward --max-depth 10
  \\b
  # Demeter.run
  BLOCKFROST_AUTH_TYPE=dmtr-api-key BLOCKFROST_API_KEY=dmtr_XXX
  BLOCKFROST_ENDPOINT_URL=https://cardano-mainnet.blockfrost.io/api/v0
  utxo-tracer trace abc123...#0 --provider blockfrost
  \b
  # UTxORPC (high-throughput, self-host or Demeter.run)
  utxo-tracer trace abc123...#0 --provider utxorpc --api-key YOUR_KEY
  utxo-tracer trace abc123...#0 --provider utxorpc --base-url https://mainnet.utxorpc.com
"""


@click.group(help=_MAIN_HELP)
@click.version_option(package_name="utxo-tracer")
def main() -> None:
    pass


@main.command(
    "trace",
    help="""\
Trace a UTXO backwards/forwards through the chain.

|\\\\b
UTXO format: <tx_hash>#<output_index>
  e.g. abc123def456...#0

\\\\b
PROVIDER OPTIONS
  --provider       blockfrost | koios | maestro | kupmios | utxorpc
  --api-key        API key (blockfrost project_id / koios / maestro / utxorpc)
  --auth-type      Blockfrost only: project_id (default) | bearer | dmtr-api-key
  --endpoint-url   Blockfrost/Demeter: upstream endpoint override
  --base-url       Override provider base URL directly
  --kupo-url       Kupmios: Kupo base URL  (e.g. http://localhost:1442)
  --ogmios-url     Kupmios: Ogmios base URL (e.g. http://localhost:1337)
  --use-proxy      Route through local proxy at --proxy-url [default: off]
  --proxy-url      Proxy base URL [default: http://localhost:3001]

\\\\b
FALLBACK
  By default the tool uses fallback across multiple providers for reliability.
  --fallback (on)   tries primary, then utxorpc -> blockfrost -> koios -> maestro
  --no-fallback     single provider only (original behavior)

\\b
TRACE OPTIONS
  --direction      backward | forward | both  [default: backward]
                   forward requires --provider kupmios
  --max-depth      Max recursion depth         [default: 5]

\\b
OUTPUT OPTIONS
  --output         table | json | csv          [default: table]
  --export-json    Save full trace result to JSON file
  --export-csv     Save nodes + edges as CSV files (prefix path)
  --depth-report   Show node count per depth level

\b
CEX DETECTION
  --cex-file       JSON file with exchange address registry
""",
)
@click.argument("utxo", type=str)
@click.option(
    "--provider",
    type=click.Choice(["blockfrost", "koios", "maestro", "kupmios", "utxorpc"]),
    default=None,
)
@click.option("--api-key", type=str, default=None)
@click.option("--base-url", type=str, default=None)
@click.option(
    "--auth-type",
    type=click.Choice(["project_id", "bearer", "dmtr-api-key"]),
    default=None,
)
@click.option("--endpoint-url", type=str, default=None)
@click.option("--kupo-url", type=str, default=None)
@click.option("--ogmios-url", type=str, default=None)
@click.option(
    "--direction",
    type=click.Choice(["backward", "forward", "both"]),
    default=None,
)
@click.option("--max-depth", type=int, default=None)
@click.option(
    "--output",
    type=click.Choice(["table", "json", "csv"]),
    default="table",
)
@click.option("--export-json", type=click.Path(dir_okay=False), default=None)
@click.option("--export-csv", type=click.Path(), default=None)
@click.option("--fallback/--no-fallback", default=True)
@click.option("--cex-file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--depth-report", is_flag=True, default=False)
@click.option("--dash/--no-dash", default=True, hidden=True)
@click.option("--use-proxy/--no-proxy", default=False)
@click.option("--proxy-url", type=str, default="http://localhost:3001")
@click.option("--no-cache", is_flag=True, default=False,
              help="Skip local cache; always query providers.")
def trace_cmd(
    utxo: str,
    provider: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    auth_type: Optional[str],
    endpoint_url: Optional[str],
    kupo_url: Optional[str],
    ogmios_url: Optional[str],
    direction: Optional[str],
    max_depth: Optional[int],
    output: str,
    export_json: Optional[str],
    export_csv: Optional[str],
    dash: bool,
    fallback: bool,
    cex_file: Optional[str],
    depth_report: bool,
    use_proxy: bool,
    proxy_url: str,
    no_cache: bool,
) -> None:
    cfg = load_config()
    defaults = cfg.get("defaults", {}) or {}
    direction = direction or defaults.get("direction") or "backward"
    max_depth = (
        max_depth if max_depth is not None else int(defaults.get("max_depth") or 5)
    )


    # Note: env vars inherited at process start (may include shell overrides of .env)
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

    # ── check cache ────────────────────────────────────────────────
    if not no_cache:
        cached_result = cache_mod.load_trace(start, direction, max_depth)
        if cached_result is not None:
            # Check if cached result had errors — if so, re-trace
            if getattr(cached_result, "errors_count", 0) > 0:
                console.print("[yellow]Cached trace had errors — re-tracing missing UTXOs[/yellow]")
            else:
                console.print("[green]Loaded from local cache[/green]")
                from .graph.dash_app import start_server
                ck = cache_mod._cache_key(start, direction, max_depth)
                start_server(cached_result, start_out_ref=start, cache_key=ck)
                return
    # ────────────────────────────────────────────────────────────────

    provider_name = _resolve_provider_name(provider, cfg)

    if direction in ("forward", "both") and provider_name != "kupmios":
        _fatal(f"Forward tracing requires --provider kupmios (got '{provider_name}').")

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

    async def _runner() -> TraceResult:
        async with prov as p:
            return await _do_trace(p, start, direction, max_depth, skip_store=no_cache)

    try:
        result = asyncio.run(_runner())
        # Save to cache for future per-step acceleration (skip when --no-cache)
        if not no_cache:
            cache_mod.save_trace(result, start, direction, max_depth,
                                 provider=provider_name)
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

    # Always start Dash Cytoscape after trace
    from .graph.dash_app import start_server

    if dash:
        console.print("[green]Starting Dash Cytoscape graph...[/green]")
        Timer(1.5, _open_browser).start()
        ck = cache_mod._cache_key(start, direction, max_depth) if not no_cache else ""
        start_server(result, start_out_ref=start, cache_key=ck)
    else:
        console.print("[green]Trace complete. Use --dash to open interactive graph.[/green]")

    if result.error:
        err_console.print(f"[red]Trace ended with error:[/red] {result.error}")
        sys.exit(2)


@main.group("config", help="Manage stored configuration.")
def config_group() -> None:
    pass


@config_group.command("set")
@click.option(
    "--provider",
    type=click.Choice(["blockfrost", "koios", "maestro", "kupmios", "utxorpc"]),
    required=True,
)
@click.option("--api-key", type=str, default=None)
@click.option("--base-url", type=str, default=None)
@click.option(
    "--auth-type",
    type=click.Choice(["project_id", "bearer", "dmtr-api-key"]),
    default=None,
)
@click.option("--endpoint-url", type=str, default=None)
@click.option("--kupo-url", type=str, default=None)
@click.option("--ogmios-url", type=str, default=None)
@click.option("--kupo-api-key", type=str, default=None)
@click.option("--ogmios-api-key", type=str, default=None)
@click.option("--make-default/--no-default", default=True)
def config_set(
    provider: str,
    api_key: Optional[str],
    base_url: Optional[str],
    auth_type: Optional[str],
    endpoint_url: Optional[str],
    kupo_url: Optional[str],
    ogmios_url: Optional[str],
    kupo_api_key: Optional[str],
    ogmios_api_key: Optional[str],
    make_default: bool,
) -> None:
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


@main.command("health", help="Check provider connectivity.")
@click.option(
    "--provider",
    type=click.Choice(["blockfrost", "koios", "maestro", "kupmios", "utxorpc"]),
    default=None,
)
@click.option("--api-key", type=str, default=None)
@click.option("--base-url", type=str, default=None)
@click.option(
    "--auth-type",
    type=click.Choice(["project_id", "bearer", "dmtr-api-key"]),
    default=None,
)
@click.option("--endpoint-url", type=str, default=None)
@click.option("--kupo-url", type=str, default=None)
@click.option("--ogmios-url", type=str, default=None)
@click.option("--fallback/--no-fallback", default=True)
@click.option("--use-proxy/--no-proxy", default=False)
@click.option("--proxy-url", type=str, default="http://localhost:3001")
def health_cmd(
    provider: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    auth_type: Optional[str],
    endpoint_url: Optional[str],
    kupo_url: Optional[str],
    ogmios_url: Optional[str],
    fallback: bool,
    use_proxy: bool,
    proxy_url: str,
) -> None:
    cfg = load_config()
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

    async def _check() -> bool:
        async with prov as p:
            return await p.health_check()

    ok = asyncio.run(_check())
    if ok:
        console.print("[green]OK[/green] provider chain is reachable.")
    else:
        err_console.print("[red]FAIL[/red] provider chain is not reachable.")
        sys.exit(1)


@main.command("assets", help="Show asset breakdown for a single UTXO.")
@click.argument("utxo", type=str)
@click.option(
    "--provider",
    type=click.Choice(["blockfrost", "koios", "maestro", "kupmios", "utxorpc"]),
    default=None,
)
@click.option("--api-key", type=str, default=None)
@click.option("--base-url", type=str, default=None)
@click.option(
    "--auth-type",
    type=click.Choice(["project_id", "bearer", "dmtr-api-key"]),
    default=None,
)
@click.option("--endpoint-url", type=str, default=None)
@click.option("--kupo-url", type=str, default=None)
@click.option("--ogmios-url", type=str, default=None)
@click.option("--use-proxy/--no-proxy", default=False)
@click.option("--proxy-url", type=str, default="http://localhost:3001")
def assets_cmd(
    utxo: str,
    provider: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    auth_type: Optional[str],
    endpoint_url: Optional[str],
    kupo_url: Optional[str],
    ogmios_url: Optional[str],
    use_proxy: bool,
    proxy_url: str,
) -> None:
    cfg = load_config()
    try:
        out_ref = parse_out_ref(utxo)
    except ValueError as e:
        _fatal(str(e))
    provider_name = _resolve_provider_name(provider, cfg)
    provider_cfg = (cfg.get("providers") or {}).get(provider_name, {}) or {}
    overrides = {
        "api_key": api_key,
        "base_url": base_url,
        "auth_type": auth_type,
        "endpoint_url": endpoint_url,
        "kupo_url": kupo_url,
        "ogmios_url": ogmios_url,
    }
    overrides = {k: v for k, v in overrides.items() if v is not None}
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

    panel = Panel.fit(
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
    console.print(panel)

    table = Table(title="Assets", header_style="bold magenta")
    table.add_column("Policy ID", overflow="fold")
    table.add_column("Asset Name")
    table.add_column("Unit", overflow="fold")
    table.add_column("Quantity", justify="right")
    for a in node.assets:
        table.add_row(
            a.policy_id or "(lovelace)",
            a.asset_name or "-",
            a.unit,
            f"{a.quantity:,}",
        )
    console.print(table)


# ── cache commands ──────────────────────────────────────────────

@main.group("cache", help="Manage local trace cache.")
def cache_group() -> None:
    pass


@cache_group.command("list", help="List cached traces.")
def cache_list() -> None:
    from rich.table import Table
    entries = cache_mod.list_traces()
    if not entries:
        console = Console()
        console.print("[yellow]No cached traces.[/yellow]")
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
            "✓" if e.get("exists") else "✗",
        )
    Console().print(table)


@cache_group.command("clear", help="Remove all cached traces.")
def cache_clear() -> None:
    count = cache_mod.clear_cache()
    Console().print(f"[green]Cleared {count} cached traces.[/green]")


@cache_group.command("info", help="Show cache storage info.")
def cache_info() -> None:
    from rich.table import Table
    from pathlib import Path
    cache_dir = cache_mod.CACHE_DIR
    size = 0
    if cache_dir.exists():
        for f in cache_dir.rglob("*"):
            if f.is_file():
                size += f.stat().st_size
    summary = cache_mod.store_summary()
    store_ok = cache_mod.STORE_FILE.exists()
    table = Table(title="Cache Info")
    table.add_column("Property")
    table.add_column("Value")
    table.add_row("Cache dir", str(cache_dir))
    table.add_row("Trace files", str(len(list(cache_dir.glob("traces/*.json")))))
    table.add_row("Store nodes", str(summary["nodes"]) + (" ✓" if store_ok else " (no store)"))
    table.add_row("Store tx inputs", str(summary["inputs"]))
    table.add_row("Store transactions", str(summary["transactions"]))
    table.add_row("Size", f"{size/1024:.1f} KB")
    Console().print(table)


# ── open from cache ─────────────────────────────────────────────

@main.command("open", help="Open cached trace visualization.")
@click.argument("cache_key")
def open_cmd(cache_key: str) -> None:
    """Load a cached trace by its cache key and open the Dash visualizer."""
    from .graph.dash_app import start_server
    trace_file = cache_mod.TRACES_DIR / f"{cache_key}.json"
    if not trace_file.exists():
        Console().print(f"[red]Cache key '{cache_key}' not found.[/red]")
        Console().print("Run [yellow]utxo-tracer cache list[/yellow] to see available keys.")
        return
    import json
    data = json.loads(trace_file.read_text())
    meta = data.get("metadata", {})
    start_str = meta.get("start", "#0")
    if "#" in start_str:
        tx_hash, idx_s = start_str.rsplit("#", 1)
        start_ref = OutRef(tx_hash, int(idx_s))
    else:
        start_ref = OutRef(start_str, 0)
    result = cache_mod.load_trace(
        start_ref, meta.get("direction", "backward"), meta.get("max_depth", 0)
    )
    if result is None:
        Console().print("[red]Failed to parse cached trace data.[/red]")
        return
    start_server(result, start_out_ref=start_ref, cache_key=cache_key)


if __name__ == "__main__":
    main()
