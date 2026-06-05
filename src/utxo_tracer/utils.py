"""Utility helpers."""

from __future__ import annotations

from enum import Enum

from .models import OutRef


class AddressType(str, Enum):
    """Cardano address type classification.

    Classified by the payment credential in the address header:
      - WALLET:  payment = Ed25519 key hash (controlled by private key)
      - SCRIPT:  payment = script hash (locked by smart contract logic)
      - BYRON:   legacy bootstrap address (Byron era)
      - STAKE:   reward account address (stake key or script hash)
      - UNKNOWN: unrecognised format
    """
    WALLET = "wallet"
    SCRIPT = "script"
    BYRON = "byron"
    STAKE = "stake"
    UNKNOWN = "unknown"


# Bech32 character set (standard from BIP-0173)
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def classify_address(address: str) -> AddressType:
    """Classify a Cardano address into WALLET / SCRIPT / BYRON / STAKE / UNKNOWN.

    How it works
    ------------
    Shelley addresses use Bech32 with human-readable prefix ``addr`` (mainnet)
    or ``addr_test`` (testnet).  The first byte of the decoded payload is the
    **address header** whose bits [7;4] encode the address type and bits [3;0]
    encode the network tag.

    Bit 4 of the header (value ``0x10``) distinguishes payment credentials:
        0 → PaymentKeyHash  (controlled by a private key → WALLET)
        1 → ScriptHash      (locked by script logic   → SCRIPT)

    In the Bech32 encoding, the *first 5-bit group* of the data part contains
    bits [7;3] of the header.  Bit 4 of the header maps to the second-least-
    significant bit (value 2) of this 5-bit value, which corresponds to the
    Bech32 character index.  Therefore::

        (bech32_index_of_first_data_char & 2)  →  0 = wallet, 2 = script

    See CIP-0019 ``https://cips.cardano.org/cip/CIP-0019`` for the full
    address header specification and CIP-0005 for Bech32 prefix registration.

    Examples
    --------
    Mainnet base address (payment key + stake key):
        addr1q…  → wallet  (Bech32 index 0 → 0 & 2 = 0)

    Mainnet base address (script hash + stake key):
        addr1z…  → script  (Bech32 index 2 → 2 & 2 = 2)

    Enterprise address (payment key only):
        addr1v…  → wallet  (Bech32 index 12 → 12 & 2 = 0)

    Enterprise script address:
        addr1w…  → script  (Bech32 index 14 → 14 & 2 = 2)

    Pointer address (payment key + chain pointer):
        addr1g…  → wallet  (Bech32 index 8 → 8 & 2 = 0)
    """
    if not address:
        return AddressType.UNKNOWN

    # Byron-era bootstrap addresses (CIP-0019, legacy Base58 format)
    if address.startswith(("Ae2", "DdzFF", "4")):
        return AddressType.BYRON

    # Stake / reward addresses
    if address.startswith(("stake1", "stake_test1")):
        return AddressType.STAKE

    # Shelley-era Bech32 addresses
    #   addr1<data>…     → mainnet
    #   addr_test1<data>… → testnet
    if address.startswith("addr_test1"):
        bech32_first = address[10] if len(address) > 10 else ""
    elif address.startswith("addr1"):
        bech32_first = address[5] if len(address) > 5 else ""
    else:
        return AddressType.UNKNOWN

    if not bech32_first:  # empty means the prefix was complete but no data follows
        return AddressType.UNKNOWN
    try:
        idx = _BECH32.index(bech32_first)
    except (ValueError, IndexError):
        return AddressType.UNKNOWN

    # Bit 1 of the Bech32 character index = bit 4 of the address header byte
    #   0 → PaymentKeyHash (wallet)
    #   1 → ScriptHash     (script)
    return AddressType.SCRIPT if (idx & 2) else AddressType.WALLET


def _bech32_to_bytes(address: str) -> bytes | None:
    """Decode a Cardano Shelley Bech32 address to its raw payload bytes.

    Returns ``None`` for non-Bech32 (Byron/stake/unknown) or malformed input.
    No checksum verification (we only need the payload, and inputs come from a
    provider that already validated them).
    """
    pos = address.rfind("1")
    if pos < 1:
        return None
    data_part = address[pos + 1 :]
    values: list[int] = []
    for ch in data_part:
        i = _BECH32.find(ch)
        if i == -1:
            return None
        values.append(i)
    if len(values) < 6:
        return None
    values = values[:-6]  # strip 6-char checksum
    # convert from 5-bit groups to 8-bit bytes
    acc = 0
    bits = 0
    out = bytearray()
    for v in values:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    return bytes(out)


def address_stake_key(address: str) -> str | None:
    """Return the hex stake credential of a Cardano base address, else ``None``.

    Two addresses sharing a non-None stake key belong to the same wallet — the
    standard heuristic used to recognise a user's own change addresses so they
    are not mistaken for third-party counterparties.

    Only base addresses (payment + stake, 57-byte payload) have a stake part.
    Enterprise/pointer/script-only addresses return ``None``.
    """
    if not address.startswith(("addr1", "addr_test1")):
        return None
    payload = _bech32_to_bytes(address)
    if payload is None or len(payload) < 57:
        return None
    # header(1) + payment credential(28) + stake credential(28)
    return payload[29:57].hex()


def hex_to_utf8(hex_str: str) -> str:
    """Convert hex string to UTF-8, fallback to original on failure."""
    if not hex_str:
        return hex_str
    try:
        decoded = bytes.fromhex(hex_str).decode("utf-8")
        # Reject strings with control chars (likely binary)
        if any(ord(c) < 0x20 and c not in "\t\n\r" for c in decoded):
            return hex_str
        return decoded
    except Exception:
        return hex_str


def parse_out_ref(raw: str) -> OutRef:
    """Parse 'txHash#index' format."""
    if not raw or "#" not in raw:
        raise ValueError(
            f"Invalid UTXO reference '{raw}'. Expected format: <tx_hash>#<output_index>"
        )
    parts = raw.split("#")
    if len(parts) != 2:
        raise ValueError(
            f"Invalid UTXO reference '{raw}'. Expected exactly one '#' separator"
        )
    tx_hash, idx_str = parts[0].strip(), parts[1].strip()
    if not tx_hash:
        raise ValueError(f"Empty tx_hash in '{raw}'")
    try:
        idx = int(idx_str)
    except ValueError as e:
        raise ValueError(f"Invalid output_index '{idx_str}' in '{raw}'") from e
    if idx < 0:
        raise ValueError(f"output_index must be >= 0, got {idx}")
    return OutRef(tx_hash=tx_hash, output_index=idx)


def parse_blockfrost_unit(unit: str) -> tuple[str, str]:
    """Parse Blockfrost asset unit (policyId 56 hex + assetName hex)."""
    if unit == "lovelace" or not unit:
        return "", ""
    policy_id = unit[:56]
    asset_name_hex = unit[56:]
    asset_name = hex_to_utf8(asset_name_hex) if asset_name_hex else ""
    return policy_id, asset_name


def lovelace_to_ada(lovelace: int) -> str:
    """Format lovelace as ADA string."""
    return f"{lovelace / 1_000_000:.6f} ADA"


def shorten(s: str, head: int = 12, tail: int = 8) -> str:
    """Shorten a long string for display."""
    if not s or len(s) <= head + tail + 3:
        return s
    return f"{s[:head]}...{s[-tail:]}"
