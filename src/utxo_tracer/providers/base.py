"""Abstract provider base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..models import OutRef, UTxONode


class Provider(ABC):
    """Abstract base class for UTXO data providers."""

    provider_type: str = "base"

    # True only for providers with a REAL multi-tx batch endpoint (e.g. Koios
    # /tx_info). When False, ``get_transactions_utxos`` is just a serial loop, so
    # address tracing must use its per-tx concurrent path to stream progress
    # smoothly instead of stalling on blocking 4-tx "batches".
    supports_batch_tx_fetch: bool = False

    # True for providers that implement ``get_address_spend_map`` (forward
    # tracing). The forward tracer gates on this capability, NOT on
    # ``provider_type`` — so wrapper providers (rotating/fallback) report the
    # capability of what they wrap instead of being wrongly excluded.
    supports_forward: bool = False

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if provider is reachable and authorized."""

    @abstractmethod
    async def get_utxo_by_out_ref(self, out_ref: "OutRef") -> Optional["UTxONode"]:
        """Resolve a single UTXO by its output reference."""

    @abstractmethod
    async def get_transaction_utxos(self, tx_hash: str) -> dict:
        """Return {'inputs': list[OutRef], 'outputs': list[UTxONode]}."""

    async def get_spent_utxos(self, address: str) -> list["OutRef"]:
        raise NotImplementedError(
            f"{self.provider_type} does not support forward tracing"
        )

    async def get_address_spend_map(self, address: str) -> dict[str, str]:
        """Return a UTXO-precise spend map for forward tracing.

        Maps every *consumed* input ``node_id`` (``tx_hash:index``) seen at this
        address to the ``tx_hash`` of the transaction that spent it. A UTXO that
        is absent from the map is unspent (a terminal node for forward tracing).

        This replaces the ambiguous ``get_spent_utxos`` semantics: forward
        tracing must follow the single transaction that consumed a specific
        UTXO, not every transaction that ever touched the address.
        """
        raise NotImplementedError(
            f"{self.provider_type} does not support forward tracing"
        )

    async def get_address_transactions(self, address: str) -> list[str]:
        """Return all transaction hashes involving this address.

        The address may appear as input, output, or both.
        Override in providers that support it (Blockfrost, Koios, Kupmios).
        """
        raise NotImplementedError(
            f"{self.provider_type} does not support address-tx lookup"
        )

    async def get_transactions_utxos(
        self, tx_hashes: list[str]
    ) -> list[dict]:
        """Batch-fetch UTXO details for multiple transactions.

        Default implementation falls back to sequential
        ``get_transaction_utxos`` calls. Providers with batch APIs (Koios)
        should override this for better performance.
        """
        results: list[dict] = []
        for tx_hash in tx_hashes:
            results.append(await self.get_transaction_utxos(tx_hash))
        return results

    async def get_tx_block_time(self, tx_hash: str) -> int | None:
        """Return the block time (unix epoch) for a transaction, or None.
        
        Override in providers that support it (Blockfrost, Koios)."""
        return None

    async def aclose(self) -> None:
        """Override to release HTTP clients."""
        return None

    async def __aenter__(self) -> "Provider":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()


class CapabilityError(Exception):
    """Raised when a provider lacks a required capability (e.g. no DumpHistory)."""

    def __init__(self, provider_name: str, reason: str = "") -> None:
        self.provider_name = provider_name
        self.reason = reason
        super().__init__(f"{provider_name}: {reason}" if reason else provider_name)
