"""Tests for apply_cex_filter — CEX-related post-filter for address traces.

These tests cover the algorithm in pure isolation: build an AddressTraceResult
from scratch, run the filter, verify the kept set matches expectations.
No provider / network / cache interaction — see test_address_interactions.py
for end-to-end trace tests.
"""

from __future__ import annotations

import pytest

from utxo_tracer.cex.registry import register_cex_address
from utxo_tracer.models import (
    AddressInteractionEdge,
    AddressInteractionNode,
    AddressTraceResult,
    CexInfo,
)
from utxo_tracer.tracing.address_interactions import apply_cex_filter


# ── Helpers ──────────────────────────────────────────────────────────────


def _node(
    address: str,
    *,
    is_cex: bool = False,
    cex_name: str = "",
    is_target: bool = False,
    depth: int = 0,
) -> AddressInteractionNode:
    """Build a minimal AddressInteractionNode for tests."""
    return AddressInteractionNode(
        address=address,
        is_cex=is_cex,
        cex_name=cex_name,
        is_target=is_target,
        depth=depth,
    )


def _edge(source: str, target: str) -> AddressInteractionEdge:
    """Build a minimal AddressInteractionEdge for tests."""
    return AddressInteractionEdge(
        source=source,
        target=target,
        tx_hashes=["tx_dummy"],
        interaction_count=1,
        direction_relative_to_target="unknown",
        source_depth=1,
    )


def _result(
    target: str,
    addresses: list[AddressInteractionNode],
    edges: list[AddressInteractionEdge],
    *,
    total_transactions: int = 10,
    error: str | None = None,
    max_depth: int = 1,
) -> AddressTraceResult:
    """Build a minimal AddressTraceResult for tests."""
    return AddressTraceResult(
        target_address=target,
        addresses=addresses,
        edges=edges,
        total_transactions=total_transactions,
        error=error,
        provider_name="test",
        max_depth=max_depth,
    )


# Unique addresses for the test suite — avoid accidental cross-test pollution
TARGET = "addr1target00000000000000000000000000000000000000000000000"
A1 = "addr1counterpartyA000000000000000000000000000000000000000000"
A2 = "addr1counterpartyB000000000000000000000000000000000000000000"
A3 = "addr1orphan00000000000000000000000000000000000000000000000"
BINANCE = "addr1binance_test_address_cex_filter_apply_cex_filter_x"
KRAKEN = "addr1kraken_test_address_cex_filter_apply_cex_filter_xx"
INTERMEDIATE = "addr1intermediate000000000000000000000000000000000000"


# ── Tests ────────────────────────────────────────────────────────────────


class TestApplyCexFilterEmpty:
    """Edge case: empty or trivial inputs."""

    def test_empty_result_returns_unchanged(self):
        """No addresses / no edges → filter is a no-op."""
        result = _result(TARGET, [], [])
        out = apply_cex_filter(result)
        assert out is result or (out.addresses == [] and out.edges == [])

    def test_keeps_target_when_no_cex_in_result(self):
        """No CEX in result → only target is kept."""
        addrs = [_node(TARGET, is_target=True), _node(A1), _node(A2)]
        edges = [_edge(TARGET, A1), _edge(A1, A2)]
        result = _result(TARGET, addrs, edges)

        out = apply_cex_filter(result)

        kept_addrs = {n.address for n in out.addresses}
        assert kept_addrs == {TARGET}, f"Expected only target, got {kept_addrs}"
        assert out.edges == [], "All edges should be dropped (no CEX path)"


