"""
ENKI Semantic Validation

Validates semantic correctness of Base-60 values and operations.

This module enforces:
- Digit range constraints
- Place value consistency
- Type checking
- Logical correctness

Author: ENKI System
Version: 0.1.0
"""

from typing import Dict, List


def validate_base60_digit(d) -> Dict[str, any]:
    """
    Validate a single Base-60 digit.
    
    Args:
        d: Value to validate as digit
        
    Returns:
        Dictionary with:
            - ok: bool (True if valid)
            - errors: List[str] (error messages)
            
    Rules:
        - Must be integer type
        - Must be >= 0
        - Must be <= 59
    """
    errors = []
    
    # Check type
    if not isinstance(d, int):
        errors.append(f"BASE60_INVALID_TYPE: Digit must be an integer, got {type(d).__name__}")
        return {"ok": False, "errors": errors}
    
    # Check range
    if d < 0:
        errors.append(f"BASE60_DIGIT_RANGE: Digit must be >= 0, got {d}")
    
    if d > 59:
        errors.append(f"BASE60_DIGIT_RANGE: Digit must be <= 59, got {d}")
    
    return {
        "ok": len(errors) == 0,
        "errors": errors
    }


def validate_base60_places(places) -> Dict[str, any]:
    """
    Validate a list of Base-60 place values.
    
    Args:
        places: Value to validate as place list
        
    Returns:
        Dictionary with:
            - ok: bool (True if valid)
            - errors: List[str] (error messages)
            
    Rules:
        - Must be a list
        - Must not be empty
        - Cannot contain null/None values
        - Each place must be a valid digit
    """
    errors = []
    
    # Check type
    if not isinstance(places, list):
        errors.append(f"BASE60_INVALID_TYPE: Places must be a list, got {type(places).__name__}")
        return {"ok": False, "errors": errors}
    
    # Check not empty
    if len(places) == 0:
        errors.append("BASE60_EMPTY_PLACES: Places list cannot be empty")
        return {"ok": False, "errors": errors}
    
    # Check for null values
    for i, place in enumerate(places):
        if place is None:
            errors.append(f"BASE60_NULL_VALUE: Place at position {i} is null")
    
    if errors:
        return {"ok": False, "errors": errors}
    
    # Validate each digit
    for i, place in enumerate(places):
        digit_result = validate_base60_digit(place)
        if not digit_result["ok"]:
            for err in digit_result["errors"]:
                errors.append(f"Position {i}: {err}")
    
    return {
        "ok": len(errors) == 0,
        "errors": errors
    }


def validate_conversion_correctness(decimal: int, places: List[int]) -> Dict[str, any]:
    """
    Validate that a decimal-to-base60 conversion is mathematically correct.
    
    Args:
        decimal: Original decimal value
        places: Base-60 place values
        
    Returns:
        Dictionary with:
            - ok: bool (True if correct)
            - errors: List[str] (error messages)
            - expected_decimal: int (recalculated value)
            
    Verification:
        - Recalculates decimal from places
        - Checks if it matches original
    """
    errors = []
    
    # Validate inputs first
    if not isinstance(decimal, int):
        errors.append(f"BASE60_INVALID_TYPE: Decimal must be integer, got {type(decimal).__name__}")
        return {"ok": False, "errors": errors, "expected_decimal": None}
    
    places_validation = validate_base60_places(places)
    if not places_validation["ok"]:
        errors.extend(places_validation["errors"])
        return {"ok": False, "errors": errors, "expected_decimal": None}
    
    # Recalculate decimal from places
    n = len(places)
    calculated_decimal = sum(place * (60 ** (n - 1 - i)) for i, place in enumerate(places))
    
    # Check if it matches
    if calculated_decimal != decimal:
        errors.append(
            f"CONVERSION_MISMATCH: Places {places} calculate to {calculated_decimal}, "
            f"but expected {decimal}"
        )
    
    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "expected_decimal": calculated_decimal
    }


def validate_rendering_determinism(render_func, test_value, iterations: int = 10) -> Dict[str, any]:
    """
    Validate that rendering is deterministic (same input → same output).
    
    Args:
        render_func: Function to test (must accept test_value)
        test_value: Input value to test with
        iterations: Number of times to render (default: 10)
        
    Returns:
        Dictionary with:
            - ok: bool (True if deterministic)
            - errors: List[str] (error messages)
            - outputs: List[str] (all outputs generated)
    """
    errors = []
    outputs = []
    
    try:
        # Render multiple times
        for i in range(iterations):
            output = render_func(test_value)
            outputs.append(output)
        
        # Check all outputs are identical
        first_output = outputs[0]
        for i, output in enumerate(outputs[1:], start=1):
            if output != first_output:
                errors.append(
                    f"NON_DETERMINISTIC: Iteration {i} produced different output. "
                    f"Expected: {first_output}, Got: {output}"
                )
    except Exception as e:
        errors.append(f"RENDER_ERROR: {e}")
    
    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "outputs": outputs
    }


if __name__ == "__main__":
    # Demonstration
    print("ENKI Semantic Validation Demo\n")
    
    # Test digit validation
    print("Digit Validation:")
    test_digits = [0, 30, 59, 60, -1, 3.14, "42"]
    for d in test_digits:
        result = validate_base60_digit(d)
        status = "✓" if result["ok"] else "✗"
        print(f"  {status} {d!r:5} → {result['errors'] if not result['ok'] else 'valid'}")
    
    # Test places validation
    print("\nPlaces Validation:")
    test_places = [
        [1, 2, 3],
        [0],
        [],
        [1, 60, 3],
        [1, None, 3],
        "not a list"
    ]
    for places in test_places:
        result = validate_base60_places(places)
        status = "✓" if result["ok"] else "✗"
        print(f"  {status} {places!r:20} → {result['errors'] if not result['ok'] else 'valid'}")
    
    # Test conversion correctness
    print("\nConversion Correctness:")
    test_conversions = [
        (3723, [1, 2, 3]),
        (0, [0]),
        (60, [1, 0]),
        (3723, [1, 2, 4]),  # Intentionally wrong
    ]
    for decimal, places in test_conversions:
        result = validate_conversion_correctness(decimal, places)
        status = "✓" if result["ok"] else "✗"
        print(f"  {status} {decimal} ↔ {places} → {result.get('expected_decimal')} {result['errors'] if not result['ok'] else ''}")
