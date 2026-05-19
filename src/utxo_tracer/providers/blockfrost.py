"""Blockfrost provider."""

from __future__ import annotations

from typing import Optional

import httpx

from ..models import Asset, OutRef, UTxONode
from ..utils import parse_blockfrost_unit
from .base import Provider


class BlockfrostProvider(Provider):
    provider_type = "blockfrost"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://cardano-mainnet.blockfrost.io/api/v0",
        auth_type: str = "project_id",
        endpoint_url: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.auth_type = auth_type
        self.endpoint_url = endpoint_url

        headers: dict[str, str] = {"Accept": "application/json"}
        if auth_type == "project_id":
            if api_key:
                headers["project_id"] = api_key
        elif auth_type == "bearer":
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        elif auth_type == "dmtr-api-key":
            if api_key:
                headers["dmtr-api-key"] = api_key
            if endpoint_url:
                headers["x-blockfrost-endpoint"] = endpoint_url
        else:
            raise ValueError(f"Unknown blockfrost auth_type: {auth_type}")

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health_check(self) -> bool:
        try:
            r = await self._client.get("/blocks/latest")
            return r.status_code == 200
        except Exception:
            return False

    def _parse_assets(self, amount: list[dict]) -> list[Asset]:
        assets: list[Asset] = []
        for a in amount:
            unit = a.get("unit", "")
            qty = int(a.get("quantity") or 0)
            policy_id, asset_name = parse_blockfrost_unit(unit)
            assets.append(
                Asset(policy_id=policy_id, asset_name=asset_name, quantity=qty)
            )
        return assets

    def _parse_output(self, tx_hash: str, item: dict) -> UTxONode:
        idx = int(item.get("output_index") or 0)
        out_ref = OutRef(tx_hash=tx_hash, output_index=idx)
        assets = self._parse_assets(item.get("amount", []))
        inline_datum = item.get("inline_datum")
        return UTxONode(
            id=out_ref.node_id(),
            out_ref=out_ref,
            address=item.get("address", ""),
            assets=assets,
            datum_hash=item.get("data_hash"),
            inline_datum=inline_datum,
            script_ref=item.get("reference_script_hash"),
        )

    async def get_utxo_by_out_ref(self, out_ref: OutRef) -> Optional[UTxONode]:
        try:
            r = await self._client.get(f"/txs/{out_ref.tx_hash}/utxos")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            if e.response.status_code == 429:
                raise  # let fallback handle rate-limit
            raise
        outputs = data.get("outputs", [])
        for o in outputs:
            if int(o.get("output_index", -1)) == out_ref.output_index:
                return self._parse_output(out_ref.tx_hash, o)
        return None

    async def get_transaction_utxos(self, tx_hash: str) -> dict:
        try:
            r = await self._client.get(f"/txs/{tx_hash}/utxos")
            if r.status_code == 404:
                return {"inputs": [], "outputs": []}
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                raise  # let fallback handle rate-limit
            raise

        inputs: list[OutRef] = []
        input_utxos: dict[str, UTxONode] = {}
        for i in data.get("inputs", []):
            # Skip collateral/reference inputs if marked
            if i.get("collateral") is True or i.get("reference") is True:
                continue
            in_tx = i.get("tx_hash", "")
            idx = int(i.get("output_index") or 0)
            if in_tx:
                out_ref = OutRef(tx_hash=in_tx, output_index=idx)
                inputs.append(out_ref)
                # API already returns address + amount per input —
                # parse full UTxONode to avoid a separate API call later
                input_utxo = self._parse_output(in_tx, i)
                input_utxos[out_ref.node_id()] = input_utxo

        outputs: list[UTxONode] = []
        for o in data.get("outputs", []):
            outputs.append(self._parse_output(tx_hash, o))

        return {"inputs": inputs, "input_utxos": input_utxos, "outputs": outputs}

    async def get_tx_block_time(self, tx_hash: str) -> int | None:
        """Fetch block time for a transaction from Blockfrost."""
        try:
            r = await self._client.get(f"/txs/{tx_hash}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            return data.get("block_time")
        except Exception:
            return None

    async def get_address_transactions(self, address: str) -> list[str]:
        """Return all transaction hashes involving this address via Blockfrost.

        Paginates through all pages to get complete transaction history.
        """
        all_hashes: set[str] = set()
        page = 1
        page_size = 100
        max_pages = 500  # safety limit (50K tx — should be enough for any address)
        try:
            while page <= max_pages:
                r = await self._client.get(
                    f"/addresses/{address}/transactions",
                    params={"order": "desc", "count": page_size, "page": page},
                )
                if r.status_code == 404:
                    break
                r.raise_for_status()
                txs = r.json()
                if not txs:
                    break
                new_hashes = {tx.get("tx_hash", "") for tx in txs if tx.get("tx_hash")}
                if not new_hashes:
                    break
                all_hashes.update(new_hashes)
                # If returned fewer than page_size, we're on the last page
                if len(txs) < page_size:
                    break
                page += 1
            return list(all_hashes)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                raise  # let fallback handle rate-limit
            return list(all_hashes) if all_hashes else []
        except Exception:
            return list(all_hashes) if all_hashes else []

    async def get_spent_utxos(self, address: str) -> list[OutRef]:
        """Find transactions that spent UTXOs from this address (forward tracing).

        Uses Blockfrost /addresses/{address}/transactions to find all TXs
        involving the address, then checks which ones have this address as input.

        Returns list of OutRefs for the spent outputs.
        """
        try:
            r = await self._client.get(
                f"/addresses/{address}/transactions",
                params={"order": "desc", "count": 100},
            )
            if r.status_code == 404:
                return []
            r.raise_for_status()
            txs = r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                raise
            return []
        except Exception:
            return []

        spent_refs: list[OutRef] = []
        seen_tx_hashes: set[str] = set()

        for tx in txs:
            tx_hash = tx.get("tx_hash", "")
            if not tx_hash or tx_hash in seen_tx_hashes:
                continue
            seen_tx_hashes.add(tx_hash)
            try:
                tx_data = await self.get_transaction_utxos(tx_hash)
                for inp in tx_data.get("inputs", []):
                    if inp.tx_hash:  # valid OutRef
                        spent_refs.append(inp)
            except Exception:
                continue

        return spent_refs
