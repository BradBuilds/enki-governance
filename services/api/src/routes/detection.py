"""
Detection rules, observable search, and dashboard API endpoints.
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

from storage.signals.rules import create_rule, get_rule, list_rules, update_rule, delete_rule
from core.detection.rule_presets import get_all_presets, get_preset, get_categories
from storage.signals.store import search_observables
from storage.signals.dashboard import get_dashboard_summary
from storage.signals.organize import get_organized_view, get_domain_signals, get_source_breakdown, get_timeline_view

router = APIRouter(tags=["detection & dashboard"])


class CreateRuleRequest(BaseModel):
    name: str
    rule_type: str
    severity: str
    condition: dict
    description: Optional[str] = None
    category: Optional[str] = None
    actions: Optional[dict] = None


class UpdateRuleRequest(BaseModel):
    enabled: Optional[bool] = None
    condition: Optional[dict] = None
    severity: Optional[str] = None


# ─── DETECTION RULES ──────────────────────────────────────────────────────────

@router.post("/detection/rules")
async def create_rule_endpoint(
    body: CreateRuleRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    x_device_id: str = Header("unknown", alias="X-Device-ID"),
):
    rule = create_rule(
        tenant_id=auth.tenant_id, name=body.name, rule_type=body.rule_type,
        severity=body.severity, condition=body.condition,
        description=body.description, category=body.category,
        actions=body.actions, created_by=x_device_id,
    )
    return JSONResponse(status_code=201, content=rule)


@router.get("/detection/rules")
async def list_rules_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    enabled: Optional[bool] = Query(None),
    rule_type: Optional[str] = Query(None),
    limit: int = Query(100, le=1000),
):
    rules = list_rules(auth.tenant_id, enabled_only=bool(enabled), rule_type=rule_type, limit=limit)
    return {"rules": rules, "count": len(rules)}


@router.get("/detection/rules/{rule_id}")
async def get_rule_endpoint(
    rule_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    rule = get_rule(rule_id, auth.tenant_id)
    if not rule:
        return JSONResponse(status_code=404, content={"error": "RULE.NOT_FOUND"})
    return rule


@router.patch("/detection/rules/{rule_id}")
async def update_rule_endpoint(
    rule_id: str,
    body: UpdateRuleRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    ok = update_rule(rule_id, auth.tenant_id, enabled=body.enabled,
                     condition=body.condition, severity=body.severity)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "RULE.NOT_FOUND"})
    return {"rule_id": rule_id, "updated": True}


@router.delete("/detection/rules/{rule_id}")
async def delete_rule_endpoint(
    rule_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    ok = delete_rule(rule_id, auth.tenant_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "RULE.NOT_FOUND"})
    return {"rule_id": rule_id, "disabled": True}


# ─── RULE PRESETS (Alert Library) ─────────────────────────────────────────────

@router.get("/detection/presets")
async def list_presets_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    category: Optional[str] = Query(None),
):
    """
    List all pre-built detection rule presets (the Alert Library).

    Returns presets with their enabled/disabled state for this tenant.
    A preset is 'active' if a detection rule with matching preset_id exists.
    """
    presets = get_all_presets()
    if category:
        presets = [p for p in presets if p["category"] == category]

    # Check which presets are already activated as rules
    existing_rules = list_rules(auth.tenant_id, enabled_only=False, limit=1000)
    active_preset_ids = {}
    for rule in existing_rules:
        # Rules created from presets store preset_id in actions
        pid = (rule.get("actions") or {}).get("preset_id")
        if pid:
            active_preset_ids[pid] = {
                "rule_id": rule["rule_id"],
                "enabled": rule.get("enabled", True),
                "match_count": rule.get("match_count", 0),
                "last_matched_at": rule.get("last_matched_at"),
            }

    # Merge activation state into presets
    result = []
    for p in presets:
        entry = {**p}
        active = active_preset_ids.get(p["preset_id"])
        if active:
            entry["active"] = True
            entry["enabled"] = active["enabled"]
            entry["rule_id"] = active["rule_id"]
            entry["match_count"] = active["match_count"]
            entry["last_matched_at"] = str(active["last_matched_at"]) if active["last_matched_at"] else None
        else:
            entry["active"] = False
            entry["enabled"] = False
            entry["rule_id"] = None
            entry["match_count"] = 0
            entry["last_matched_at"] = None
        result.append(entry)

    return {"presets": result, "count": len(result)}


@router.get("/detection/presets/categories")
async def list_preset_categories():
    """List all preset categories with counts."""
    return {"categories": get_categories()}


@router.post("/detection/presets/{preset_id}/activate")
async def activate_preset(
    preset_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    x_device_id: str = Header("system", alias="X-Device-ID"),
):
    """
    Activate a preset — creates a detection rule from the preset template.
    If already activated, re-enables the existing rule.
    """
    preset = get_preset(preset_id)
    if not preset:
        return JSONResponse(status_code=404, content={"error": "PRESET.NOT_FOUND"})

    # Check if already exists
    existing = list_rules(auth.tenant_id, enabled_only=False, limit=1000)
    for rule in existing:
        pid = (rule.get("actions") or {}).get("preset_id")
        if pid == preset_id:
            # Re-enable if disabled
            if not rule.get("enabled", True):
                update_rule(rule["rule_id"], auth.tenant_id, enabled=True)
            return {"activated": True, "rule_id": rule["rule_id"], "reactivated": True}

    # Create new rule from preset
    rule = create_rule(
        tenant_id=auth.tenant_id,
        name=preset["name"],
        rule_type=preset["rule_type"],
        severity=preset["severity"],
        condition=preset["condition"],
        description=preset["description"],
        category=preset["category"],
        actions={"preset_id": preset_id},
        created_by=x_device_id,
    )

    return JSONResponse(status_code=201, content={
        "activated": True, "rule_id": rule.get("rule_id"), "created": True,
    })


@router.post("/detection/presets/{preset_id}/deactivate")
async def deactivate_preset(
    preset_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Deactivate a preset — disables (not deletes) the detection rule."""
    existing = list_rules(auth.tenant_id, enabled_only=False, limit=1000)
    for rule in existing:
        pid = (rule.get("actions") or {}).get("preset_id")
        if pid == preset_id:
            update_rule(rule["rule_id"], auth.tenant_id, enabled=False)
            return {"deactivated": True, "rule_id": rule["rule_id"]}

    return JSONResponse(status_code=404, content={
        "error": "PRESET.NOT_ACTIVE", "detail": f"Preset {preset_id} is not currently active",
    })


