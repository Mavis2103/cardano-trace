"""Cross-CEX flow tracking — trace funds flowing through multiple exchanges.

Key scenario: Hacker receives funds from CEX_A (withdrawal), then deposits
them to CEX_B (deposit). We can:

1. Backward trace from hacker UTXO → find CEX_A withdrawal (cash-in source)
2. Forward trace from hacker UTXO → find CEX_B deposit (cash-out destination)
3. Use cashflow reconciliation to CONFIRM both directions with CEX API data

This gives a complete fund flow: CEX_A → Hacker → CEX_B
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ...models import OutRef, TraceResult, UTxONode
from ..models import CashflowMatch, CashflowSummary, CexRecord, OnChainRecord

_LOGGER = logging.getLogger(__name__)


@dataclass
class CrossCexFlow:
    """A complete fund flow detected across CEX boundaries."""

    inflow_cex: str               # Source CEX (e.g. "binance")
    inflow_amount: float          # Amount received from CEX_A
    inflow_tx_hash: str           # On-chain tx from CEX_A → hacker
    inflow_confidence: float

    outflow_cex: str              # Destination CEX (e.g. "kucoin")
    outflow_amount: float         # Amount sent to CEX_B
    outflow_tx_hash: str          # On-chain tx hacker → CEX_B
    outflow_confidence: float

    hacker_address: str           # Hacker's intermediary address
    time_delta_seconds: int       # Time between inflow and outflow
    intermediate_addresses: list[str]  # Any intermediate hops


def detect_cross_cex_flows(
    backward_trace: Optional[TraceResult],
    forward_trace: Optional[TraceResult],
    cashflow_summary: CashflowSummary,
) -> list[CrossCexFlow]:
    """Detect cross-CEX fund flows by combining backward + forward traces
    with cashflow reconciled CEX records.

    Backward trace finds: CEX_A → ... → Hacker (cash-in sources)
    Forward trace finds:  Hacker → ... → CEX_B (cash-out destinations)
    Cashflow confirms:    Actual CEX API records for both sides
    """
    # Build lookup: address → CEX name from cashflow matches
    cex_withdrawal_addrs: dict[str, str] = {}
    cex_deposit_addrs: dict[str, str] = {}
    cex_withdrawal_hashes: dict[str, str] = {}  # address → tx_hash
    cex_deposit_hashes: dict[str, str] = {}
    cex_withdrawal_times: dict[str, int] = {}  # address → CEX timestamp
    cex_deposit_times: dict[str, int] = {}

    for m in cashflow_summary.matches:
        if m.cex_record.is_withdrawal:
            for oc in m.onchain_records:
                cex_withdrawal_addrs[oc.address] = m.cex_record.exchange
                cex_withdrawal_hashes[oc.address] = oc.tx_hash
                cex_withdrawal_times[oc.address] = m.cex_record.timestamp
        elif m.cex_record.is_deposit:
            for oc in m.onchain_records:
                cex_deposit_addrs[oc.address] = m.cex_record.exchange
                cex_deposit_hashes[oc.address] = oc.tx_hash
                cex_deposit_times[oc.address] = m.cex_record.timestamp

    # Collect all addresses from cashflow
    all_cex_source_addrs = set(cex_withdrawal_addrs.keys())
    all_cex_dest_addrs = set(cex_deposit_addrs.keys())

    # Find hacker address from trace start
    hacker_addr = ""
    if backward_trace and backward_trace.nodes:
        hacker_addr = backward_trace.nodes[0].address if backward_trace.nodes else ""
    if forward_trace and forward_trace.nodes:
        hacker_addr = hacker_addr or forward_trace.nodes[0].address

    flows: list[CrossCexFlow] = []

    # If we have both directions, pair them
    if backward_trace and forward_trace:
        # Find CEX addresses in backward trace (cash-in sources)
        found_sources: list[tuple[str, str, float, str, int]] = []  # (cex, tx_hash, amount, addr, timestamp)
        for node in backward_trace.nodes:
            addr = node.address
            if addr in cex_withdrawal_addrs:
                found_sources.append((
                    cex_withdrawal_addrs[addr],
                    cex_withdrawal_hashes.get(addr, ""),
                    node.ada,
                    addr,
                    cex_withdrawal_times.get(addr, 0),
                ))

        # Find CEX addresses in forward trace (cash-out destinations)
        found_dests: list[tuple[str, str, float, str, int]] = []
        for node in forward_trace.nodes:
            addr = node.address
            if addr in cex_deposit_addrs:
                found_dests.append((
                    cex_deposit_addrs[addr],
                    cex_deposit_hashes.get(addr, ""),
                    node.ada,
                    addr,
                    cex_deposit_times.get(addr, 0),
                ))

        # Pair sources and dests when they connect through the hacker
        for src_cex, src_tx, src_amt, src_addr, src_ts in found_sources:
            for dst_cex, dst_tx, dst_amt, dst_addr, dst_ts in found_dests:
                if abs(src_amt - dst_amt) / max(src_amt, 0.001) < 0.5:
                    time_delta = abs(dst_ts - src_ts) if src_ts and dst_ts else 0
                    flows.append(CrossCexFlow(
                        inflow_cex=src_cex,
                        inflow_amount=src_amt,
                        inflow_tx_hash=src_tx,
                        inflow_confidence=0.85,
                        outflow_cex=dst_cex,
                        outflow_amount=dst_amt,
                        outflow_tx_hash=dst_tx,
                        outflow_confidence=0.85,
                        hacker_address=hacker_addr if hacker_addr else src_addr,
                        time_delta_seconds=time_delta,
                        intermediate_addresses=[],
                    ))

    # Also detect from cashflow alone (CEX_A withdrawal + CEX_B deposit same time window)
    inflow_by_cex: dict[str, float] = {}
    outflow_by_cex: dict[str, float] = {}
    for m in cashflow_summary.matches:
        if m.cex_record.is_withdrawal:
            ex = m.cex_record.exchange
            inflow_by_cex[ex] = inflow_by_cex.get(ex, 0) + m.cex_record.amount
        elif m.cex_record.is_deposit:
            ex = m.cex_record.exchange
            outflow_by_cex[ex] = outflow_by_cex.get(ex, 0) + m.cex_record.amount

    # If we see funds leaving one CEX and arriving at another, note it
    for src_cex, out_amt in outflow_by_cex.items():
        for dst_cex, in_amt in inflow_by_cex.items():
            if src_cex != dst_cex and abs(out_amt - in_amt) / max(out_amt, 0.001) < 0.3:
                already = any(f.inflow_cex == src_cex and f.outflow_cex == dst_cex for f in flows)
                if not already:
                    amounts_close = f"amounts close ({out_amt:.0f} vs {in_amt:.0f} ADA)"
                    flows.append(CrossCexFlow(
                        inflow_cex=dst_cex,  # dst receives
                        inflow_amount=in_amt,
                        inflow_tx_hash="",
                        inflow_confidence=0.4,
                        outflow_cex=src_cex,  # src sends
                        outflow_amount=out_amt,
                        outflow_tx_hash="",
                        outflow_confidence=0.4,
                        hacker_address="",
                        time_delta_seconds=0,
                        intermediate_addresses=[],
                    ))

    return flows
