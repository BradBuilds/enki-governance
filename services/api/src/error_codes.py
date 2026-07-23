"""
API Error Code Registry - Canonical Source

This module defines the official API error codes and their HTTP status mappings.
The registry in this file is the single source of truth.

services/api/API_ERROR_CODES.md is validated against this registry.
"""

from types import MappingProxyType
from typing import Dict, Tuple


class UnknownApiErrorCode(Exception):
    """Raised when attempting to map an unregistered API error code.
    
    This exception indicates a code registration gap:
    either a route is using a code that wasn't declared in the registry,
    or the registry is incomplete.
    
    Always an error condition in production.
    """
    def __init__(self, code: str):
        self.code = code
        super().__init__(
            f"API error code '{code}' not registered. "
            f"Add to API_ERROR_CODE_REGISTRY in {__file__}"
        )

# Canonical error code registry: (error_code, http_status, severity, description)
_API_ERROR_CODE_REGISTRY: Dict[str, Tuple[int, str, str]] = {
    # Client Request Errors (4xx)
    "API.BAD_REQUEST": (
        400,
        "validation",
        "Invalid input format, limit constraints, or cursor validation"
    ),
    "API.UNAUTHORIZED": (
        401,
        "auth",
        "Missing or invalid authentication credentials"
    ),
    "API.FORBIDDEN": (
        403,
        "authz",
        "Valid authentication but insufficient permissions"
    ),
    "API.NOT_FOUND": (
        404,
        "lookup",
        "Requested resource does not exist"
    ),
    "API.PAYLOAD_TOO_LARGE": (
        413,
        "rejection",
        "Request body exceeds configured size limit"
    ),
    
    # Server Errors (5xx)
    "SERVER.MISCONFIGURATION": (
        500,
        "server",
        "Server configuration error or unregistered error code"
    ),
    "API.TIMEOUT": (
        503,
        "transient",
        "Request processing exceeded configured timeout"
    ),
}

# Read-only public view to prevent runtime mutation
API_ERROR_CODE_REGISTRY = MappingProxyType(_API_ERROR_CODE_REGISTRY)


def get_http_status(error_code: str) -> int:
    """Get HTTP status for an error code.
    
    Raises UnknownApiErrorCode if code is not registered.
    This is always an error condition: either the registry is incomplete
    or a route is using a code it shouldn't.
    """
    if error_code in API_ERROR_CODE_REGISTRY:
        return API_ERROR_CODE_REGISTRY[error_code][0]
    raise UnknownApiErrorCode(error_code)


def is_registered(error_code: str) -> bool:
    """Check if an error code is registered."""
    return error_code in API_ERROR_CODE_REGISTRY


def all_error_codes() -> set:
    """Get all registered error codes."""
    return set(API_ERROR_CODE_REGISTRY.keys())
