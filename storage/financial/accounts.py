"""
Financial Accounts — CRUD + balance tracking + recurring detection.

Manages financial accounts (checking, savings, credit cards, loans),
tracks balance history over time, and detects recurring transactions.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone, timedelta, date
from typing import Optional

from storage.postgres.db import get_connection

logger = logging.getLogger("enki.financial")


# ─── Account CRUD ─────────────────────────────────────────────────────────────

def _generate_account_id(tenant_id: str, name: str) -> str:
    """Generate deterministic account ID from tenant + name."""
    h = hashlib.sha256(f"{tenant_id}:{name}".encode()).hexdigest()[:12]
    return f"acct_{h}"


def upsert_account(
    tenant_id: str,
    name: str,
    account_type: str,
    institution: str = None,
    account_number_masked: str = None,
    routing_number: str = None,
    current_balance: float = None,
    credit_limit: float = None,
    interest_rate: float = None,
    device_id: str = None,
    dsn: Optional[str] = None,
) -> dict:
    """Create or update a financial account. Returns account dict."""
    account_id = _generate_account_id(tenant_id, name)
    is_liability = account_type in ("credit_card", "loan", "mortgage")

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO financial_accounts
                    (account_id, tenant_id, name, institution, account_type,
                     account_number_masked, routing_number, is_liability,
                     current_balance, credit_limit, interest_rate, device_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_id) DO UPDATE SET
                    current_balance = COALESCE(EXCLUDED.current_balance, financial_accounts.current_balance),
                    institution = COALESCE(EXCLUDED.institution, financial_accounts.institution),
                    account_number_masked = COALESCE(EXCLUDED.account_number_masked, financial_accounts.account_number_masked),
                    credit_limit = COALESCE(EXCLUDED.credit_limit, financial_accounts.credit_limit),
                    interest_rate = COALESCE(EXCLUDED.interest_rate, financial_accounts.interest_rate),
                    last_statement_date = NOW()::date,
                    updated_at = NOW()
                RETURNING account_id, name, account_type, institution, is_liability,
                          current_balance, credit_limit, interest_rate, status
            """, (
                account_id, tenant_id, name, institution, account_type,
                account_number_masked, routing_number, is_liability,
                current_balance, credit_limit, interest_rate, device_id,
            ))
            row = cur.fetchone()
        conn.commit()

    return {
        "account_id": row[0], "name": row[1], "account_type": row[2],
        "institution": row[3], "is_liability": row[4],
        "current_balance": float(row[5]) if row[5] else 0,
        "credit_limit": float(row[6]) if row[6] else None,
        "interest_rate": float(row[7]) if row[7] else None,
        "status": row[8],
    }


def list_accounts(tenant_id: str, dsn: Optional[str] = None) -> list[dict]:
    """List all financial accounts for a tenant."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT account_id, name, institution, account_type, is_liability,
                       current_balance, available_balance, credit_limit, interest_rate,
                       minimum_payment, payment_due_date, original_balance,
                       currency, last_statement_date, status, account_number_masked,
                       created_at, updated_at
                FROM financial_accounts
                WHERE tenant_id = %s AND status != 'closed'
                ORDER BY account_type, name
            """, (tenant_id,))
            return [
                {
                    "account_id": r[0], "name": r[1], "institution": r[2],
                    "account_type": r[3], "is_liability": r[4],
                    "current_balance": float(r[5]) if r[5] else 0,
                    "available_balance": float(r[6]) if r[6] else None,
                    "credit_limit": float(r[7]) if r[7] else None,
                    "interest_rate": float(r[8]) if r[8] else None,
                    "minimum_payment": float(r[9]) if r[9] else None,
                    "payment_due_date": str(r[10]) if r[10] else None,
                    "original_balance": float(r[11]) if r[11] else None,
                    "currency": r[12], "last_statement_date": str(r[13]) if r[13] else None,
                    "status": r[14], "account_number_masked": r[15],
                    "created_at": str(r[16]), "updated_at": str(r[17]),
                }
                for r in cur.fetchall()
            ]


