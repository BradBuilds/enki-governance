"""
ENKI API Routes - Render

Base-60 rendering endpoints.

Author: ENKI System
Version: 0.1.0
"""

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import JSONResponse
import sys
from pathlib import Path

# Add core validation to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "core"))

from validation import render_decimal_as_base60, GLYPH_STYLES

router = APIRouter(prefix="/render", tags=["render"])


@router.get("/base60")
async def render_base60_endpoint(
    value: int = Query(..., description="Decimal integer to render (>= 0)"),
    style: str = Query("wedge_ascii", description="Glyph style"),
    separator: str = Query(" ", description="Separator between digits")
):
    """
    Render a decimal integer as Base-60 glyphs.
    
    This is the primary rendering endpoint.
    """
    # Validate value
    if value < 0:
        raise HTTPException(
            status_code=400,
            detail="BASE60_NEGATIVE: Value must be >= 0"
        )
    
    # Validate style
    if style not in GLYPH_STYLES:
        raise HTTPException(
            status_code=400,
            detail=f"INVALID_STYLE: Unknown style '{style}'. Valid: {list(GLYPH_STYLES.keys())}"
        )
    
    # Render
    try:
        result = render_decimal_as_base60(value, style, separator)
        return JSONResponse(content=result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Render failed")
