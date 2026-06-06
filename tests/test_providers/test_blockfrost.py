"""Unit tests for Blockfrost provider."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from utxo_tracer.models import OutRef
from utxo_tracer.providers.blockfrost import BlockfrostProvider


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mock_client():
    """Create a mock httpx.AsyncClient with configurable .get() and .post()."""
    client = AsyncMock(spec=httpx.AsyncClient)
    # By default return a 404 response
    fake = MagicMock()
    fake.status_code = 404
    fake.json.return_value = {}

    async def _default(*args, **kwargs):
        return fake
    client.get.side_effect = _default
    client.post.side_effect = _default
    return client


def _make_get_response(status_code: int = 200, json_data: dict | None = None):
    """Return an async function that produces a fake httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}

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
    """Health check returns True when /blocks/latest returns 200."""
    provider = BlockfrostProvider(api_key="test_key")
    provider._client = _mock_client()
    provider._client.get.side_effect = _make_get_response(200, {})
    result = await provider.health_check()
    assert result is True


@pytest.mark.asyncio
async def test_health_check_fail():
    """Health check returns False on connection error."""
    provider = BlockfrostProvider(api_key="test_key")
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
    out_ref = OutRef(
        tx_hash="aabbccdd00112233445566778899aabbccdd00112233445566778899aabb",
        output_index=1,
    )
    blockfrost_response = {
        "hash": out_ref.tx_hash,
        "inputs": [],
        "outputs": [
            {
                "address": "addr_test1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",
                "amount": [
                    {"unit": "lovelace", "quantity": "5000000"},
                ],
                "output_index": 0,
            },
            {
                "address": "addr_test1qzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",
                "amount": [
                    {"unit": "lovelace", "quantity": "3000000"},
                ],
                "output_index": 1,
                "data_hash": "abc123",
            },
        ],
    }
    provider = BlockfrostProvider(api_key="test_key")
    provider._client = _mock_client()
    provider._client.get.side_effect = _make_get_response(200, blockfrost_response)
    result = await provider.get_utxo_by_out_ref(out_ref)
    assert result is not None
    assert result.out_ref.tx_hash == out_ref.tx_hash
    assert result.out_ref.output_index == 1
    assert result.datum_hash == "abc123"


@pytest.mark.asyncio
async def test_get_utxo_by_out_ref_404():
    """Returns None when the transaction is not found (404)."""
    out_ref = OutRef(
        tx_hash="deadbeef00112233445566778899aabbccdd00112233445566778899aabb",
        output_index=0,
    )
    provider = BlockfrostProvider(api_key="test_key")
    provider._client = _mock_client()
    provider._client.get.side_effect = _make_get_response(404, {})
    result = await provider.get_utxo_by_out_ref(out_ref)
    assert result is None


# ── get_transaction_utxos ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_transaction_utxos():
    """Returns populated inputs, input_utxos, and outputs dict."""
    tx_hash = "aabbccdd00112233445566778899aabbccdd00112233445566778899aabb"
    blockfrost_response = {
        "hash": tx_hash,
        "inputs": [
            {
                "tx_hash": "input00112233445566778899aabbccdd00112233445566778899aa01",
                "output_index": 0,
                "address": "addr_test1qinputaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "amount": [{"unit": "lovelace", "quantity": "1000000"}],
            },
        ],
        "outputs": [
            {
                "address": "addr_test1qoutputaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "amount": [{"unit": "lovelace", "quantity": "500000"}],
                "output_index": 1,
            },
        ],
    }
    provider = BlockfrostProvider(api_key="test_key")
    provider._client = _mock_client()
    provider._client.get.side_effect = _make_get_response(200, blockfrost_response)
    result = await provider.get_transaction_utxos(tx_hash)

    assert "inputs" in result
    assert "input_utxos" in result
    assert "outputs" in result
    assert len(result["inputs"]) == 1
    assert len(result["outputs"]) == 1
    assert result["inputs"][0].tx_hash == "input00112233445566778899aabbccdd00112233445566778899aa01"
    assert result["outputs"][0].out_ref.tx_hash == tx_hash


# ── Auth Headers ─────────────────────────────────────────────────────────────

def test_auth_header_project_id():
    """Sets project_id header when auth_type='project_id'."""
    provider = BlockfrostProvider(api_key="test_key", auth_type="project_id")
    assert "project_id" in provider._client.headers
    assert provider._client.headers["project_id"] == "test_key"


def test_auth_header_bearer():
    """Sets Authorization: Bearer header when auth_type='bearer'."""
    provider = BlockfrostProvider(api_key="test_key", auth_type="bearer")
    assert "Authorization" in provider._client.headers
    assert provider._client.headers["Authorization"] == "Bearer test_key"


def test_auth_header_dmtr():
    """Sets dmtr-api-key header when auth_type='dmtr-api-key'."""
    provider = BlockfrostProvider(
        api_key="test_key",
        auth_type="dmtr-api-key",
        endpoint_url="https://test.demeter.run",
    )
    assert "dmtr-api-key" in provider._client.headers
    assert provider._client.headers["dmtr-api-key"] == "test_key"
    assert provider._client.headers.get("x-blockfrost-endpoint") == "https://test.demeter.run"
