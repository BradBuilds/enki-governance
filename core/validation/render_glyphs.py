"""
ENKI Base-60 Glyph Rendering

PRIMARY IMPLEMENTATION TARGET

This module implements deterministic, stateless glyph rendering for Base-60 digits.

Core Principles:
- Numeric truth is canonical (always decimal)
- Base-60 is a representation layer
- Glyphs never affect numeric truth
- All rendering is deterministic and reproducible

Author: ENKI System
Version: 0.1.0
"""

from typing import Dict

# Glyph style definitions
GLYPH_STYLES: Dict[str, Dict[str, str]] = {
    "wedge_ascii": {
        "unit": "|",
        "ten": "<",
        "zero": "∅"
    },
    "wedge_unicode_modern": {
        "unit": "𒁹",
        "ten": "𒌋",
        "zero": "∅"
    }
}

# Default style
DEFAULT_STYLE = "wedge_ascii"


def render_digit(d: int, style: str = DEFAULT_STYLE) -> str:
    """
    Render a single Base-60 digit as glyphs.
    
    Args:
        d: Digit value (must be 0 <= d <= 59)
        style: Glyph style name (default: "wedge_ascii")
        
    Returns:
        String representation of the digit in the specified style
        
    Raises:
        ValueError: If digit is out of range or style is invalid
        
    Rules:
        - d must be 0 <= d <= 59
        - If d == 0, return zero glyph
        - Else: tens = d // 10, units = d % 10
        - Output = ten_symbol * tens + unit_symbol * units
        
    Examples:
        >>> render_digit(0, "wedge_ascii")
        '∅'
        >>> render_digit(1, "wedge_ascii")
        '|'
        >>> render_digit(10, "wedge_ascii")
        '<'
        >>> render_digit(23, "wedge_ascii")
        '<<|||'
        >>> render_digit(59, "wedge_ascii")
        '<<<<<|||||||||'
    """
    # Validate digit range
    if not isinstance(d, int):
        raise ValueError("BASE60_INVALID_TYPE: Digit must be an integer")
    
    if d < 0 or d > 59:
        raise ValueError(f"BASE60_DIGIT_RANGE: Digit must be between 0 and 59, got {d}")
    
    # Validate style
    if style not in GLYPH_STYLES:
        raise ValueError(f"INVALID_STYLE: Unknown style '{style}'. Valid styles: {list(GLYPH_STYLES.keys())}")
    
    symbols = GLYPH_STYLES[style]
    
    # Handle zero explicitly
    if d == 0:
        return symbols["zero"]
    
    # Calculate tens and units
    tens = d // 10
    units = d % 10
    
    # Render: ten_symbol * tens + unit_symbol * units
    return symbols["ten"] * tens + symbols["unit"] * units


def render_places(places: list[int], style: str = DEFAULT_STYLE, separator: str = " ") -> str:
    """
    Render multiple Base-60 place values as glyphs.
    
    Args:
        places: List of digit values (each must be 0 <= d <= 59)
        style: Glyph style name (default: "wedge_ascii")
        separator: String to join rendered digits (default: " ")
        
    Returns:
        String representation of all places joined by separator
        
    Raises:
        ValueError: If any place is invalid or list is empty
        
    Rules:
        - Each place must validate as a digit
        - Output is rendered digits joined by separator
        - No trimming or collapsing allowed
        - All places must be preserved
        
    Examples:
        >>> render_places([1, 2, 3], "wedge_ascii")
        '| << <<<'
        >>> render_places([0, 5, 10], "wedge_ascii")
        '∅ ||||| <'
    """
    # Validate places list
    if not isinstance(places, list):
        raise ValueError("BASE60_INVALID_TYPE: Places must be a list")
    
    if len(places) == 0:
        raise ValueError("BASE60_EMPTY_PLACES: Places list cannot be empty")
    
    # Check for null/None values
    if any(p is None for p in places):
        raise ValueError("BASE60_NULL_VALUE: Places cannot contain null values")
    
    # Render each digit and join with separator
    rendered_digits = []
    for i, place in enumerate(places):
        try:
            rendered = render_digit(place, style)
            rendered_digits.append(rendered)
        except ValueError as e:
            raise ValueError(f"Invalid digit at position {i}: {e}")
    
    # Join with separator (no trimming, no collapsing)
    return separator.join(rendered_digits)


