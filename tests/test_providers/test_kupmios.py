"""Unit tests for the Kupo provider (provider_type "kupmios", Kupo-only)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from utxo_tracer.models import OutRef
from utxo_tracer.providers.kupmios import KupmiosProvider


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mock_kupo_client():
    """Create a mock httpx.AsyncClient for Kupo."""
    client = AsyncMock(spec=httpx.AsyncClient)
    fake = MagicMock()
    fake.status_code = 404
    fake.json.return_value = []

    async def _default(*args, **kwargs):
        return fake
    client.get.side_effect = _default
    client.post.side_effect = _default
    return client


def _make_get_response(status_code: int = 200, json_data=None):
    """Return async function producing a fake httpx.Response for .get()."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}

    async def _get(*args, **kwargs):
        if status_code >= 400:
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Error", request=MagicMock(), response=resp
            )
        return resp
    return _get


def _kupo_match(tx_id: str, output_index: int, address: str,
                coins: int = 5000000, extra: dict | None = None):
    """Create a Kupo-formatted match dict."""
    item: dict = {
        "transaction_id": tx_id,
        "output_index": output_index,
        "address": address,
        "value": {"coins": coins, "assets": {}},
    }
    if extra:
        item.update(extra)
    return item


# ── Health Check ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_check_kupo_ok():
    """Health returns True when Kupo /health returns 200."""
    provider = KupmiosProvider()
    provider._kupo = _mock_kupo_client()
    provider._kupo.get.side_effect = _make_get_response(200, {})
    result = await provider.health_check()
    assert result is True


@pytest.mark.asyncio
async def test_health_check_kupo_fail():
    """Health returns False when Kupo is down."""
    provider = KupmiosProvider()
    provider._kupo = _mock_kupo_client()

    async def _kupo_raise(*args, **kwargs):
        raise httpx.ConnectError("Kupo down")
    provider._kupo.get.side_effect = _kupo_raise
    result = await provider.health_check()
    assert result is False


@pytest.mark.asyncio
async def test_health_check_is_kupo_only():
    """Provider has no Ogmios dependency — health is driven solely by Kupo,
    and the provider declares it cannot trace backward."""
    provider = KupmiosProvider()
    assert not hasattr(provider, "_ogmios")
    assert provider.supports_backward is False
    assert provider.supports_forward is True


# ── get_utxo_by_out_ref ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_utxo_by_out_ref():
    """Returns UTxONode from Kupo match."""
    tx_hash = "aabbccdd00112233445566778899aabbccdd00112233445566778899aabb"
    out_ref = OutRef(tx_hash=tx_hash, output_index=1)

    match = _kupo_match(
        tx_id=tx_hash,
        output_index=1,
        address="addr_test1qz...",
        coins=3000000,
        extra={"datum_hash": "abc123", "datum_type": "hash"},
    )

    provider = KupmiosProvider()
    provider._kupo = _mock_kupo_client()
    provider._kupo.get.side_effect = _make_get_response(200, [match])

    result = await provider.get_utxo_by_out_ref(out_ref)
    assert result is not None
    assert result.out_ref.tx_hash == tx_hash
    assert result.out_ref.output_index == 1
    assert result.datum_hash == "abc123"


@pytest.mark.asyncio
async def test_get_utxo_by_out_ref_404():
    """Returns None when Kupo returns 404."""
    out_ref = OutRef(
        tx_hash="deadbeef00112233445566778899aabbccdd00112233445566778899aabb",
        output_index=0,
    )
    provider = KupmiosProvider()
    provider._kupo = _mock_kupo_client()
    provider._kupo.get.side_effect = _make_get_response(404, [])

    result = await provider.get_utxo_by_out_ref(out_ref)
    assert result is None


# ── get_transaction_utxos ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_transaction_utxos():
    """Returns outputs from Kupo; inputs are always empty.

    Kupo can't resolve a tx's inputs (no reverse query), so backward tracing is
    unsupported and ``inputs`` is always ``[]``. Forward tracing uses only the
    outputs.
    """
    tx_hash = "aabbccdd00112233445566778899aabbccdd00112233445566778899aabb"

    kupo_outputs = [
        _kupo_match(tx_id=tx_hash, output_index=0, address="addr_out0...", coins=500000),
        _kupo_match(tx_id=tx_hash, output_index=1, address="addr_out1...", coins=700000),
    ]

    provider = KupmiosProvider()
    provider._kupo = _mock_kupo_client()
    provider._kupo.get.side_effect = _make_get_response(200, kupo_outputs)

    result = await provider.get_transaction_utxos(tx_hash)
    assert result["inputs"] == []
    assert len(result["outputs"]) == 2


# ── get_address_transactions ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_address_transactions():
    """Collects both the creating tx (top-level transaction_id) and the
    spending tx (spent_at.transaction_id).

    Kupo's ``created_at`` carries only {slot_no, header_hash} — NOT a
    transaction_id — so the creating tx (address received funds) must come from
    the match's top-level ``transaction_id`` field, not created_at.
    """
    kupo_data = [
        {
            # received-only UTXO: creating tx in top-level transaction_id, unspent
            "transaction_id": "txaaaa0000000000000000000000000000000000000000000000000000001",
            "output_index": 0,
            "address": "addr_test...",
            "value": {"coins": 1000000, "assets": {}},
            "created_at": {"slot_no": 100, "header_hash": "aa"},
        },
        {
            # created by txbbbb, later spent by txcccc
            "transaction_id": "txbbbb0000000000000000000000000000000000000000000000000000002",
            "output_index": 1,
            "address": "addr_test...",
            "value": {"coins": 2000000, "assets": {}},
            "created_at": {"slot_no": 200, "header_hash": "bb"},
            "spent_at": {"slot_no": 300, "header_hash": "cc",
                         "transaction_id": "txcccc0000000000000000000000000000000000000000000000000000003"},
        },
    ]

    provider = KupmiosProvider()
    provider._kupo = _mock_kupo_client()
    provider._kupo.get.side_effect = _make_get_response(200, kupo_data)

    result = await provider.get_address_transactions("addr_test...")
    assert set(result) == {
        "txaaaa0000000000000000000000000000000000000000000000000000001",  # created (received)
        "txbbbb0000000000000000000000000000000000000000000000000000002",  # created (received)
        "txcccc0000000000000000000000000000000000000000000000000000003",  # spent (sent)
    }


# ── get_address_spend_map ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_address_spend_map():
    """Returns correct spend map from Kupo ?spent results."""
    kupo_data = [
        {
            "transaction_id": "spentutx00000000000000000000000000000000000000000000000000000001",
            "output_index": 0,
            "address": "addr_test...",
            "value": {"coins": 1000000, "assets": {}},
            "spent_at": {"transaction_id": "spender00000000000000000000000000000000000000000000000000000001"},
        },
    ]

    provider = KupmiosProvider()
    provider._kupo = _mock_kupo_client()
    provider._kupo.get.side_effect = _make_get_response(200, kupo_data)

    result = await provider.get_address_spend_map("addr_test...")
    assert len(result) == 1
    assert result["spentutx00000000000000000000000000000000000000000000000000000001:0"] == "spender00000000000000000000000000000000000000000000000000000001"


# ── Capabilities ─────────────────────────────────────────────────────────────

def test_supports_forward():
    """Kupmios supports forward tracing."""
    assert KupmiosProvider.supports_forward is True