def update_account(
    account_id: str,
    tenant_id: str,
    updates: dict,
    dsn: Optional[str] = None,
) -> bool:
    """Update account fields. Returns True if updated."""
    allowed = {
        "name", "institution", "account_type", "current_balance",
        "available_balance", "credit_limit", "interest_rate",
        "minimum_payment", "payment_due_date", "original_balance",
        "status", "tags",
    }
    fields = {k: v for k, v in updates.items() if k in allowed}
    if not fields:
        return False

    # Recalculate is_liability if account_type changed
    if "account_type" in fields:
        fields["is_liability"] = fields["account_type"] in ("credit_card", "loan", "mortgage")

    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values())

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE financial_accounts SET {set_clause}, updated_at = NOW() "
                f"WHERE account_id = %s AND tenant_id = %s",
                values + [account_id, tenant_id],
            )
            updated = cur.rowcount > 0
        conn.commit()
    return updated


# ─── Balance History ──────────────────────────────────────────────────────────

def record_balance(
    account_id: str,
    tenant_id: str,
    balance: float,
    balance_date: date,
    source: str = "statement",
    signal_id: str = None,
    dsn: Optional[str] = None,
) -> None:
    """Record a balance snapshot (append-only history)."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO financial_balance_history
                    (account_id, tenant_id, balance, balance_date, source, signal_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_id, balance_date, source) DO UPDATE
                SET balance = EXCLUDED.balance
            """, (account_id, tenant_id, balance, balance_date, source, signal_id))

            # Also update current_balance on the account
            cur.execute("""
                UPDATE financial_accounts
                SET current_balance = %s, last_statement_date = %s, updated_at = NOW()
                WHERE account_id = %s AND tenant_id = %s
            """, (balance, balance_date, account_id, tenant_id))
        conn.commit()