def decimal_to_base60(value: int) -> list[int]:
    """
    Convert a decimal integer to Base-60 place values.
    
    Args:
        value: Decimal integer (must be >= 0)
        
    Returns:
        List of place values (leftmost is highest place)
        
    Raises:
        ValueError: If value is negative or not an integer
        
    Rules:
        - Conversion must be explicit and deterministic
        - Place values calculated right-to-left
        - Result is reversed to put highest place first
        
    Examples:
        >>> decimal_to_base60(0)
        [0]
        >>> decimal_to_base60(59)
        [59]
        >>> decimal_to_base60(60)
        [1, 0]
        >>> decimal_to_base60(3723)
        [1, 2, 3]
    """
    if not isinstance(value, int):
        raise ValueError("BASE60_INVALID_TYPE: Value must be an integer")
    
    if value < 0:
        raise ValueError("BASE60_NEGATIVE: Negative values not supported in v0.1")
    
    # Special case: zero
    if value == 0:
        return [0]
    
    # Convert to base-60
    places = []
    while value > 0:
        places.append(value % 60)
        value //= 60
    
    # Reverse to get highest place first
    return list(reversed(places))


def base60_to_decimal(places: list[int]) -> int:
    """
    Convert Base-60 place values to decimal integer.
    
    Args:
        places: List of place values (leftmost is highest place)
        
    Returns:
        Decimal integer value
        
    Raises:
        ValueError: If any place is invalid
        
    Rules:
        - Formula: sum(place[i] * 60^(n-1-i) for i in range(n))
        - Validates all places before calculation
        
    Examples:
        >>> base60_to_decimal([0])
        0
        >>> base60_to_decimal([59])
        59
        >>> base60_to_decimal([1, 0])
        60
        >>> base60_to_decimal([1, 2, 3])
        3723
    """
    if not isinstance(places, list):
        raise ValueError("BASE60_INVALID_TYPE: Places must be a list")
    
    if len(places) == 0:
        raise ValueError("BASE60_EMPTY_PLACES: Places list cannot be empty")
    
    # Validate all places
    for i, place in enumerate(places):
        if not isinstance(place, int):
            raise ValueError(f"BASE60_INVALID_TYPE: Place at position {i} must be an integer")
        if place < 0 or place > 59:
            raise ValueError(f"BASE60_DIGIT_RANGE: Place at position {i} must be between 0 and 59, got {place}")
    
    # Calculate decimal value
    n = len(places)
    total = 0
    for i, place in enumerate(places):
        power = n - 1 - i
        total += place * (60 ** power)
    
    return total


# Convenience function for full rendering pipeline
def render_decimal_as_base60(value: int, style: str = DEFAULT_STYLE, separator: str = " ") -> Dict:
    """
    Convert a decimal value to Base-60 and render as glyphs.
    
    This is the primary function for the complete rendering pipeline.
    
    Args:
        value: Decimal integer to render
        style: Glyph style name (default: "wedge_ascii")
        separator: String to join rendered digits (default: " ")
        
    Returns:
        Dictionary with:
            - decimal: original value
            - base60_places: list of place values
            - glyphs: rendered string
            - style: style used
            
    Examples:
        >>> render_decimal_as_base60(3723)
        {'decimal': 3723, 'base60_places': [1, 2, 3], 'glyphs': '| << <<<', 'style': 'wedge_ascii'}
    """
    places = decimal_to_base60(value)
    glyphs = render_places(places, style, separator)
    
    return {
        "decimal": value,
        "base60_places": places,
        "glyphs": glyphs,
        "style": style
    }


if __name__ == "__main__":
    # Demonstration
    print("ENKI Base-60 Glyph Rendering Demo\n")
    
    # Test rendering digits 0-59
    print("Digits 0-9:")
    for i in range(10):
        glyph = render_digit(i)
        print(f"  {i:2d} → {glyph}")
    
    print("\nDigits 10-19:")
    for i in range(10, 20):
        glyph = render_digit(i)
        print(f"  {i:2d} → {glyph}")
    
    print("\nDigits 50-59:")
    for i in range(50, 60):
        glyph = render_digit(i)
        print(f"  {i:2d} → {glyph}")
    
    # Test decimal conversion and rendering
    print("\n\nDecimal to Base-60 Examples:")
    test_values = [0, 1, 59, 60, 119, 3600, 3723]
    for val in test_values:
        result = render_decimal_as_base60(val)
        print(f"  {val:5d} → places={result['base60_places']} → glyphs='{result['glyphs']}'")
    
    # Test verification
    print("\n\nVerification (Base-60 to Decimal):")
    test_places = [[0], [1], [59], [1, 0], [1, 59], [1, 2, 3]]
    for places in test_places:
        decimal = base60_to_decimal(places)
        print(f"  {places} → {decimal}")
