"""Tests for the fallback-chain ordering in _build_providers.

minibf was added to FALLBACK_ORDER after utxorpc, but only kept in the chain
when its base_url is configured (an unconfigured local node would otherwise add
a dead hop). The chosen primary is always moved to the front.
"""
from __future__ import annotations

import asyncio

from utxo_tracer.cli import FALLBACK_ORDER, _build_providers


def _chain(name, cfg):
    p = _build_providers(
        name, cfg, use_fallback=True, api_key=None, base_url=None, auth_type=None,
        endpoint_url=None, kupo_url=None, ogmios_url=None, use_proxy=False,
        proxy_url="http://localhost:3001",
    )
    names = list(getattr(p, "_names", []))
    asyncio.run(p.aclose())
    return names


def _cfg(minibf_url=None):
    return {
        "providers": {
            "blockfrost": {"api_key": "bf"},
            "koios": {"api_key": "k"},
            "maestro": {"api_key": "m"},
            "utxorpc": {"base_url": "http://dolos:50051"},
            "minibf": {"base_url": minibf_url},
        }
    }


def test_minibf_is_after_utxorpc_in_static_order():
    assert FALLBACK_ORDER.index("minibf") > FALLBACK_ORDER.index("utxorpc")


def test_minibf_in_chain_when_base_url_configured():
    chain = _chain("utxorpc", _cfg(minibf_url="http://dolos:50053"))
    # local-first order: utxorpc → minibf, then cloud providers
    assert chain == ["utxorpc", "minibf", "koios", "blockfrost", "maestro"]


def test_minibf_skipped_when_base_url_missing():
    chain = _chain("utxorpc", _cfg(minibf_url=None))
    assert "minibf" not in chain
    assert chain == ["utxorpc", "koios", "blockfrost", "maestro"]


def test_minibf_kept_when_chosen_as_primary_even_without_config():
    # Explicit --provider minibf must never be silently dropped, even if the
    # config has no base_url (it falls back to the localhost default).
    chain = _chain("minibf", _cfg(minibf_url=None))
    assert chain[0] == "minibf"
