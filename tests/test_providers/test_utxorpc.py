"""TDD tests for UTxORPC provider — get_spent_utxos bug fix + ReadTx path."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from utxo_tracer.models import OutRef
from utxo_tracer.providers.utxorpc import UTxORPCProvider
from utxo_tracer.utils import (
    _bech32_to_bytes,
    classify_address,
    encode_cardano_address,
)


@pytest.mark.asyncio
async def test_get_spent_utxos_raises_not_implemented_error():
    """get_spent_utxos MUST raise NotImplementedError.
    
    Current implementation tries to call async_search_utxos which returns
    CURRENT UTXOs (not spent ones) — that is wrong data. Instead it should
    raise NotImplementedError immediately.
    """
    provider = UTxORPCProvider(api_key="", base_url="")
    with pytest.raises(NotImplementedError):
        await provider.get_spent_utxos(
            "addr_test1qpc6mrwu9cucrq4w6y69qchflvypq76a47ylvjvm2w"
            "kphyajfxk6cw5p8f2w5hv2htc9x4nl2p7prp4acxn22zmdq4qgxrg7u"
        )


@pytest.mark.asyncio
async def test_get_spent_utxos_does_not_return_wrong_data():
    """get_spent_utxos MUST NOT return current UTXO data as spent.
    
    Even if async_search_utxos somehow succeeds, the method should raise
    NotImplementedError BEFORE any gRPC call is made — verifying it never
    returns wrong data (current UTXOs presented as spent ones).
    """
    provider = UTxORPCProvider(api_key="", base_url="")
    with pytest.raises(NotImplementedError):
        # Should raise immediately, never reach gRPC layer
        await provider.get_spent_utxos("addr_test1...")


# ── Address Bech32 encoding (CIP-19) ──────────────────────────────────
# UTxORPC delivers addresses as raw header+credential bytes; the provider must
# re-encode to Bech32 so classify_address + CEX matching behave like every
# other provider. Regression for the "addresses arrive as hex → UNKNOWN" bug.

class TestEncodeCardanoAddress:
    def test_mainnet_base_address_roundtrips_to_wallet(self):
        # header 0x01 = type 0 (base, payment key + stake key), network 1 (mainnet)
        raw = bytes([0x01]) + b"\x11" * 28 + b"\x22" * 28
        enc = encode_cardano_address(raw)
        assert enc.startswith("addr1")
        assert _bech32_to_bytes(enc) == raw  # checksum + 5/8-bit conversion roundtrip
        assert classify_address(enc).value == "wallet"

    def test_mainnet_base_script_address_classifies_script(self):
        # header 0x31 = type 3 (base, script payment), network 1
        raw = bytes([0x31]) + b"\x33" * 28 + b"\x44" * 28
        enc = encode_cardano_address(raw)
        assert enc.startswith("addr1")
        assert classify_address(enc).value == "script"

    def test_enterprise_script_address(self):
        # header 0x71 = type 7 (enterprise, script only), network 1
        raw = bytes([0x71]) + b"\x55" * 28
        enc = encode_cardano_address(raw)
        assert enc.startswith("addr1")
        assert _bech32_to_bytes(enc) == raw

    def test_stake_address_uses_stake_hrp(self):
        # header 0xe1 = type 14 (reward/stake key), network 1
        raw = bytes([0xE1]) + b"\x66" * 28
        enc = encode_cardano_address(raw)
        assert enc.startswith("stake1")

    def test_testnet_address_uses_addr_test_hrp(self):
        # header 0x00 = type 0, network 0 (testnet)
        raw = bytes([0x00]) + b"\x11" * 28 + b"\x22" * 28
        enc = encode_cardano_address(raw)
        assert enc.startswith("addr_test1")

    def test_byron_or_unknown_falls_back_to_hex(self):
        # header 0x82 = type 8 (Byron bootstrap) → no Bech32 form, return hex
        raw = bytes([0x82, 0xAB, 0xCD])
        assert encode_cardano_address(raw) == raw.hex()

    def test_empty_bytes(self):
        assert encode_cardano_address(b"") == ""


# ── ReadTx path: get_transaction_utxos must work for SPENT outputs ────────
# ReadUtxos only returns the live (unspent) UTXO set, so historical txs (whose
# outputs are spent) returned {inputs:[], outputs:[]} — backward tracing was
# silently empty. ReadTx returns inputs + outputs regardless. Regression test.

def _fake_tx_output(addr_bytes: bytes, lovelace: int):
    """Minimal stand-in for a cardano TxOutput proto used by _parse_tx_output."""
    from unittest.mock import MagicMock

    coin = MagicMock()
    coin.HasField.side_effect = lambda f: f == "int"
    coin.int = lovelace
    out = MagicMock()
    out.address = addr_bytes
    out.coin = coin
    out.assets = []
    out.HasField.return_value = False  # no datum / no script
    return out


@pytest.mark.asyncio
async def test_get_transaction_utxos_uses_readtx_for_spent_outputs():
    provider = UTxORPCProvider(base_url="http://localhost:50051")

    raw_addr = bytes([0x01]) + b"\x11" * 28 + b"\x22" * 28
    fake = {
        "inputs": [OutRef(tx_hash="aa" * 32, output_index=2)],
        "outputs": [provider._parse_tx_output("bb" * 32, _fake_tx_output(raw_addr, 5_000_000), 0)],
    }
    with patch.object(provider, "_read_tx", AsyncMock(return_value=fake)) as rt:
        res = await provider.get_transaction_utxos("bb" * 32)

    rt.assert_awaited_once()
    assert len(res["inputs"]) == 1
    assert len(res["outputs"]) == 1
    # address must be Bech32, not hex
    assert res["outputs"][0].address.startswith("addr1")
    assert res["outputs"][0].lovelace == 5_000_000


@pytest.mark.asyncio
async def test_get_transaction_utxos_caches_readtx_result():
    provider = UTxORPCProvider(base_url="http://localhost:50051")
    fake = {"inputs": [], "outputs": []}
    with patch.object(provider, "_read_tx", AsyncMock(return_value=fake)) as rt:
        await provider.get_transaction_utxos("cc" * 32)
        await provider.get_transaction_utxos("cc" * 32)  # cache hit
    rt.assert_awaited_once()  # second call served from cache
