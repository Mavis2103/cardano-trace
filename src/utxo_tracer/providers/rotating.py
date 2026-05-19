"""Rotating-key provider wrapper.

Wraps any Provider and rotates API keys on HTTP 429 (rate-limit) responses.
Supports comma-separated keys from config, env, or CLI args.

Usage::

    provider = RotatingKeyProvider([
        BlockfrostProvider("key1"),
        BlockfrostProvider("key2"),
        ...
    ])

All Provider methods are proxied. On 429, the wrapper automatically
rotates to the next key and retries the request once.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

import httpx

from ..models import OutRef, UTxONode
from .base import Provider

logger = logging.getLogger(__name__)


class RotatingKeyProvider(Provider):
    """Provider wrapper that rotates API keys on 429 rate-limit responses.

    Takes one or more (key_name, Provider) pairs. On HTTP 429, rotates to
    the next key and retries the failed call once.

    The *key_name* is only used for logging — it can be an API key prefix,
    a sequential index (``key-0``, ``key-1``), or any identifier.
    """

    provider_type = "rotating"

    def __init__(
        self,
        instances: list[tuple[str, Provider]],
        max_rotations_per_call: int = 3,
    ) -> None:
        if not instances:
            raise ValueError("RotatingKeyProvider requires at least one instance")
        self._instances = instances
        self._count = len(instances)
        self._current = 0
        self._max_rotations = max_rotations_per_call
        self._lock = asyncio.Lock()

    @property
    def current_provider(self) -> str:
        """Name of the currently-active key for display/logging."""
        name, _ = self._instances[self._current]
        return f"{self._instances[self._current][1].provider_type}:{name}"

    # ── Rotate on 429 ────────────────────────────────────────────────

    async def _call(self, method: str, *args, **kwargs) -> Any:
        """Call *method* on the current provider, rotating on 429."""
        rotations = 0
        last_error: Exception | None = None
        while rotations <= self._max_rotations:
            idx = self._current
            key_name, prov = self._instances[idx]
            try:
                result = await getattr(prov, method)(*args, **kwargs)
                return result
            except httpx.HTTPStatusError as e:
                last_error = e
                if e.response.status_code == 429:
                    rotations += 1
                    logger.warning(
                        "Rate-limited (429) on %s/%s — rotating key (attempt %d/%d)",
                        key_name, method, rotations, self._max_rotations,
                    )
                    if rotations > self._max_rotations:
                        break
                    await self._rotate()
                    continue
                raise
            except Exception as e:
                last_error = e
                raise
        # All keys exhausted on 429
        raise last_error or RuntimeError("All keys rate-limited")

    async def _rotate(self) -> None:
        """Advance to the next key."""
        async with self._lock:
            self._current = (self._current + 1) % self._count
            new_name = self._instances[self._current][0]
            logger.info(
                "Rotated to key %d/%d: %s",
                self._current + 1, self._count, new_name,
            )

    # ── Provider interface ────────────────────────────────────────────

    async def health_check(self) -> bool:
        # Health check uses the first provider — no rotation
        return await self._instances[0][1].health_check()

    async def get_utxo_by_out_ref(self, out_ref: OutRef) -> Optional[UTxONode]:
        return await self._call("get_utxo_by_out_ref", out_ref)

    async def get_transaction_utxos(self, tx_hash: str) -> dict:
        return await self._call("get_transaction_utxos", tx_hash)

    async def get_spent_utxos(self, address: str) -> list[OutRef]:
        return await self._call("get_spent_utxos", address)

    async def get_address_transactions(self, address: str) -> list[str]:
        return await self._call("get_address_transactions", address)

    async def get_transactions_utxos(
        self, tx_hashes: list[str]
    ) -> list[dict]:
        return await self._call("get_transactions_utxos", tx_hashes)

    async def get_tx_block_time(self, tx_hash: str) -> int | None:
        return await self._call("get_tx_block_time", tx_hash)

    async def aclose(self) -> None:
        for _, prov in self._instances:
            try:
                await prov.aclose()
            except Exception as e:
                logger.warning("Error closing provider: %s", e)

    async def __aenter__(self) -> RotatingKeyProvider:
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()


def split_api_keys(raw: str | list[str] | None) -> list[str]:
    """Parse one or more API keys from config/env/CLI values.

    * ``None`` or empty → ``[]``
    * ``"key1"`` → ``["key1"]``
    * ``"key1,key2,key3"`` → ``["key1", "key2", "key3"]``
    * ``["key1", "key2"]`` → ``["key1", "key2"]``
    """
    if not raw:
        return []
    if isinstance(raw, list):
        keys = [k.strip() for k in raw if k and k.strip()]
        return keys
    raw_str = str(raw).strip()
    if not raw_str:
        return []
    parts = [k.strip() for k in raw_str.split(",") if k.strip()]
    return parts
