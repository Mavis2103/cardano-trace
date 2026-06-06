"""Unit tests for Koios provider."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from utxo_tracer.models import OutRef
from utxo_tracer.providers.koios import KoiosProvider


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mock_client():
    """Create a mock httpx.AsyncClient with configurable .post()."""
    client = AsyncMock(spec=httpx.AsyncClient)
    fake = MagicMock()
    fake.status_code = 404
    fake.json.return_value = []

    async def _default(*args, **kwargs):
        return fake
    client.post.side_effect = _default
    client.get.side_effect = _default
    return client


def _make_post_response(status_code: int = 200, json_data=None):
    """Return an async function that produces a fake httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else []

    async def _post(*args, **kwargs):
        if status_code >= 400:
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Error", request=MagicMock(), response=resp
            )
        return resp
    return _post


def _koios_utxo(tx_hash: str, tx_index: int, address: str, value: str = "5000000",
                extra: dict | None = None):
    """Create a Koios-formatted UTXO dict matching _parse_utxo expectations."""
    item: dict = {
        "tx_hash": tx_hash,
        "tx_index": tx_index,
        "payment_addr": {"bech32": address},
        "value": value,
        "asset_list": [],
    }
    if extra:
        item.update(extra)
    return item


# ── Health Check ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_check_ok():
    """Health check returns True when /tip returns 200."""
    provider = KoiosProvider()
    provider._client = _mock_client()
    provider._client.post.side_effect = _make_post_response(200, {})
    result = await provider.health_check()
    assert result is True


@pytest.mark.asyncio
async def test_health_check_fail():
    """Health check returns False on connection error."""
    provider = KoiosProvider()

    async def _raise(*args, **kwargs):
        raise httpx.ConnectError("Connection refused")
    provider._client = _mock_client()
    provider._client.post.side_effect = _raise
    result = await provider.health_check()
    assert result is False


# ── get_utxo_by_out_ref ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_utxo_by_out_ref():
    """Returns UTxONode for a valid UTXO reference."""
    out_ref = OutRef(
        tx_hash="aabbccdd00112233445566778899aabbccdd00112233445566778899aabb",
        output_index=1,
    )
    koios_data = [
        _koios_utxo(
            tx_hash=out_ref.tx_hash,
            tx_index=1,
            address="addr_test1qzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",
            value="3000000",
            extra={"datum_hash": "abc123"},
        )
    ]
    provider = KoiosProvider()
    provider._client = _mock_client()
    provider._client.post.side_effect = _make_post_response(200, koios_data)
    result = await provider.get_utxo_by_out_ref(out_ref)
    assert result is not None
    assert result.out_ref.tx_hash == out_ref.tx_hash
    assert result.out_ref.output_index == 1
    assert result.datum_hash == "abc123"


@pytest.mark.asyncio
async def test_get_utxo_by_out_ref_empty():
    """Returns None when Koios returns empty list."""
    out_ref = OutRef(
        tx_hash="deadbeef00112233445566778899aabbccdd00112233445566778899aabb",
        output_index=0,
    )
    provider = KoiosProvider()
    provider._client = _mock_client()
    provider._client.post.side_effect = _make_post_response(200, [])
    result = await provider.get_utxo_by_out_ref(out_ref)
    assert result is None


# ── get_transaction_utxos ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_transaction_utxos():
    """Returns populated inputs and outputs for a transaction."""
    tx_hash = "aabbccdd00112233445566778899aabbccdd00112233445566778899aabb"
    koios_data = [{
        "tx_hash": tx_hash,
        "inputs": [
            _koios_utxo(
                tx_hash="input11111111111111111111111111111111111111111111111111111111",
                tx_index=0,
                address="addr_test1qinputaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                value="1000000",
            ),
        ],
        "outputs": [
            _koios_utxo(
                tx_hash=tx_hash,
                tx_index=1,
                address="addr_test1qoutputaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                value="500000",
            ),
        ],
    }]
    provider = KoiosProvider()
    provider._client = _mock_client()
    provider._client.post.side_effect = _make_post_response(200, koios_data)
    result = await provider.get_transaction_utxos(tx_hash)

    assert "inputs" in result
    assert "outputs" in result
    assert len(result["inputs"]) == 1
    assert len(result["outputs"]) == 1
    assert result["inputs"][0].tx_hash == "input11111111111111111111111111111111111111111111111111111111"


# ── Batch fetch ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_transactions_utxos_batch():
    """Batch endpoint returns correct number of results."""
    tx_hashes = [
        "aaaa000000000000000000000000000000000000000000000000000000000001",
        "bbbb000000000000000000000000000000000000000000000000000000000002",
    ]
    koios_data = [
        {
            "tx_hash": tx_hashes[0],
            "inputs": [],
            "outputs": [
                _koios_utxo(tx_hashes[0], 0, "addr1", "1000"),
            ],
        },
        {
            "tx_hash": tx_hashes[1],
            "inputs": [],
            "outputs": [
                _koios_utxo(tx_hashes[1], 0, "addr2", "2000"),
            ],
        },
    ]
    provider = KoiosProvider()
    provider._client = _mock_client()
    provider._client.post.side_effect = _make_post_response(200, koios_data)
    results = await provider.get_transactions_utxos(tx_hashes)
    assert len(results) == 2
    assert len(results[0]["outputs"]) == 1
    assert len(results[1]["outputs"]) == 1


# ── get_address_transactions ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_address_transactions():
    """Returns list of unique tx hashes for an address."""
    koios_data = [
        {"tx_hash": "tx1111111111111111111111111111111111111111111111111111111111111111"},
        {"tx_hash": "tx2222222222222222222222222222222222222222222222222222222222222222"},
        # Duplicate — should be deduplicated
        {"tx_hash": "tx1111111111111111111111111111111111111111111111111111111111111111"},
    ]
    provider = KoiosProvider()
    provider._client = _mock_client()
    provider._client.post.side_effect = _make_post_response(200, koios_data)
    result = await provider.get_address_transactions("addr_test1q...")
    assert len(result) == 2
    assert "tx1111111111111111111111111111111111111111111111111111111111111111" in result
    assert "tx2222222222222222222222222222222222222222222222222222222222222222" in result


# ── Capabilities ─────────────────────────────────────────────────────────────

def test_supports_forward():
    """Koios supports forward tracing."""
    assert KoiosProvider.supports_forward is True
