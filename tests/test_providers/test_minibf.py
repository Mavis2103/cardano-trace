"""Wiring tests for the minibf provider.

minibf = Dolos's Blockfrost-compatible REST subset. It has no dedicated class;
``build_provider("minibf")`` reuses ``BlockfrostProvider`` pointed at Dolos's
REST root (no ``/api/v0`` prefix, auth optional). These tests lock that wiring
so a refactor can't silently break minibf (wrong prefix / wrong default port /
wrong driver).

Endpoint coverage is verified by the live dolos run, not here; the BlockfrostProvider
behaviour itself is covered by tests/test_providers/test_blockfrost*.
"""
from __future__ import annotations

from utxo_tracer.providers import BlockfrostProvider, build_provider


def test_minibf_uses_blockfrost_driver():
    p = build_provider("minibf", {"base_url": "http://dolos.local:50053"})
    assert isinstance(p, BlockfrostProvider)
    assert p.provider_type == "blockfrost"
    assert p.supports_forward is True


def test_minibf_base_url_has_no_api_v0_prefix():
    # Dolos minibf serves Blockfrost routes at the ROOT path, unlike hosted
    # Blockfrost (/api/v0). The configured base_url must be used verbatim.
    p = build_provider("minibf", {"base_url": "http://dolos.local:50053"})
    assert p.base_url == "http://dolos.local:50053"
    assert "/api/v0" not in p.base_url


def test_minibf_default_base_url_when_unset():
    p = build_provider("minibf", {})
    assert p.base_url == "http://localhost:50053"


def test_minibf_proxy_base_url():
    p = build_provider("minibf", {}, use_proxy=True, proxy_url="http://localhost:3001")
    assert p.base_url == "http://localhost:3001/api/minibf"


def test_minibf_works_without_api_key():
    # minibf needs no auth; empty key must not raise and must send no auth header.
    p = build_provider("minibf", {"base_url": "http://dolos.local:50053"})
    assert "project_id" not in p._client.headers
    assert "Authorization" not in p._client.headers


def test_minibf_optional_api_key_sets_project_id_header():
    p = build_provider(
        "minibf", {"base_url": "http://dolos.local:50053", "api_key": "tok"}
    )
    assert p._client.headers.get("project_id") == "tok"
