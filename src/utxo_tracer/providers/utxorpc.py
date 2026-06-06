"""UTxORPC provider — native gRPC client using utxorpc SDK v0.2+.

Uses CardanoQueryClient / CardanoSyncClient from the SDK
(https://github.com/utxorpc/python-sdk) for proper BigInt handling
and connection management.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

from ..models import Asset, OutRef, UTxONode
from .base import CapabilityError, Provider

logger = logging.getLogger(__name__)

MAX_OUTPUT_PROBE = 50
MAX_CACHE_SIZE = 1000


class UTxORPCProvider(Provider):
    """UTxORPC gRPC provider via python-sdk.

    Connects to a UTxORPC endpoint (Demeter.run, self-hosted, etc.)
    using CardanoQueryClient for UTXO queries.

    URL & API key from config.json / .env — no hardcoded values.
    """

    provider_type = "utxorpc"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        endpoint_url: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self._timeout = timeout

        # Build URI for SDK — endpoint_url takes priority but must be valid.
        # Scheme decides transport: http:// (or grpc://) → plaintext insecure
        # channel (self-hosted Dolos on :50051), https:// → TLS secure channel
        # (Demeter.run et al.). No scheme defaults to https/TLS.
        raw_url = endpoint_url or base_url or ""
        if not raw_url:
            self._uri = ""
            self._secure = True
        else:
            if "://" not in raw_url:
                raw_url = "https://" + raw_url
            parsed = urlparse(raw_url)
            scheme = (parsed.scheme or "https").lower()
            self._secure = scheme in ("https", "grpcs")
            host = parsed.hostname or ""
            port = parsed.port or (443 if self._secure else 80)
            self._uri = f"{host}:{port}"

        # SDK clients (lazy)
        self._query_client = None
        self._sync_client = None
        self._init_lock: asyncio.Lock | None = None

        # Transaction cache
        self._tx_cache: dict[str, dict] = {}

    # ── SDK client helpers ────────────────────────────────────────────

    def _metadata(self) -> dict:
        """Return metadata dict for SDK clients."""
        md = {}
        if self.api_key:
            if "demeter" in self._uri:
                md["dmtr-api-key"] = self.api_key
            else:
                md["x-api-key"] = self.api_key
        return md

    def _bigint_value(self, bi) -> int:
        """Extract int from BigInt message."""
        if bi.HasField("int"):
            return bi.int
        if bi.HasField("big_u_int"):
            return int.from_bytes(bi.big_u_int, "big")
        if bi.HasField("big_n_int"):
            return int.from_bytes(bi.big_n_int, "big", signed=True)
        return 0

    async def _get_query_client(self):
        """Get or create a connected CardanoQueryClient."""
        if self._query_client is not None:
            return self._query_client

        if self._init_lock is None:
            self._init_lock = asyncio.Lock()

        async with self._init_lock:
            if self._query_client is not None:
                return self._query_client

            try:
                from utxorpc.query import CardanoQueryClient
            except ImportError as e:
                raise CapabilityError(
                    self.provider_type,
                    f"SDK not available: {e}",
                )

            self._query_client = CardanoQueryClient(
                uri=self._uri,
                metadata=self._metadata(),
                secure=self._secure,
            )
            self._qc_cm = self._query_client.async_connect()
            await self._qc_cm.__aenter__()
            return self._query_client

    async def _get_sync_client(self):
        """Get or create a connected CardanoSyncClient."""
        if self._sync_client is not None:
            return self._sync_client

        if self._init_lock is None:
            self._init_lock = asyncio.Lock()

        async with self._init_lock:
            if self._sync_client is not None:
                return self._sync_client

            try:
                from utxorpc.sync import CardanoSyncClient
            except ImportError as e:
                raise CapabilityError(
                    self.provider_type,
                    f"SDK not available: {e}",
                )

            self._sync_client = CardanoSyncClient(
                uri=self._uri,
                metadata=self._metadata(),
                secure=self._secure,
            )
            self._sc_cm = self._sync_client.async_connect()
            await self._sc_cm.__aenter__()
            return self._sync_client

    async def aclose(self) -> None:
        """Close SDK clients with timeout."""
        for cm in (getattr(self, "_qc_cm", None), getattr(self, "_sc_cm", None)):
            if cm is not None:
                try:
                    await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=5.0)
                except (RuntimeError, asyncio.TimeoutError):
                    pass  # generator already stopped or timed out
        self._query_client = None
        self._sync_client = None

    # ── Health check ──────────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            qc = await self._get_query_client()
            await qc.async_read_params()
            return True
        except Exception as e:
            logger.debug("UTxORPC health_check failed: %s", e)
            return False

    # ── Single UTXO lookup ────────────────────────────────────────────

    async def get_utxo_by_out_ref(self, out_ref: OutRef) -> Optional[UTxONode]:
        from utxorpc_spec.utxorpc.v1alpha.query.query_pb2 import TxoRef

        qc = await self._get_query_client()
        tx_hash_bytes = bytes.fromhex(out_ref.tx_hash)
        ref = TxoRef(hash=tx_hash_bytes, index=out_ref.output_index)

        try:
            resp = await qc.async_read_utxos(keys=[ref])
        except Exception as e:
            logger.debug("get_utxo_by_out_ref failed: %s", e)
            return None

        if not resp.items:
            return None
        return self._parse_utxo_data(out_ref.tx_hash, resp.items[0])

    # ── Transaction lookup ────────────────────────────────────────────

    async def get_transaction_utxos(self, tx_hash: str) -> dict:
        if tx_hash in self._tx_cache:
            return self._tx_cache[tx_hash]

        qc = await self._get_query_client()
        outputs = await self._probe_outputs(qc, tx_hash)

        if not outputs:
            return {"inputs": [], "outputs": []}

        # DumpHistory may be UNIMPLEMENTED on some servers (e.g. Demeter.run)
        inputs = await self._scan_for_tx_inputs(tx_hash)

        # If we got outputs but no inputs, this provider can't trace backward
        # — raise CapabilityError so fallback chain can try the next provider
        if not inputs and outputs:
            raise CapabilityError(
                self.provider_type,
                "DumpHistory not available — can't trace backward",
            )

        result = {"inputs": inputs, "outputs": outputs}
        self._tx_cache[tx_hash] = result
        if len(self._tx_cache) > MAX_CACHE_SIZE:
            oldest_key = next(iter(self._tx_cache))
            del self._tx_cache[oldest_key]
        return result

    async def _probe_outputs(self, qc, tx_hash: str) -> list:
        from utxorpc_spec.utxorpc.v1alpha.query.query_pb2 import TxoRef

        tx_hash_bytes = bytes.fromhex(tx_hash)
        keys = [
            TxoRef(hash=tx_hash_bytes, index=idx)
            for idx in range(MAX_OUTPUT_PROBE)
        ]

        try:
            resp = await asyncio.wait_for(
                qc.async_read_utxos(keys=keys),
                timeout=self._timeout,
            )
        except Exception as e:
            logger.debug("_probe_outputs failed: %s", e)
            return []

        items = sorted(
            (it for it in resp.items if it.HasField("txo_ref")),
            key=lambda it: it.txo_ref.index,
        )
        outputs = []
        for item in items:
            node = self._parse_utxo_data(tx_hash, item)
            if node is None:
                break
            outputs.append(node)
        return outputs

    async def _scan_for_tx_inputs(self, tx_hash: str) -> list:
        """Scan blocks via DumpHistory to find tx inputs (SDK method)."""
        try:
            sc = await self._get_sync_client()
        except Exception as e:
            logger.debug("_scan_for_tx_inputs: sync client unavailable: %s", e)
            return []

        target_bytes = bytes.fromhex(tx_hash)

        # Get chain tip via SDK's async_read_tip
        try:
            tip = await sc.async_read_tip()
            if tip is None:
                return []
            slot_cursor = tip.slot
            hash_cursor = tip.hash
        except Exception as e:
            logger.debug("Failed to get chain tip via async_read_tip: %s", e)
            return []

        from utxorpc.sync import CardanoPoint

        BATCH_SIZE = 100
        MAX_BATCHES = 5
        inputs: list = []

        for _ in range(MAX_BATCHES):
            if slot_cursor <= 0:
                break

            try:
                blocks = await asyncio.wait_for(
                    sc.async_dump_history(
                        start=CardanoPoint(slot=slot_cursor, hash=hash_cursor),
                        max_items=BATCH_SIZE,
                    ),
                    timeout=self._timeout,
                )
            except Exception as e:
                logger.debug("DumpHistory (SDK) failed: %s", e)
                break

            if not blocks:
                break

            found = False
            min_slot_seen = None

            for block in blocks:
                if block is None:
                    continue
                if not block.HasField("header"):
                    continue
                slot = block.header.slot
                if min_slot_seen is None or slot < min_slot_seen:
                    min_slot_seen = slot

                for tx in block.body.tx if block.body else []:
                    tx_hash_hex = tx.hash.hex() if tx.hash else None
                    tx_inputs = [
                        OutRef(
                            tx_hash=inp.tx_hash.hex(),
                            output_index=inp.output_index,
                        )
                        for inp in tx.inputs
                        if inp.tx_hash
                    ]
                    tx_outputs = [
                        node
                        for idx, out in enumerate(tx.outputs)
                        if (node := self._parse_tx_output(tx_hash_hex or "", out, idx))
                        is not None
                    ]
                    entry = {"inputs": tx_inputs, "outputs": tx_outputs}
                    if tx_hash_hex is not None:
                        self._tx_cache[tx_hash_hex] = entry
                        if len(self._tx_cache) > MAX_CACHE_SIZE:
                            oldest_key = next(iter(self._tx_cache))
                            del self._tx_cache[oldest_key]
                    if tx_hash_hex == tx_hash:
                        inputs = tx_inputs
                        found = True
                if found:
                    break

            if found:
                break
            if min_slot_seen is not None and min_slot_seen > 0:
                slot_cursor = max(0, min_slot_seen - 1)
            else:
                break

        return inputs

    # ── Forward tracing ───────────────────────────────────────────────

    async def get_spent_utxos(self, address: str) -> list:
        try:
            from utxorpc_spec.utxorpc.v1alpha.query import query_pb2 as q_pb2
        except ImportError as e:
            raise NotImplementedError(
                f"UTxORPC SDK import failed: {e}"
            ) from e

        qc = await self._get_query_client()

        try:
            addr_bytes = bytes.fromhex(address)
        except ValueError:
            addr_bytes = address.encode("utf-8")

        predicate = q_pb2.UtxoPredicate()
        pattern = predicate.match.cardano
        pattern.address.exact_address = addr_bytes

        try:
            async for resp in qc.async_search_utxos(predicate=predicate):
                refs = []
                for item in resp.items:
                    if item.txo_ref:
                        refs.append(
                            OutRef(
                                tx_hash=item.txo_ref.hash.hex(),
                                output_index=item.txo_ref.index,
                            )
                        )
                return refs
        except Exception as e:
            logger.debug("get_spent_utxos failed: %s", e)
            return []

    async def get_address_transactions(self, address: str) -> list[str]:
        """Return all transaction hashes involving this address via UTxORPC.

        NOTE: async_search_utxos only finds the CURRENT UTXO set — it only
        returns transactions where this address is a *receiver* (output owner),
        completely missing transactions where this address is a spender (input).

        Until DumpHistory-based block range scanning is implemented, raise
        NotImplementedError so the fallback chain picks a provider with
        proper address history (Koios, Blockfrost, etc.).
        """
        raise NotImplementedError(
            "UtxoRPC get_address_transactions only finds current UTXOs "
            "(receiver side only); falls back to Koios/Blockfrost for "
            "complete address transaction history"
        )

    # ── Parsing helpers ───────────────────────────────────────────────

    def _parse_tx_output(self, tx_hash: str, out, idx: int) -> Optional[UTxONode]:
        """Parse a TxOutput from SDK response into UTxONode."""
        address = out.address.hex() if out.address else ""
        lovelace = self._bigint_value(out.coin)

        assets: list = []
        for ma in out.assets:
            policy_id = ma.policy_id.hex() if ma.policy_id else ""
            for a in ma.assets:
                asset_name = a.name.hex() if a.name else ""
                qty = self._bigint_value(a.output_coin)
                if policy_id or asset_name:
                    assets.append(
                        Asset(
                            policy_id=policy_id,
                            asset_name=asset_name,
                            quantity=qty,
                        )
                    )

        datum_hash = None
        if out.HasField("datum"):
            d = out.datum
            if d.hash:
                datum_hash = d.hash.hex()

        script_ref = None
        if out.HasField("script"):
            s = out.script
            if s.plutus_v1:
                script_ref = s.plutus_v1.hex()
            elif s.plutus_v2:
                script_ref = s.plutus_v2.hex()

        ref = OutRef(tx_hash=tx_hash, output_index=idx)
        return UTxONode(
            id=ref.node_id(),
            out_ref=ref,
            address=address,
            assets=[Asset("", "", lovelace)] + assets,
            datum_hash=datum_hash,
            inline_datum=None,
            script_ref=script_ref,
        )

    def _parse_utxo_data(self, tx_hash: str, item) -> Optional[UTxONode]:
        """Parse AnyUtxoData (from ReadUtxos) into UTxONode."""
        if not item.HasField("cardano"):
            return None
        if not item.HasField("txo_ref"):
            return None
        return self._parse_tx_output(tx_hash, item.cardano, item.txo_ref.index)

    # ── Context manager support ───────────────────────────────────────

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()
