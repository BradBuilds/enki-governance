"""
Intelligence Brief Generator.
Queries recent signals, alerts, cases, watch topics and uses AI to generate a natural language briefing.
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta

log = logging.getLogger("enki.reports.generator")


def generate_intelligence_brief(
    tenant_id: str,
    topics: list[str] = None,
    include_alerts: bool = True,
    include_cases: bool = True,
    include_signals: bool = True,
    dsn: str = None,
) -> dict:
    """Generate an intelligence brief from recent system activity."""
    from storage.postgres.db import get_connection
    from core.ai.config import get_ai_config
    from core.ai.local_provider import LocalProvider
    from core.ai.cloud_provider import get_cloud_provider

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    sections = []
    summary_data = {}

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Recent alerts
            if include_alerts:
                cur.execute("""
                    SELECT alert_id, title, severity, status, created_at
                    FROM alerts WHERE tenant_id = %s AND created_at > %s
                    ORDER BY created_at DESC LIMIT 20
                """, (tenant_id, since))
                alerts = [{"id": r[0], "title": r[1], "severity": r[2], "status": r[3],
                          "time": r[4].isoformat() if r[4] else None} for r in cur.fetchall()]
                summary_data["alerts"] = alerts
                if alerts:
                    by_sev = {}
                    for a in alerts:
                        by_sev[a["severity"]] = by_sev.get(a["severity"], 0) + 1
                    sections.append({
                        "heading": "Alerts",
                        "body": f"{len(alerts)} alerts in the last 24 hours. " +
                                ", ".join(f"{c} {s}" for s, c in by_sev.items()) + ".",
                        "items": [f"[{a['severity'].upper()}] {a['title']}" for a in alerts[:5]],
                    })

            # Active cases
            if include_cases:
                cur.execute("""
                    SELECT case_id, title, status, priority, updated_at
                    FROM cases WHERE tenant_id = %s AND status IN ('open', 'investigating')
                    ORDER BY updated_at DESC LIMIT 10
                """, (tenant_id,))
                cases = [{"id": r[0], "title": r[1], "status": r[2], "priority": r[3],
                         "updated": r[4].isoformat() if r[4] else None} for r in cur.fetchall()]
                summary_data["cases"] = cases
                if cases:
                    sections.append({
                        "heading": "Active Cases",
                        "body": f"{len(cases)} active cases. " +
                                f"{sum(1 for c in cases if c['status'] == 'investigating')} investigating, " +
                                f"{sum(1 for c in cases if c['status'] == 'open')} open.",
                        "items": [f"[{c['priority']}] {c['title']} ({c['status']})" for c in cases[:5]],
                    })

            # Signal volume
            if include_signals:
                cur.execute("""
                    SELECT signal_type, COUNT(*) FROM signals
                    WHERE tenant_id = %s AND timestamp > %s
                    GROUP BY signal_type ORDER BY COUNT(*) DESC LIMIT 10
                """, (tenant_id, since))
                sig_types = [{"type": r[0], "count": r[1]} for r in cur.fetchall()]
                total = sum(s["count"] for s in sig_types)
                summary_data["signals"] = {"total": total, "by_type": sig_types}
                if sig_types:
                    sections.append({
                        "heading": "Signal Activity",
                        "body": f"{total} signals ingested in the last 24 hours across {len(sig_types)} types.",
                        "items": [f"{s['type']}: {s['count']}" for s in sig_types[:5]],
                    })

            # Knowledge nodes created
            cur.execute("""
                SELECT COUNT(*) FROM knowledge_nodes
                WHERE tenant_id = %s AND created_at > %s
            """, (tenant_id, since))
            node_count = cur.fetchone()[0] or 0
            if node_count > 0:
                sections.append({
                    "heading": "Knowledge Graph",
                    "body": f"{node_count} new knowledge nodes created in the last 24 hours.",
                    "items": [],
                })

    # Try to generate AI summary
    ai_summary = None
    try:
        config = get_ai_config(tenant_id, dsn)
        if config:
            prompt = f"Generate a concise intelligence briefing summary (3-5 sentences) from this data:\n\n"
            for sec in sections:
                prompt += f"## {sec['heading']}\n{sec['body']}\n"
                for item in sec.get("items", []):
                    prompt += f"- {item}\n"
                prompt += "\n"
            prompt += "Write a brief executive summary highlighting the most important developments and any patterns."

            provider_name = config.get("provider", "ollama")
            model = config.get("model", "")
            if provider_name == "ollama":
                p = LocalProvider(model=model)
                result = p.chat([{"role": "user", "content": prompt}])
                ai_summary = result.get("response", "")
            else:
                p = get_cloud_provider(provider_name, config.get("api_key", ""), model=model)
                if p:
                    result = p.chat([{"role": "user", "content": prompt}])
                    ai_summary = result.get("response", "")
    except Exception as e:
        log.warning(f"AI summary generation failed: {e}")

    # Store report
    report_id = f"rpt_{uuid.uuid4().hex[:12]}"
    title = f"Intelligence Brief — {now.strftime('%B %d, %Y %H:%M')}"
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO reports (report_id, tenant_id, title, report_type, generated_at,
                        sections, summary, section_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (report_id, tenant_id, title, "intelligence_brief", now,
                      str(sections), ai_summary, len(sections)))
                conn.commit()
    except Exception as e:
        log.warning(f"Failed to store report: {e}")

    return {
        "report_id": report_id,
        "title": title,
        "generated_at": now.isoformat(),
        "sections": sections,
        "summary": ai_summary,
        "data": summary_data,
    }
