"""Providers package."""

from __future__ import annotations

import logging
from typing import Any

from .base import CapabilityError, Provider
from .blockfrost import BlockfrostProvider
from .fallback import FallbackProvider
from .koios import KoiosProvider
from .kupmios import KupmiosProvider
from .maestro import MaestroProvider
from .utxorpc import UTxORPCProvider

_LOGGER = logging.getLogger(__name__)


def build_provider(
    name: str,
    cfg: dict[str, Any],
    *,
    use_proxy: bool = False,
    proxy_url: str = "http://localhost:3001",
    overrides: dict[str, Any] | None = None,
) -> Provider:
    """Construct a provider from a config dict, with CLI overrides."""
    overrides = overrides or {}
    name = name.lower()

    def merged(key: str, default: Any = None) -> Any:
        v = overrides.get(key)
        if v is not None:
            return v
        v = cfg.get(key)
        if v is not None:
            return v
        return default

    if name == "blockfrost":
        api_key = merged("api_key") or ""
        if not api_key:
            _LOGGER.warning(
                "Blockfrost provider initialized without API key — "
                "some endpoints may be unavailable"
            )
        base_url = merged("base_url")
        if not base_url:
            base_url = (
                f"{proxy_url}/api/blockfrost"
                if use_proxy
                else "https://cardano-mainnet.blockfrost.io/api/v0"
            )
        return BlockfrostProvider(
            api_key=api_key,
            base_url=base_url,
            auth_type=merged("auth_type", "project_id") or "project_id",
            endpoint_url=merged("endpoint_url"),
        )

    if name == "koios":
        base_url = merged("base_url")
        if not base_url:
            base_url = (
                f"{proxy_url}/api/koios"
                if use_proxy
                else "https://api.koios.rest/api/v1"
            )
        return KoiosProvider(
            api_key=merged("api_key"),
            base_url=base_url,
        )

    if name == "maestro":
        base_url = merged("base_url")
        if not base_url:
            base_url = (
                f"{proxy_url}/api/maestro"
                if use_proxy
                else "https://mainnet.gomaestro-api.org/v1"
            )
        return MaestroProvider(
            api_key=merged("api_key") or "",
            base_url=base_url,
        )

    if name == "kupmios":
        return KupmiosProvider(
            kupo_url=merged("kupo_url") or "http://localhost:1442",
            ogmios_url=merged("ogmios_url") or "http://localhost:1337",
            kupo_api_key=merged("kupo_api_key"),
            ogmios_api_key=merged("ogmios_api_key"),
        )

    if name == "utxorpc":
        return UTxORPCProvider(
            api_key=merged("api_key"),
            base_url=merged("base_url"),
            endpoint_url=merged("endpoint_url"),
        )

    raise ValueError(f"Unknown provider: {name}")


__all__ = [
    "Provider",
    "BlockfrostProvider",
    "CapabilityError",
    "KoiosProvider",
    "KupmiosProvider",
    "MaestroProvider",
    "UTxORPCProvider",
    "FallbackProvider",
    "build_provider",
]