class TestApplyCexFilterWithCex:
    """Filter behavior when a CEX is present in the trace graph."""

    def setup_method(self):
        """Register a fake CEX before each test in this class."""
        # KNOWN_CEX (Binance hot wallet) is already loaded at registry import
        # time. To avoid mutating the shared registry, we use the existing
        # Binance seed and the Kraken address below for multi-CEX coverage.
        register_cex_address(
            KRAKEN, CexInfo(name="Kraken", type="exchange", confidence="high")
        )

    def test_keeps_target_and_direct_cex(self):
        """Target ↔ CEX edge → both endpoints kept, no intermediates."""
        addrs = [_node(TARGET, is_target=True), _node(BINANCE, is_cex=True, cex_name="Binance")]
        edges = [_edge(TARGET, BINANCE)]
        result = _result(TARGET, addrs, edges)

        out = apply_cex_filter(result)

        kept = {n.address for n in out.addresses}
        assert kept == {TARGET, BINANCE}
        assert len(out.edges) == 1
        assert {out.edges[0].source, out.edges[0].target} == {TARGET, BINANCE}

    def test_keeps_intermediate_path_to_cex(self):
        """Target ↔ A1 ↔ A2(CEX) → all 3 kept, both edges kept."""
        addrs = [
            _node(TARGET, is_target=True),
            _node(A1),
            _node(A2, is_cex=True, cex_name="TestCEX"),
        ]
        edges = [_edge(TARGET, A1), _edge(A1, A2)]
        result = _result(TARGET, addrs, edges)

        out = apply_cex_filter(result)

        kept = {n.address for n in out.addresses}
        assert kept == {TARGET, A1, A2}
        assert len(out.edges) == 2

    def test_drops_orphan_component(self):
        """A1 ↔ A3 cluster (no CEX) is dropped entirely; CEX cluster kept."""
        addrs = [
            _node(TARGET, is_target=True),
            _node(BINANCE, is_cex=True, cex_name="Binance"),
            _node(A1),
            _node(A3),
        ]
        edges = [
            _edge(TARGET, BINANCE),  # target ↔ CEX
            _edge(A1, A3),  # orphan: no path to CEX or target
        ]
        result = _result(TARGET, addrs, edges)

        out = apply_cex_filter(result)

        kept = {n.address for n in out.addresses}
        # Target ↔ CEX cluster is kept; orphan A1 ↔ A3 is dropped.
        assert kept == {TARGET, BINANCE}, f"Got {kept}"
        assert len(out.edges) == 1
        assert {out.edges[0].source, out.edges[0].target} == {TARGET, BINANCE}

    def test_multiple_cex_union(self):
        """Two CEXs reachable from target → union of paths kept."""
        addrs = [
            _node(TARGET, is_target=True),
            _node(A1),
            _node(BINANCE, is_cex=True, cex_name="Binance"),
            _node(A2),
            _node(KRAKEN, is_cex=True, cex_name="Kraken"),
        ]
        edges = [
            _edge(TARGET, A1),
            _edge(A1, BINANCE),
            _edge(TARGET, A2),
            _edge(A2, KRAKEN),
        ]
        result = _result(TARGET, addrs, edges)

        out = apply_cex_filter(result)

        kept = {n.address for n in out.addresses}
        assert kept == {TARGET, A1, BINANCE, A2, KRAKEN}
        assert len(out.edges) == 4

    def test_target_is_a_cex(self):
        """Target itself is a CEX → all reachable nodes kept (BFS from target)."""
        addrs = [
            _node(TARGET, is_target=True, is_cex=True, cex_name="Binance"),
            _node(A1),
            _node(A2),
            _node(A3),  # not connected to anything reachable
        ]
        edges = [
            _edge(TARGET, A1),
            _edge(A1, A2),
        ]
        result = _result(TARGET, addrs, edges)

        out = apply_cex_filter(result)

        kept = {n.address for n in out.addresses}
        # Target is CEX, so BFS expands to A1 and A2. A3 has no edges.
        assert kept == {TARGET, A1, A2}, f"Got {kept}"
        assert len(out.edges) == 2

    def test_metadata_preserved(self):
        """total_transactions, error, max_depth are preserved on filtered result."""
        addrs = [
            _node(TARGET, is_target=True),
            _node(BINANCE, is_cex=True, cex_name="Binance"),
        ]
        edges = [_edge(TARGET, BINANCE)]
        result = _result(
            TARGET,
            addrs,
            edges,
            total_transactions=42,
            error="some warning",
            max_depth=3,
        )

        out = apply_cex_filter(result)

        assert out.total_transactions == 42
        assert out.error == "some warning"
        assert out.max_depth == 3
        assert out.target_address == TARGET
        assert out.provider_name == "test"