# ─── OBSERVABLE SEARCH ─────────────────────────────────────────────────────────

@router.get("/search/observables")
async def search_observables_endpoint(
    observable_type: str = Query(...),
    value: str = Query(...),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Find all signals containing a specific observable value."""
    normalized = value.strip().lower()
    results = search_observables(auth.tenant_id, observable_type, normalized)
    return {"observable_type": observable_type, "value": value,
            "matches": results, "count": len(results)}


# ─── DASHBOARD ─────────────────────────────────────────────────────────────────

@router.get("/dashboard/summary")
async def dashboard_summary_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """High-level stats for the dashboard."""
    return get_dashboard_summary(auth.tenant_id)


# ─── ORGANIZED VIEWS ──────────────────────────────────────────────────────────

@router.get("/organize/domains")
async def organized_view_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    All signals organized by domain with counts.

    Returns folder-like structure:
      Financial (47): transactions (32), invoices (10), payments (5)
      Communications (123): email (89), chat (30), sms (4)
      Security (15): auth_failures (8), intrusion (4), policy (3)
    """
    return {"domains": get_organized_view(auth.tenant_id)}


@router.get("/organize/domains/{domain}")
async def domain_signals_endpoint(
    domain: str,
    signal_type: Optional[str] = Query(None),
    limit: int = Query(100, le=1000),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Get signals within a specific domain (e.g., 'financial', 'communications')."""
    signals = get_domain_signals(auth.tenant_id, domain, signal_type, limit)
    return {"domain": domain, "signals": signals, "count": len(signals)}


@router.get("/organize/sources")
async def source_breakdown_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Signals organized by source device/connector."""
    return {"sources": get_source_breakdown(auth.tenant_id)}


# ─── FINANCIAL ACCOUNTS & NET WORTH ──────────────────────────────────────────

@router.get("/financial/accounts")
async def list_financial_accounts(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List all financial accounts."""
    from storage.financial.accounts import list_accounts
    accounts = list_accounts(auth.tenant_id)
    return {"accounts": accounts, "count": len(accounts)}


@router.post("/financial/accounts")
async def create_financial_account(
    body: dict,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Create or update a financial account."""
    from storage.financial.accounts import upsert_account
    account = upsert_account(
        tenant_id=auth.tenant_id,
        name=body.get("name", ""),
        account_type=body.get("account_type", "checking"),
        institution=body.get("institution"),
        account_number_masked=body.get("account_number_masked"),
        current_balance=body.get("current_balance"),
        credit_limit=body.get("credit_limit"),
        interest_rate=body.get("interest_rate"),
    )
    return JSONResponse(status_code=201, content=account)


@router.patch("/financial/accounts/{account_id}")
async def update_financial_account(
    account_id: str,
    body: dict,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Update a financial account."""
    from storage.financial.accounts import update_account
    ok = update_account(account_id, auth.tenant_id, body)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "ACCOUNT.NOT_FOUND"})
    return {"account_id": account_id, "updated": True}


@router.get("/financial/net-worth")
async def net_worth_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Calculate net worth from accounts + assets."""
    from storage.financial.accounts import calculate_net_worth
    return calculate_net_worth(auth.tenant_id)


@router.get("/financial/recurring")
async def recurring_transactions_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List detected recurring transactions."""
    from storage.financial.accounts import list_recurring
    recurring = list_recurring(auth.tenant_id)
    return {"recurring": recurring, "count": len(recurring)}


@router.post("/financial/recurring/detect")
async def detect_recurring_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Run recurring transaction detection on historical data."""
    from storage.financial.accounts import detect_recurring
    results = detect_recurring(auth.tenant_id)
    return {"detected": results, "count": len(results)}


@router.get("/financial/accounts/{account_id}/balance-history")
async def balance_history_endpoint(
    account_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    days: int = Query(365, le=3650),
):
    """Get balance history for an account."""
    from storage.financial.accounts import get_balance_history
    history = get_balance_history(account_id, auth.tenant_id, days)
    return {"account_id": account_id, "history": history, "count": len(history)}


@router.get("/financial/summary")
async def financial_summary_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    days: int = Query(30, le=365),
):
    """
    Financial dashboard summary — spending totals, category breakdown, trends.
    Powers the financial widgets on the dashboard.
    """
    from storage.postgres.db import get_connection
    from datetime import datetime, timezone, timedelta

    since = datetime.now(timezone.utc) - timedelta(days=days)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Total spending and income
                cur.execute("""
                    SELECT
                        COUNT(*) as transaction_count,
                        COALESCE(SUM(CASE WHEN (payload->>'amount')::numeric < 0
                            THEN (payload->>'amount')::numeric ELSE 0 END), 0) as total_debits,
                        COALESCE(SUM(CASE WHEN (payload->>'amount')::numeric > 0
                            THEN (payload->>'amount')::numeric ELSE 0 END), 0) as total_credits
                    FROM signals
                    WHERE tenant_id = %s
                      AND signal_type = 'signal.financial.transaction'
                      AND timestamp >= %s
                      AND payload->>'_parent_signal_id' IS NOT NULL
                """, (auth.tenant_id, since))
                row = cur.fetchone()
                transaction_count = row[0] or 0
                total_debits = round(float(row[1] or 0), 2)
                total_credits = round(float(row[2] or 0), 2)

                # Category breakdown
                cur.execute("""
                    SELECT
                        COALESCE(payload->>'category', 'Uncategorized') as category,
                        COUNT(*) as count,
                        COALESCE(SUM((payload->>'amount')::numeric), 0) as total
                    FROM signals
                    WHERE tenant_id = %s
                      AND signal_type = 'signal.financial.transaction'
                      AND timestamp >= %s
                      AND payload->>'_parent_signal_id' IS NOT NULL
                    GROUP BY payload->>'category'
                    ORDER BY total ASC
                    LIMIT 20
                """, (auth.tenant_id, since))
                categories = [
                    {"category": r[0], "count": r[1], "total": round(float(r[2]), 2)}
                    for r in cur.fetchall()
                ]

                # Daily spending trend (last N days)
                cur.execute("""
                    SELECT
                        DATE(timestamp) as day,
                        COALESCE(SUM(CASE WHEN (payload->>'amount')::numeric < 0
                            THEN ABS((payload->>'amount')::numeric) ELSE 0 END), 0) as spent
                    FROM signals
                    WHERE tenant_id = %s
                      AND signal_type = 'signal.financial.transaction'
                      AND timestamp >= %s
                      AND payload->>'_parent_signal_id' IS NOT NULL
                    GROUP BY DATE(timestamp)
                    ORDER BY day
                """, (auth.tenant_id, since))
                daily_trend = [
                    {"date": str(r[0]), "spent": round(float(r[1]), 2)}
                    for r in cur.fetchall()
                ]

                # Top merchants by spend
                cur.execute("""
                    SELECT
                        COALESCE(payload->>'description', 'Unknown') as merchant,
                        COUNT(*) as count,
                        COALESCE(SUM(ABS((payload->>'amount')::numeric)), 0) as total
                    FROM signals
                    WHERE tenant_id = %s
                      AND signal_type = 'signal.financial.transaction'
                      AND timestamp >= %s
                      AND (payload->>'amount')::numeric < 0
                      AND payload->>'_parent_signal_id' IS NOT NULL
                    GROUP BY payload->>'description'
                    ORDER BY total DESC
                    LIMIT 10
                """, (auth.tenant_id, since))
                top_merchants = [
                    {"merchant": r[0], "count": r[1], "total": round(float(r[2]), 2)}
                    for r in cur.fetchall()
                ]

        return {
            "period_days": days,
            "transaction_count": transaction_count,
            "total_debits": total_debits,
            "total_credits": total_credits,
            "net": round(total_credits + total_debits, 2),
            "categories": categories,
            "daily_trend": daily_trend,
            "top_merchants": top_merchants,
        }
    except Exception as e:
        # Return empty summary if no financial data yet
        return {
            "period_days": days,
            "transaction_count": 0,
            "total_debits": 0,
            "total_credits": 0,
            "net": 0,
            "categories": [],
            "daily_trend": [],
            "top_merchants": [],
        }


@router.get("/organize/timeline")
async def timeline_endpoint(
    hours: int = Query(24, le=720),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Signal volume over time (for charts)."""
    return {"timeline": get_timeline_view(auth.tenant_id, hours)}
