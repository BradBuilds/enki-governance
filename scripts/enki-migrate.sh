#!/usr/bin/env bash
# Enki Governance — Database Migration Runner
# Runs all pending migrations in order. Safe to run multiple times.
#
# Usage:
#   bash scripts/enki-migrate.sh                    # default: localhost
#   bash scripts/enki-migrate.sh --host db.example.com --port 5432
#   PGPASSWORD=secret bash scripts/enki-migrate.sh  # with password

set -euo pipefail

# Defaults
DB_HOST="${ENKI_DB_HOST:-localhost}"
DB_PORT="${ENKI_DB_PORT:-5432}"
DB_NAME="${ENKI_DB_NAME:-enki}"
DB_USER="${ENKI_DB_USER:-enki_db}"
MIGRATIONS_DIR="$(dirname "$0")/../storage/postgres/migrations"

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --host) DB_HOST="$2"; shift 2 ;;
        --port) DB_PORT="$2"; shift 2 ;;
        --db)   DB_NAME="$2"; shift 2 ;;
        --user) DB_USER="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

PSQL="psql -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -v ON_ERROR_STOP=1"

echo "╔═══════════════════════════════════════════════════════╗"
echo "║  Enki Database Migration Runner                       ║"
echo "║  Host: $DB_HOST:$DB_PORT  DB: $DB_NAME               "
echo "╚═══════════════════════════════════════════════════════╝"

# Ensure schema_migrations table exists
$PSQL -c "
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     VARCHAR(255) PRIMARY KEY,
    description VARCHAR(1000),
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
" 2>/dev/null || true

# Fresh DB bootstrap: numbered migrations are additive on top of
# storage/postgres/schema_v2.sql (which creates knowledge_nodes, entities,
# topics, etc.). If the foundational tables don't exist, load schema_v2
# first — it's idempotent, but we gate on existence to keep the common
# "already initialized" case fast and quiet.
SCHEMA_V2="$(dirname "$0")/../storage/postgres/schema_v2.sql"
KN_EXISTS=$($PSQL -t -A -c "SELECT to_regclass('public.knowledge_nodes') IS NOT NULL;" 2>/dev/null || echo "f")
if [ "$KN_EXISTS" != "t" ]; then
    if [ -f "$SCHEMA_V2" ]; then
        echo "  → Fresh database detected — loading baseline schema_v2.sql"
        if $PSQL -f "$SCHEMA_V2" >/dev/null; then
            echo "    ✓ baseline applied"
        else
            echo "    ✗ baseline FAILED"
            exit 1
        fi
    else
        echo "  ✗ Foundational tables missing AND $SCHEMA_V2 not found — cannot bootstrap"
        exit 1
    fi
fi

# Get applied migrations
APPLIED=$($PSQL -t -A -c "SELECT version FROM schema_migrations ORDER BY version;" 2>/dev/null || echo "")

# Find and run pending migrations
PENDING=0
for migration_file in "$MIGRATIONS_DIR"/*.sql; do
    [ -f "$migration_file" ] || continue

    version=$(basename "$migration_file" .sql)

    if echo "$APPLIED" | grep -q "^${version}$"; then
        echo "  ✓ $version (already applied)"
        continue
    fi

    echo "  → Running $version ..."
    if $PSQL -f "$migration_file"; then
        # Record the apply if the migration didn't already insert its own row.
        # ON CONFLICT DO NOTHING keeps this a no-op for migrations that
        # already record themselves.
        $PSQL -q -c "INSERT INTO schema_migrations (version, description)
                     VALUES ('${version}', 'auto-recorded by enki-migrate.sh')
                     ON CONFLICT (version) DO NOTHING;" >/dev/null
        echo "    ✓ $version applied"
        PENDING=$((PENDING + 1))
    else
        echo "    ✗ $version FAILED"
        exit 1
    fi
done

if [ "$PENDING" -eq 0 ]; then
    echo ""
    echo "  Database is up to date. No pending migrations."
else
    echo ""
    echo "  Applied $PENDING migration(s)."
fi

echo ""
echo "Done."
