"""
Signal ID generation per FD-6 (node-aware, underscore-separated).

All IDs are self-describing: you can extract the node origin from any ID
without a database lookup.

Formats:
    signal_id:  sig_<node_short>_<timestamp_hex>_<random_hex>
    ldu_t_id:   ldu_t_<node_short>_<timestamp_hex>_<random_hex>
    ldu_p_id:   ldu_p_<node_short>_<timestamp_hex>_<random_hex>
    receipt_id: rx_<node_short>_<sequence>

node_short = first 8 hex chars from node_id (the "enki_XXXXXXXX" part)
timestamp_hex = 8-char hex of unix timestamp (seconds)
random_hex = 4-char hex of random bytes (collision avoidance)
"""

import os
import time


def _node_short(node_id: str) -> str:
    """Extract the 8-char hex portion from a node_id like 'enki_a3f7b2c1'."""
    if node_id.startswith("enki_"):
        return node_id[5:]
    return node_id[:8]


def _ts_hex() -> str:
    """Current unix timestamp as 8-char hex."""
    return format(int(time.time()), "08x")


def _rand_hex(n: int = 4) -> str:
    """Random hex string of n bytes (2n chars)."""
    return os.urandom(n).hex()[:2 * n]


def generate_signal_id(node_id: str) -> str:
    """Generate a signal_id: sig_<node>_<ts>_<rand>"""
    return f"sig_{_node_short(node_id)}_{_ts_hex()}_{_rand_hex(4)}"


def generate_ldu_t_id(node_id: str) -> str:
    """Generate an ldu_t_id: ldu_t_<node>_<ts>_<rand>"""
    return f"ldu_t_{_node_short(node_id)}_{_ts_hex()}_{_rand_hex(4)}"


def generate_ldu_p_id(node_id: str) -> str:
    """Generate an ldu_p_id: ldu_p_<node>_<ts>_<rand>"""
    return f"ldu_p_{_node_short(node_id)}_{_ts_hex()}_{_rand_hex(4)}"


def generate_receipt_id(node_id: str, sequence: int) -> str:
    """Generate a receipt_id: rx_<node>_<sequence>"""
    return f"rx_{_node_short(node_id)}_{sequence}"


def extract_node_from_id(entity_id: str) -> str:
    """Extract the node_short from any Enki ID.
    sig_a3f7b2c1_... → a3f7b2c1
    ldu_t_a3f7b2c1_... → a3f7b2c1
    rx_a3f7b2c1_... → a3f7b2c1
    """
    parts = entity_id.split("_")
    if parts[0] == "ldu" and len(parts) >= 3:
        # ldu_t_<node>_... or ldu_p_<node>_...
        return parts[2]
    if len(parts) >= 2:
        # sig_<node>_... or rx_<node>_...
        return parts[1]
    return ""
