"""
Storage configuration from environment variables.

Required:
- ENKI_DB_DSN: PostgreSQL connection string

Optional:
- ENKI_BATCH_SIZE: Batch size for ingestion (default: 100)
"""

import os


def get_db_dsn() -> str:
    """
    Get PostgreSQL DSN from environment.
    
    Raises:
        EnvironmentError: If ENKI_DB_DSN is not set
    
    Returns:
        PostgreSQL connection string
    """
    dsn = os.environ.get('ENKI_DB_DSN')
    if not dsn:
        raise EnvironmentError(
            "ENKI_DB_DSN environment variable not set. "
            "Set it to your PostgreSQL connection string. "
            "Example: postgresql://user:pass@localhost:5432/enki_dev"
        )
    return dsn


def get_batch_size() -> int:
    """Get batch size for ingestion operations (default: 100)"""
    return int(os.environ.get('ENKI_BATCH_SIZE', '100'))
