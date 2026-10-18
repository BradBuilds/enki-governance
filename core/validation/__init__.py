"""
ENKI Core Validation Package

Provides validation and rendering for Base-60 numeric system.

Modules:
- render_glyphs: Glyph rendering logic
- validate_schema: JSON schema validation
- validate_semantics: Semantic validation

Author: ENKI System
Version: 0.1.0
"""

from .render_glyphs import (
    render_digit,
    render_places,
    decimal_to_base60,
    base60_to_decimal,
    render_decimal_as_base60,
    GLYPH_STYLES,
    DEFAULT_STYLE
)

from .validate_semantics import (
    validate_base60_digit,
    validate_base60_places,
    validate_conversion_correctness,
    validate_rendering_determinism
)

from .validate_schema import (
    validate_definition,
    validate_registry_pack,
    load_schema
)

__all__ = [
    # Rendering
    'render_digit',
    'render_places',
    'decimal_to_base60',
    'base60_to_decimal',
    'render_decimal_as_base60',
    'GLYPH_STYLES',
    'DEFAULT_STYLE',
    
    # Semantic validation
    'validate_base60_digit',
    'validate_base60_places',
    'validate_conversion_correctness',
    'validate_rendering_determinism',
    
    # Schema validation
    'validate_definition',
    'validate_registry_pack',
    'load_schema'
]

__version__ = "0.1.0"
