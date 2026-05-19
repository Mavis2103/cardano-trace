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
        raw = merged("api_key") or ""
        keys = split_api_keys(raw)
        if not keys:
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
        auth_type = merged("auth_type", "project_id") or "project_id"
        endpoint_url = merged("endpoint_url")

        if len(keys) <= 1:
            return BlockfrostProvider(
                api_key=keys[0] if keys else "",
                base_url=base_url,
                auth_type=auth_type,
                endpoint_url=endpoint_url,
            )

        instances: list[tuple[str, Provider]] = []
        for i, key in enumerate(keys):
            name_tag = f"bf-key-{i}"
            instances.append((
                name_tag,
                BlockfrostProvider(
                    api_key=key,
                    base_url=base_url,
                    auth_type=auth_type,
                    endpoint_url=endpoint_url,
                ),
            ))
        _LOGGER.info(
            "Blockfrost: created %d instances for rotating-key provider",
            len(keys),
        )
        return RotatingKeyProvider(instances)

    if name == "koios":
        raw = merged("api_key") or ""
        keys = split_api_keys(raw)
        base_url = merged("base_url")
        if not base_url:
            base_url = (
                f"{proxy_url}/api/koios"
                if use_proxy
                else "https://api.koios.rest/api/v1"
            )

        if len(keys) <= 1:
            return KoiosProvider(
                api_key=keys[0] if keys else None,
                base_url=base_url,
            )

        instances: list[tuple[str, Provider]] = []
        for i, key in enumerate(keys):
            instances.append((
                f"koios-key-{i}",
                KoiosProvider(api_key=key, base_url=base_url),
            ))
        _LOGGER.info(
            "Koios: created %d instances for rotating-key provider",
            len(keys),
        )
        return RotatingKeyProvider(instances)

    if name == "maestro":
        raw = merged("api_key") or ""
        keys = split_api_keys(raw)
        base_url = merged("base_url")
        if not base_url:
            base_url = (
                f"{proxy_url}/api/maestro"
                if use_proxy
                else "https://mainnet.gomaestro-api.org/v1"
            )

        if len(keys) <= 1:
            return MaestroProvider(
                api_key=keys[0] if keys else "",
                base_url=base_url,
            )

        instances: list[tuple[str, Provider]] = []
        for i, key in enumerate(keys):
            instances.append((
                f"maestro-key-{i}",
                MaestroProvider(api_key=key, base_url=base_url),
            ))
        _LOGGER.info(
            "Maestro: created %d instances for rotating-key provider",
            len(keys),
        )
        return RotatingKeyProvider(instances)

    if name == "kupmios":
        raw_kupo = merged("kupo_url") or "http://localhost:1442"
        raw_ogmios = merged("ogmios_url") or "http://localhost:1337"
        kupo_urls = split_api_keys(raw_kupo)
        ogmios_urls = split_api_keys(raw_ogmios)
        kupo_api_keys = split_api_keys(merged("kupo_api_key") or "")
        ogmios_api_keys = split_api_keys(merged("ogmios_api_key") or "")

        # Zip or pad: pair kupo_url[i] ↔ ogmios_url[i] ↔ kupo_api_key[i] ↔ ogmios_api_key[i]
        n = max(len(kupo_urls), len(ogmios_urls), len(kupo_api_keys), len(ogmios_api_keys), 1)
        while len(kupo_urls) < n:
            kupo_urls.append(kupo_urls[0])
        while len(ogmios_urls) < n:
            ogmios_urls.append(ogmios_urls[0])
        while len(kupo_api_keys) < n:
            kupo_api_keys.append(kupo_api_keys[0] if kupo_api_keys else "")
        while len(ogmios_api_keys) < n:
            ogmios_api_keys.append(ogmios_api_keys[0] if ogmios_api_keys else "")

        if n <= 1:
            return KupmiosProvider(
                kupo_url=kupo_urls[0],
                ogmios_url=ogmios_urls[0],
                kupo_api_key=kupo_api_keys[0],
                ogmios_api_key=ogmios_api_keys[0],
            )

        instances: list[tuple[str, Provider]] = []
        for i in range(n):
            instances.append((
                f"kupo-{i}",
                KupmiosProvider(
                    kupo_url=kupo_urls[i],
                    ogmios_url=ogmios_urls[i],
                    kupo_api_key=kupo_api_keys[i],
                    ogmios_api_key=ogmios_api_keys[i],
                ),
            ))
        _LOGGER.info(
            "Kupmios: created %d instances for rotating-key provider",
            n,
        )
        return RotatingKeyProvider(instances)

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
