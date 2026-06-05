"""Tests for new address-trace features:

- ``--direction`` (backward / forward / both) flow filtering
- "Binance User" (cex_user) labeling of direct CEX counterparties
- address tx-list caching (skip provider pagination on repeat)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from utxo_tracer.models import Asset, CexInfo, OutRef, UTxONode
import utxo_tracer.tracing.address_interactions as ai_mod
from utxo_tracer.tracing.address_interactions import trace_address_interactions

T = "addr1q" + "t" * 100  # target
B = "addr1q" + "b" * 100  # recipient (target → B)
C = "addr1q" + "c" * 100  # sender    (C → target)
W = "addr1q" + "w" * 100  # wallet that touches the CEX
X = "addr1q" + "x" * 100  # CEX address


def _utxo(tx_hash: str, idx: int, address: str, ada: float) -> UTxONode:
    out_ref = OutRef(tx_hash, idx)
    return UTxONode(
        id=out_ref.node_id(),
        out_ref=out_ref,
        address=address,
        assets=[Asset(policy_id="", asset_name="", quantity=int(ada * 1_000_000))],
    )


def _tx(tx_hash: str, in_addr: str, out_addr: str, ada: float = 10.0) -> dict:
    return {
        "input_utxos": {f"prev_{tx_hash}:0": _utxo(f"prev_{tx_hash}", 0, in_addr, ada)},
        "outputs": [_utxo(tx_hash, 0, out_addr, ada)],
    }


def _make_provider(addr_txs: dict[str, list[str]], tx_map: dict[str, dict]):
    mock = AsyncMock()
    mock.current_provider = "blockfrost"

    async def _addr(addr):
        return addr_txs.get(addr, [])

    async def _batch(hashes):
        return [tx_map[h] for h in hashes]

    mock.get_address_transactions.side_effect = _addr
    mock.get_transactions_utxos.side_effect = _batch
    return mock


@pytest.fixture(autouse=True)
def _no_sqlite(monkeypatch):
    monkeypatch.setattr(ai_mod, "save_transaction", lambda *a, **k: None)
    monkeypatch.setattr(ai_mod, "save_utxos_to_store", lambda *a, **k: None, raising=False)


# ── direction filtering ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_direction_forward_keeps_only_downstream():
    """forward = value LEAVING target → only the recipient (B), not sender C."""
    addr_txs = {T: ["tx_TB", "tx_CT"], B: [], C: []}
    tx_map = {"tx_TB": _tx("tx_TB", T, B), "tx_CT": _tx("tx_CT", C, T)}
    prov = _make_provider(addr_txs, tx_map)

    res = await trace_address_interactions(prov, T, max_depth=2, direction="forward")
    addrs = {n.address for n in res.addresses}
    assert B in addrs, "forward must keep downstream recipient B"
    assert C not in addrs, "forward must NOT keep upstream sender C"


@pytest.mark.asyncio
async def test_direction_backward_keeps_only_upstream():
    """backward = value ENTERING target → only the sender (C), not recipient B."""
    addr_txs = {T: ["tx_TB", "tx_CT"], B: [], C: []}
    tx_map = {"tx_TB": _tx("tx_TB", T, B), "tx_CT": _tx("tx_CT", C, T)}
    prov = _make_provider(addr_txs, tx_map)

    res = await trace_address_interactions(prov, T, max_depth=2, direction="backward")
    addrs = {n.address for n in res.addresses}
    assert C in addrs, "backward must keep upstream sender C"
    assert B not in addrs, "backward must NOT keep downstream recipient B"


@pytest.mark.asyncio
async def test_direction_both_keeps_all():
    addr_txs = {T: ["tx_TB", "tx_CT"], B: [], C: []}
    tx_map = {"tx_TB": _tx("tx_TB", T, B), "tx_CT": _tx("tx_CT", C, T)}
    prov = _make_provider(addr_txs, tx_map)

    res = await trace_address_interactions(prov, T, max_depth=2, direction="both")
    addrs = {n.address for n in res.addresses}
    assert {B, C}.issubset(addrs)
    assert res.direction == "both"


# ── cex_user ("Binance User") labeling ─────────────────────────────────────


@pytest.mark.asyncio
async def test_cex_user_label(monkeypatch):
    """A non-CEX wallet that directly transacts with a registered CEX is tagged."""
    monkeypatch.setattr(
        ai_mod, "identify_cex",
        lambda a: CexInfo(name="Binance") if a == X else None,
    )
    # T → W (W is a counterparty), W → X (X is the CEX)
    addr_txs = {T: ["tx_TW"], W: ["tx_WX"], X: []}
    tx_map = {"tx_TW": _tx("tx_TW", T, W), "tx_WX": _tx("tx_WX", W, X)}
    prov = _make_provider(addr_txs, tx_map)

    res = await trace_address_interactions(prov, T, max_depth=2, direction="both")
    by_addr = {n.address: n for n in res.addresses}

    assert by_addr[X].is_cex, "X must be flagged as CEX"
    assert by_addr[W].cex_user == "Binance", "W transacts with CEX → 'Binance User'"
    assert not by_addr[W].is_cex
    # a pure non-CEX-adjacent node is not labeled
    assert by_addr[T].cex_user == ""  # target only touches W, not the CEX


# ── address tx-list caching ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_addr_txs_cache_skips_provider_pagination():
    """When the tx-list is cached, get_address_transactions is NOT called."""
    addr_txs = {T: ["tx_TB"], B: []}
    tx_map = {"tx_TB": _tx("tx_TB", T, B)}
    prov = _make_provider(addr_txs, tx_map)

    saved: dict[str, list[str]] = {}

    def _get(addr):
        return set(saved.get(addr, []))

    def _save(addr, hashes):
        saved[addr] = list(hashes)

    # pre-seed the cache for the target so its pagination is skipped
    saved[T] = ["tx_TB"]

    res = await trace_address_interactions(
        prov, T, max_depth=1,
        addr_txs_cache_get=_get, addr_txs_cache_save=_save,
    )
    # target's tx-list came from cache → provider.get_address_transactions
    # was never called for T (only the target is expanded at depth 1).
    called_addrs = [c.args[0] for c in prov.get_address_transactions.call_args_list]
    assert T not in called_addrs, "cached tx-list must skip provider pagination for T"
    assert any(n.address == B for n in res.addresses)