class TestApplyCexFilterLongPath:
    """The "ancestors of CEX up to target" semantic specifically.

    The naive BFS-reachable-from-CEX semantic would keep the entire graph
    once a CEX is found (because target is the central hub). The point
    of the ancestors semantic is to keep ONLY the CEX-touching branches
    and drop unrelated ones — including intermediate nodes on the CEX
    branch.
    """

    def test_keeps_full_path_to_cex_drops_other_branches(self):
        """Longer layout: TARGET has multiple direct counterparties, only
        one chain leads to a CEX. The non-CEX branches must be dropped,
        the CEX branch (and its intermediates) must be kept.

        Layout::

            TARGET ─── A1 (no CEX)         <- dropped
            TARGET ─── X1 ─── X2 ─── BIN   <- kept (X1, X2, BIN are ancestors of BIN up to TARGET)
            TARGET ─── A5 (no CEX)         <- dropped
        """
        A1 = "addr1qx_unrelated_A1_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        X1 = "addr1qx_intermediate_X1_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        X2 = "addr1qx_intermediate_X2_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        BIN = "addr1vx7j284mqe59w2mka36gf5xq0hvu8ms2989553fk5qh3prcapfpj3"
        A5 = "addr1qx_unrelated_A5_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

        nodes = [
            _node(TARGET, is_target=True),
            _node(A1),
            _node(X1),
            _node(X2),
            _node(BIN, is_cex=True, cex_name="Binance"),
            _node(A5),
        ]
        edges = [
            _edge(TARGET, A1),
            _edge(TARGET, X1),
            _edge(X1, X2),
            _edge(X2, BIN),
            _edge(TARGET, A5),
        ]
        result = _result(TARGET, nodes, edges, max_depth=3)

        out = apply_cex_filter(result)

        kept = {n.address for n in out.addresses}
        # TARGET + X1 + X2 + BIN kept; A1, A5 dropped.
        assert kept == {TARGET, X1, X2, BIN}, f"Got {kept}"

        # Edges: only the CEX branch.
        kept_edge_pairs = {(e.source, e.target) for e in out.edges}
        # Original edges are undirected in our graph; check both directions.
        expected = {
            (TARGET, X1), (X1, X2), (X2, BIN),
        }
        assert kept_edge_pairs == expected, f"Got edges {kept_edge_pairs}"

    def test_reduces_large_graph_around_cex_branch(self):
        """Big graph (200 nodes), only one chain touches a CEX.

        Demonstrates the real-world value of the filter: scanning a
        large trace for CEX exposure should return a small focused
        subgraph, not the entire trace.
        """
        # 99 unrelated wallet addresses, all connected only to TARGET
        unrelated = [f"addr1qx_wallet_{i:03d}_xxxxxxxxxxxxxxxxxxxxxxxxxxxx" for i in range(99)]
        # CEX branch: TARGET → Y1 → Y2 → Y3 → KRAKEN
        Y1 = "addr1qx_intermediate_Y1_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        Y2 = "addr1qx_intermediate_Y2_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        Y3 = "addr1qx_intermediate_Y3_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

        nodes = [_node(TARGET, is_target=True)]
        for u in unrelated:
            nodes.append(_node(u))
        nodes.extend([
            _node(Y1), _node(Y2), _node(Y3),
            _node(KRAKEN, is_cex=True, cex_name="Kraken"),
        ])
        edges = [_edge(TARGET, u) for u in unrelated]
        edges.extend([
            _edge(TARGET, Y1),
            _edge(Y1, Y2),
            _edge(Y2, Y3),
            _edge(Y3, KRAKEN),
        ])
        result = _result(TARGET, nodes, edges, total_transactions=5000, max_depth=4)

        out = apply_cex_filter(result)

        kept = {n.address for n in out.addresses}
        # 99 unrelated + 1 KRAKEN branch + 1 TARGET = 5 kept out of 104
        assert kept == {TARGET, Y1, Y2, Y3, KRAKEN}, f"Got {len(kept)} kept"
        assert len(out.addresses) == 5
        assert len(out.edges) == 4  # TARGET-Y1, Y1-Y2, Y2-Y3, Y3-KRAKEN


class TestApplyCexFilterNodeFlags:
    """Verify node flags (is_target, is_cex, cex_name) are preserved."""

    def test_node_flags_survive_filter(self):
        """is_target, is_cex, cex_name, depth pass through unchanged."""
        addrs = [
            _node(TARGET, is_target=True, depth=0),
            _node(INTERMEDIATE, depth=1),
            _node(BINANCE, is_cex=True, cex_name="Binance", depth=2),
        ]
        edges = [_edge(TARGET, INTERMEDIATE), _edge(INTERMEDIATE, BINANCE)]
        result = _result(TARGET, addrs, edges)

        out = apply_cex_filter(result)

        by_addr = {n.address: n for n in out.addresses}
        assert by_addr[TARGET].is_target is True
        assert by_addr[TARGET].is_cex is False
        assert by_addr[INTERMEDIATE].is_target is False
        assert by_addr[INTERMEDIATE].is_cex is False
        assert by_addr[BINANCE].is_cex is True
        assert by_addr[BINANCE].cex_name == "Binance"


class TestApplyCexFilterRegistry:
    """Test that registry re-check catches is_cex=False but registered nodes."""

    def test_identify_cex_recovers_misflagged_node(self):
        """A node with is_cex=False but now in the registry must be picked up.

        This covers the case where a trace ran before a CEX was registered
        (so is_cex=False) but the user then registered it and re-runs the
        filter. The filter should re-check via identify_cex() and treat it
        as a CEX seed.

        Note: with the "ancestors of CEX up to target" semantic, only the
        path TARGET → A1 is kept (A1 is itself a CEX, so A2 is a descendant
        of A1, not an ancestor of a CEX). The point of this test is that
        the registry re-check correctly identifies A1 as a CEX — the kept
        set is then TARGET + A1 (the path TO A1).
        """
        # Node is marked is_cex=False, but we register the address BEFORE
        # running the filter, so identify_cex() will return the info.
        register_cex_address(
            A1, CexInfo(name="NewlyRegistered", type="exchange", confidence="high")
        )

        addrs = [
            _node(TARGET, is_target=True),
            _node(A1, is_cex=False),  # stale flag
            _node(A2),
        ]
        edges = [_edge(TARGET, A1), _edge(A1, A2)]
        result = _result(TARGET, addrs, edges)

        out = apply_cex_filter(result)

        # A1 is in registry → acts as CEX seed → TARGET + A1 kept.
        # A2 is a descendant of A1 (not an ancestor of a CEX) → dropped.
        kept = {n.address for n in out.addresses}
        assert kept == {TARGET, A1}, f"Expected {{TARGET, A1}}, got {kept}"
        # Verify the CEX was correctly identified (registry re-check works)
        assert out.addresses[0].is_target is True or any(
            n.address == A1 for n in out.addresses
        )
