"""Per-dataset parsers for the refdata layer.

Each module exposes:
    run(pack_def: dict, refdata_home: Path) -> dict

with returned keys: records_indexed, bytes_downloaded, sha256, version,
on_disk_path. The manager handles registry bookkeeping; the parser is
responsible only for download + index.

Modules also typically expose a `lookup(name, ...)` function used by source
handlers to query the local cache.
"""
