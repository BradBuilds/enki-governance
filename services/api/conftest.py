"""
Pytest configuration for ENKI API tests.

Provides:
- Auto-skip for DB-dependent tests when ENKI_DB_DSN not configured
"""

import os
import pytest


def pytest_collection_modifyitems(config, items):
    """
    Auto-skip DB-dependent tests when ENKI_DB_DSN not configured.
    
    Tests marked with @pytest.mark.db will be skipped unless
    ENKI_DB_DSN environment variable is set.
    """
    db_dsn = os.getenv("ENKI_DB_DSN")
    skip_db = pytest.mark.skip(reason="ENKI_DB_DSN not set - skipping DB-dependent tests")
    
    for item in items:
        if "db" in item.keywords and not db_dsn:
            item.add_marker(skip_db)