def get_balance_history(
    account_id: str,
    tenant_id: str,
    days: int = 365,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Get balance history for an account."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT balance_date, balance, source
                FROM financial_balance_history
                WHERE account_id = %s AND tenant_id = %s AND created_at >= %s
                ORDER BY balance_date
            """, (account_id, tenant_id, since))
            return [
                {"date": str(r[0]), "balance": float(r[1]), "source": r[2]}
                for r in cur.fetchall()
            ]


# ─── Net Worth Calculation ────────────────────────────────────────────────────

def calculate_net_worth(tenant_id: str, dsn: Optional[str] = None) -> dict:
    """
    Calculate net worth = assets - liabilities.

    Assets come from:
      1. financial_accounts where is_liability = FALSE (checking, savings, investment)
      2. assets table (vehicles, property, crypto, equipment)

    Liabilities come from:
      1. financial_accounts where is_liability = TRUE (credit cards, loans, mortgages)
    """
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Financial account balances
            cur.execute("""
                SELECT account_id, name, account_type, is_liability,
                       current_balance, institution
                FROM financial_accounts
                WHERE tenant_id = %s AND status = 'active'
                ORDER BY is_liability, account_type, name
            """, (tenant_id,))
            accounts = cur.fetchall()

            # Physical/digital assets
            cur.execute("""
                SELECT asset_id, display_name, asset_category,
                       COALESCE(estimated_value, 0)
                FROM assets
                WHERE tenant_id = %s AND status = 'active'
                ORDER BY estimated_value DESC
            """, (tenant_id,))
            physical_assets = cur.fetchall()

    # Build asset and liability lists
    asset_items = []
    liability_items = []

    for r in accounts:
        item = {
            "id": r[0], "name": r[1], "type": r[2],
            "is_liability": r[3], "balance": float(r[4]) if r[4] else 0,
            "institution": r[5], "source": "financial_account",
        }
        if r[3]:  # is_liability
            liability_items.append(item)
        else:
            asset_items.append(item)

    for r in physical_assets:
        asset_items.append({
            "id": r[0], "name": r[1], "type": r[2],
            "balance": float(r[3]), "source": "asset",
        })

    total_assets = sum(a["balance"] for a in asset_items)
    total_liabilities = sum(abs(l["balance"]) for l in liability_items)
    net_worth = total_assets - total_liabilities

    return {
        "net_worth": round(net_worth, 2),
        "total_assets": round(total_assets, 2),
        "total_liabilities": round(total_liabilities, 2),
        "assets": asset_items,
        "liabilities": liability_items,
    }


# ─── Recurring Transaction Detection ─────────────────────────────────────────

def detect_recurring(tenant_id: str, dsn: Optional[str] = None) -> list[dict]:
    """
    Analyze transaction history to detect recurring patterns.

    Groups transactions by merchant name (normalized), counts occurrences,
    checks for regular intervals. Requires 2+ occurrences to flag.
    """
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Get merchant frequency and amount consistency
            cur.execute("""
                SELECT
                    LOWER(TRIM(payload->>'description')) as merchant,
                    COUNT(*) as count,
                    AVG((payload->>'amount')::numeric) as avg_amount,
                    STDDEV((payload->>'amount')::numeric) as stddev_amount,
                    MIN(timestamp::date) as first_seen,
                    MAX(timestamp::date) as last_seen,
                    COALESCE(payload->>'category', '') as category,
                    COALESCE(payload->>'category_id', '') as category_id
                FROM signals
                WHERE tenant_id = %s
                  AND signal_type = 'signal.financial.transaction'
                  AND payload->>'_parent_signal_id' IS NOT NULL
                  AND payload->>'description' IS NOT NULL
                GROUP BY LOWER(TRIM(payload->>'description')),
                         payload->>'category', payload->>'category_id'
                HAVING COUNT(*) >= 2
                ORDER BY COUNT(*) DESC
                LIMIT 50
            """, (tenant_id,))

            results = []
            for r in cur.fetchall():
                merchant = r[0] or ""
                count = r[1]
                avg_amount = float(r[2]) if r[2] else 0
                stddev = float(r[3]) if r[3] else 0
                first_seen = r[4]
                last_seen = r[5]
                category = r[6]
                category_id = r[7]

                if not merchant or count < 2:
                    continue

                # Estimate frequency from date range
                if first_seen and last_seen and first_seen != last_seen:
                    span_days = (last_seen - first_seen).days
                    avg_interval = span_days / (count - 1) if count > 1 else span_days

                    if avg_interval <= 8:
                        frequency = "weekly"
                    elif avg_interval <= 16:
                        frequency = "biweekly"
                    elif avg_interval <= 35:
                        frequency = "monthly"
                    elif avg_interval <= 100:
                        frequency = "quarterly"
                    else:
                        frequency = "annual"
                else:
                    frequency = "unknown"

                is_income = avg_amount > 0

                # Upsert into recurring table
                recurring_id = f"rec_{hashlib.sha256(f'{tenant_id}:{merchant}'.encode()).hexdigest()[:12]}"
                cur.execute("""
                    INSERT INTO financial_recurring
                        (recurring_id, tenant_id, merchant_pattern, typical_amount,
                         amount_variance, frequency, category, category_id,
                         is_income, last_seen, occurrence_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (recurring_id) DO UPDATE SET
                        typical_amount = EXCLUDED.typical_amount,
                        amount_variance = EXCLUDED.amount_variance,
                        frequency = EXCLUDED.frequency,
                        last_seen = EXCLUDED.last_seen,
                        occurrence_count = EXCLUDED.occurrence_count,
                        updated_at = NOW()
                """, (
                    recurring_id, tenant_id, merchant, round(avg_amount, 2),
                    round(stddev, 2), frequency, category, category_id,
                    is_income, last_seen, count,
                ))

                results.append({
                    "recurring_id": recurring_id,
                    "merchant": merchant,
                    "typical_amount": round(avg_amount, 2),
                    "amount_variance": round(stddev, 2),
                    "frequency": frequency,
                    "category": category,
                    "is_income": is_income,
                    "last_seen": str(last_seen) if last_seen else None,
                    "occurrence_count": count,
                })

            conn.commit()
            return results


def list_recurring(tenant_id: str, dsn: Optional[str] = None) -> list[dict]:
    """List all detected recurring transactions."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT recurring_id, merchant_pattern, typical_amount, amount_variance,
                       frequency, category, category_id, is_income, last_seen,
                       next_expected, occurrence_count, status
                FROM financial_recurring
                WHERE tenant_id = %s
                ORDER BY occurrence_count DESC
            """, (tenant_id,))
            return [
                {
                    "recurring_id": r[0], "merchant": r[1],
                    "typical_amount": float(r[2]) if r[2] else 0,
                    "amount_variance": float(r[3]) if r[3] else 0,
                    "frequency": r[4], "category": r[5], "category_id": r[6],
                    "is_income": r[7], "last_seen": str(r[8]) if r[8] else None,
                    "next_expected": str(r[9]) if r[9] else None,
                    "occurrence_count": r[10], "status": r[11],
                }
                for r in cur.fetchall()
            ]
