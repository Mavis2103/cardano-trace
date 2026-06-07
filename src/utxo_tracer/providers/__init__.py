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
from .rotating import RotatingKeyProvider, split_api_keys
from .utxorpc import UTxORPCProvider

_LOGGER = logging.getLogger(__name__)


def _wrap_multikey(
    log_name: str,
    tag_prefix: str,
    keys: list[str],
    make: Any,
) -> Provider:
    """Build one provider, or a ``RotatingKeyProvider`` when >1 key is given.

    ``make(key)`` constructs a single provider instance for ``key`` — called
    with ``None`` when no key was supplied, so each factory applies its own
    default. ``log_name`` / ``tag_prefix`` keep the log line and per-key
    instance tags identical to the previous hand-rolled blocks.
    """
    if len(keys) <= 1:
        return make(keys[0] if keys else None)
    instances: list[tuple[str, Provider]] = [
        (f"{tag_prefix}-{i}", make(key)) for i, key in enumerate(keys)
    ]
    _LOGGER.info(
        "%s: created %d instances for rotating-key provider", log_name, len(keys)
    )
    return RotatingKeyProvider(instances)


def build_provider(
    name: str,
    cfg: dict[str, Any],
    *,
    use_proxy: bool = False,
    proxy_url: str = "http://localhost:3001",
    overrides: dict[str, Any] | None = None,
) -> Provider:
    """Construct a provider from a config dict, with CLI overrides.

    Supports multiple API keys via comma-separated values in ``api_key``.
    When multiple keys are provided, the provider is wrapped in a
    ``RotatingKeyProvider`` that auto-rotates on HTTP 429 responses.
    """
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
        keys = split_api_keys(merged("api_key") or "")
        if not keys:
            _LOGGER.warning(
                "Blockfrost provider initialized without API key — "
                "some endpoints may be unavailable"
            )
        base_url = merged("base_url") or (
            f"{proxy_url}/api/blockfrost"
            if use_proxy
            else "https://cardano-mainnet.blockfrost.io/api/v0"
        )
        auth_type = merged("auth_type", "project_id") or "project_id"
        endpoint_url = merged("endpoint_url")
        return _wrap_multikey(
            "Blockfrost",
            "bf-key",
            keys,
            lambda k: BlockfrostProvider(
                api_key=k or "",
                base_url=base_url,
                auth_type=auth_type,
                endpoint_url=endpoint_url,
            ),
        )

    if name == "koios":
        keys = split_api_keys(merged("api_key") or "")
        base_url = merged("base_url") or (
            f"{proxy_url}/api/koios"
            if use_proxy
            else "https://api.koios.rest/api/v1"
        )
        return _wrap_multikey(
            "Koios",
            "koios-key",
            keys,
            lambda k: KoiosProvider(api_key=k, base_url=base_url),
        )

    if name == "maestro":
        keys = split_api_keys(merged("api_key") or "")
        base_url = merged("base_url") or (
            f"{proxy_url}/api/maestro"
            if use_proxy
            else "https://mainnet.gomaestro-api.org/v1"
        )
        return _wrap_multikey(
            "Maestro",
            "maestro-key",
            keys,
            lambda k: MaestroProvider(api_key=k or "", base_url=base_url),
        )

    if name == "kupmios":
        raw_kupo = merged("kupo_url") or "http://localhost:1442"
        kupo_urls = split_api_keys(raw_kupo)
        kupo_api_keys = split_api_keys(merged("kupo_api_key") or "")

        # Pad api_keys to match the number of Kupo URLs (multi-instance rotation).
        n = max(len(kupo_urls), len(kupo_api_keys), 1)
        while len(kupo_urls) < n:
            kupo_urls.append(kupo_urls[0])
        while len(kupo_api_keys) < n:
            kupo_api_keys.append(kupo_api_keys[0] if kupo_api_keys else "")

        if n <= 1:
            return KupmiosProvider(
                kupo_url=kupo_urls[0],
                kupo_api_key=kupo_api_keys[0],
            )

        instances: list[tuple[str, Provider]] = []
        for i in range(n):
            instances.append((
                f"kupo-{i}",
                KupmiosProvider(
                    kupo_url=kupo_urls[i],
                    kupo_api_key=kupo_api_keys[i],
                ),
            ))
        _LOGGER.info(
            "Kupmios: created %d instances for rotating-key provider",
            n,
        )
        return RotatingKeyProvider(instances)

    if name == "minibf":
        # Dolos minibf — Blockfrost-compatible REST served at the root path
        # (no /api/v0 prefix), typically no auth. Reuses the Blockfrost driver.
        base_url = merged("base_url")
        if not base_url:
            base_url = (
                f"{proxy_url}/api/minibf"
                if use_proxy
                else "http://localhost:50053"
            )
        auth_type = merged("auth_type", "project_id") or "project_id"
        return BlockfrostProvider(
            api_key=merged("api_key") or "",
            base_url=base_url,
            auth_type=auth_type,
            endpoint_url=merged("endpoint_url"),
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
    "RotatingKeyProvider",
    "split_api_keys",
    "build_provider",
]
