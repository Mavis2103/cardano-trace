"""Unit tests for Maestro provider."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from utxo_tracer.models import OutRef
from utxo_tracer.providers.maestro import MaestroProvider


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mock_client():
    """Create a mock httpx.AsyncClient with configurable .get()."""
    client = AsyncMock(spec=httpx.AsyncClient)
    fake = MagicMock()
    fake.status_code = 404
    fake.json.return_value = {}

    async def _default(*args, **kwargs):
        return fake
    client.get.side_effect = _default
    return client


def _make_get_response(status_code: int = 200, json_data=None):
    """Return an async function that produces a fake httpx.Response."""
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


# ── Health Check ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_check_ok():
    """Health check returns True when /health returns 200."""
    provider = MaestroProvider(api_key="test_key")
    provider._client = _mock_client()
    provider._client.get.side_effect = _make_get_response(200, {})
    result = await provider.health_check()
    assert result is True


@pytest.mark.asyncio
async def test_health_check_fallback():
    """Health check uses /chain-tip fallback when /health fails."""
    provider = MaestroProvider(api_key="test_key")
    provider._client = _mock_client()

    call_count = [0]

    async def _multi(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            # /health fails with 500
            resp = MagicMock()
            resp.status_code = 500
            return resp
        else:
            # /chain-tip succeeds
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {}
            return resp

    provider._client.get.side_effect = _multi
    result = await provider.health_check()
    assert result is True
    assert call_count[0] == 2  # tried both endpoints


@pytest.mark.asyncio
async def test_health_check_fail():
    """Health check returns False when both endpoints fail."""
    provider = MaestroProvider(api_key="test_key")
    provider._client = _mock_client()

    async def _raise(*args, **kwargs):
        raise httpx.ConnectError("Connection refused")

    provider._client.get.side_effect = _raise
    result = await provider.health_check()
    assert result is False


# ── get_utxo_by_out_ref ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_utxo_by_out_ref():
    """Returns UTxONode when output is found at the expected index."""
    tx_hash = "aabbccdd00112233445566778899aabbccdd00112233445566778899aabb"
    out_ref = OutRef(tx_hash=tx_hash, output_index=1)

    maestro_tx = {
        "outputs": [
            {
                "index": 0,
                "address": "addr_test1qa...",
                "assets": [{"unit": "lovelace", "amount": "5000000"}],
            },
            {
                "index": 1,
                "address": "addr_test1qz...",
                "assets": [{"unit": "lovelace", "amount": "3000000"}],
                "datum": {"hash": "abc123", "type": "hash"},
            },
        ],
    }

    provider = MaestroProvider(api_key="test_key")
    provider._client = _mock_client()
    provider._client.get.side_effect = _make_get_response(200, maestro_tx)
    result = await provider.get_utxo_by_out_ref(out_ref)
    assert result is not None
    assert result.out_ref.tx_hash == tx_hash
    assert result.out_ref.output_index == 1
    assert result.datum_hash == "abc123"


@pytest.mark.asyncio
async def test_get_utxo_by_out_ref_wrapped():
    """Handles Maestro's {"data": {...}} wrapper."""
    tx_hash = "bbaa000000000000000000000000000000000000000000000000000000000000"
    out_ref = OutRef(tx_hash=tx_hash, output_index=0)

    maestro_tx = {
        "data": {
            "outputs": [
                {
                    "index": 0,
                    "address": "addr_test1qw...",
                    "assets": [{"unit": "lovelace", "amount": "2000000"}],
                },
            ],
        },
    }

    provider = MaestroProvider(api_key="test_key")
    provider._client = _mock_client()
    provider._client.get.side_effect = _make_get_response(200, maestro_tx)
    result = await provider.get_utxo_by_out_ref(out_ref)
    assert result is not None
    assert result.out_ref.output_index == 0


# ── get_transaction_utxos ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_transaction_utxos():
    """Returns correct inputs and outputs for a transaction."""
    tx_hash = "txhash0000000000000000000000000000000000000000000000000000000000"
    maestro_tx = {
        "inputs": [
            {
                "tx_hash": "input11111111111111111111111111111111111111111111111111111111",
                "index": 0,
                "address": "addr_test1qin...",
                "assets": [{"unit": "lovelace", "amount": "1000000"}],
            },
        ],
        "outputs": [
            {
                "index": 1,
                "address": "addr_test1qout...",
                "assets": [{"unit": "lovelace", "amount": "500000"}],
            },
        ],
    }

    provider = MaestroProvider(api_key="test_key")
    provider._client = _mock_client()
    provider._client.get.side_effect = _make_get_response(200, maestro_tx)
    result = await provider.get_transaction_utxos(tx_hash)

    assert "inputs" in result
    assert "outputs" in result
    assert len(result["inputs"]) == 1
    assert len(result["outputs"]) == 1
    assert result["inputs"][0].tx_hash == "input11111111111111111111111111111111111111111111111111111111"


# ── Capabilities ─────────────────────────────────────────────────────────────

def test_supports_forward_false():
    """Maestro does NOT support forward tracing."""
    assert MaestroProvider.supports_forward is False
