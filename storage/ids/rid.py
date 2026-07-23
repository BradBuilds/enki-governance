"""
Compact RID Encoding for PostgreSQL IDENTITY sequences.

This module provides compact alphanumeric encoding for 64-bit integers using
a 55-character alphabet (base-55). This is distinct from the semantic Base-60
library used for assertion depth visualization.
"""

# Compact alphabet: 55 characters (excludes confusables: 0, O, I, l, 1)
ALPHABET = "23456789abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
BASE = len(ALPHABET)  # 55


def encode_rid(rid_int: int) -> str:
    """
    Encode PostgreSQL IDENTITY integer as compact alphanumeric string (base-55).
    
    This is a simple radix conversion using a 55-character alphabet that excludes
    visually confusable characters. This is NOT the semantic Base-60 system used
    for assertion depth visualization.
    
    Args:
        rid_int: PostgreSQL sequence value (BIGINT >= 0)
    
    Returns:
        Compact RID string (e.g., 0->"2", 54->"Z", 55->"32")
        
    Raises:
        ValueError: If rid_int is negative
    """
    if rid_int < 0:
        raise ValueError(f"RID must be non-negative, got {rid_int}")
    
    if rid_int == 0:
        return ALPHABET[0]
    
    result = []
    while rid_int:
        rid_int, remainder = divmod(rid_int, BASE)
        result.append(ALPHABET[remainder])
    
    return ''.join(reversed(result))


def decode_rid(rid_str: str) -> int:
    """
    Decode compact RID string to integer.
    
    Args:
        rid_str: Compact RID string
    
    Returns:
        Integer value
        
    Raises:
        ValueError: If rid_str contains invalid characters
    """
    if not rid_str:
        raise ValueError("RID string cannot be empty")
    
    result = 0
    for char in rid_str:
        if char not in ALPHABET:
            raise ValueError(f"Invalid character '{char}' in RID string")
        result = result * BASE + ALPHABET.index(char)
    
    return result


# =============================================================================
# LEGACY ALIASES (backward compatibility)
# =============================================================================

def rid_to_base60(rid_int: int) -> str:
    """
    LEGACY ALIAS for encode_rid().
    
    Note: Despite the name, this uses base-55, not base-60. The name "base60"
    is a historical artifact. New code should use encode_rid().
    """
    return encode_rid(rid_int)


def base60_to_int(rid_str: str) -> int:
    """
    LEGACY ALIAS for decode_rid().
    
    Note: Despite the name, this decodes base-55, not base-60. New code should
    use decode_rid().
    """
    return decode_rid(rid_str)


def int_to_base60(value: int) -> str:
    """
    LEGACY ALIAS for encode_rid().
    
    Note: Despite the name, this uses base-55, not base-60. New code should
    use encode_rid().
    """
    return encode_rid(value)

