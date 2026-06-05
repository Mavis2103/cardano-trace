"""Dataclass models for UTXO tracer."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class OutRef:
    tx_hash: str
    output_index: int

    def node_id(self) -> str:
        return f"{self.tx_hash}:{self.output_index}"

    def __str__(self) -> str:
        return f"{self.tx_hash}#{self.output_index}"

    def __hash__(self) -> int:
        return hash(self.node_id())

    def __eq__(self, other: object) -> bool:
        return isinstance(other, OutRef) and self.node_id() == other.node_id()


@dataclass
class Asset:
    policy_id: str  # empty string = lovelace
    asset_name: str
    quantity: int  # Python int = arbitrary precision

    @property
    def is_lovelace(self) -> bool:
        return self.policy_id == ""

    @property
    def unit(self) -> str:
        return "lovelace" if self.is_lovelace else f"{self.policy_id}.{self.asset_name}"


@dataclass
class UTxONode:
    id: str
    out_ref: OutRef
    address: str
    assets: list[Asset]
    datum_hash: Optional[str] = None
    inline_datum: Optional[Any] = None
    script_ref: Optional[str] = None

    # Internal cache for address_type — computed once on first access
    _address_type_cache: Optional[str] = None

    @property
    def lovelace(self) -> int:
        for a in self.assets:
            if a.is_lovelace:
                return a.quantity
        return 0

    @property
    def ada(self) -> float:
        return self.lovelace / 1_000_000

    @property
    def address_type(self) -> str:
        """Classify the address as 'wallet', 'script', 'byron', 'stake', or 'unknown'.

        Result is memoized on the node so ``classify_address()`` is only
        called once per node instance.
        """
        if self._address_type_cache is None:
            from .utils import classify_address
            object.__setattr__(self, '_address_type_cache', classify_address(self.address).value)
        return self._address_type_cache  # type: ignore[return-value]


@dataclass
class TransactionEdge:
    id: str
    source: str
    target: str
    direction: str  # 'input' | 'output'
    fee: Optional[int] = None
    tx_hash: Optional[str] = None


@dataclass
class TraceStep:
    out_ref: OutRef
    direction: str
    depth: int
    utxo: Optional[UTxONode] = None
    error: Optional[str] = None
    visited_at: float = field(default_factory=time.time)
    parent_out_ref: Optional[OutRef] = None


@dataclass
class AddressInteractionNode:
    """An address node in the address-interaction graph."""
    address: str
    address_type: str = "unknown"
    total_ada: float = 0.0
    net_ada: float = 0.0
    total_incoming_ada: float = 0.0
    total_outgoing_ada: float = 0.0
    tx_count: int = 0
    is_cex: bool = False
    cex_name: str = ""
    is_target: bool = False
    depth: int = 0  # hop distance from target address
    # Non-CEX wallet that directly sent to / received from a registered CEX
    # address. Holds the CEX label, e.g. "Binance User". Empty otherwise.
    cex_user: str = ""


@dataclass
class AddressInteractionEdge:
    """Connection between two addresses through a shared transaction.

    direction_relative_to_target:
        ``"incoming"`` — target received funds from source
        ``"outgoing"`` — target sent funds to source
        ``"both"`` — transactions exist in both directions
        ``"unknown"`` — direction could not be determined
    """
    source: str
    target: str
    tx_hashes: list[str] = field(default_factory=list)
    interaction_count: int = 0
    direction_relative_to_target: str = "unknown"
    source_depth: int = 0  # depth at which this edge was discovered


@dataclass
class AddressTraceResult:
    """Result of an address-interaction trace."""
    target_address: str
    addresses: list[AddressInteractionNode]
    edges: list[AddressInteractionEdge]
    total_transactions: int = 0
    error: Optional[str] = None
    provider_name: str = ""
    max_depth: int = 1
    direction: str = "both"  # backward | forward | both (flow relative to target)


@dataclass
class CexInfo:
    name: str
    type: str = "exchange"
    confidence: str = "high"


@dataclass
class TraceResult:
    nodes: list[UTxONode]
    edges: list[TransactionEdge]
    traced_path: list[str]
    start_out_ref: OutRef
    direction: str
    max_depth: int
    cex_findings: list[dict] = field(default_factory=list)
    error: Optional[str] = None
    errors_count: int = 0
    steps: list[TraceStep] = field(default_factory=list)
    provider_name: str = ""
