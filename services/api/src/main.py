"""
ENKI Command Center — Unified API Server

Single entry point for every profile. The active operating posture is
controlled by ONE environment variable: ENKI_PROFILE. See docs/PROFILES.md
and core/profile/spec.py for the canonical definitions.

Profiles:
  dev       — local development; no auth, header-based identity
  cloud     — cloud node; Logto OIDC + software root key
  hardware  — on-prem; HWKEY required, local password auth, LUKS
  hardened  — on-prem; HWKEY required, Logto for users, LUKS
  hub       — control plane only; marketing + fleet, no customer data

The server runs the profile validator at boot and refuses to start if any
required dependency is missing. There is no AUTH_MODE / HARDWARE_PROFILE
to set independently — picking a profile decides everything.

Usage:
    # Dev mode (Docker compose default)
    ENKI_PROFILE=dev uvicorn services.api.src.main:app --host 0.0.0.0 --port 8000

    # Hardware profile
    ENKI_PROFILE=hardware uvicorn services.api.src.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
import time
import traceback
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ─── Profile resolution ──────────────────────────────────────────────────────
# Load persistent server config if present, then resolve the profile.
# The profile decides everything: which auth paths are valid, whether Logto
# is required, whether HWKEY is required, etc.

_server_env = os.environ.get("ENKI_CONFIG_DIR", "/etc/enki")
_server_env_path = os.path.join(_server_env, "server.env")
if os.path.isfile(_server_env_path):
    with open(_server_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# Resolve once at import time. Any failure here is a hard error — server
# will not start. This is intentional: a misconfigured profile is a security
# bug, not a "warn and continue" situation.
from core.profile import (  # noqa: E402
    resolve_profile,
    Profile,
    ProfileResolutionError,
    ProfileValidationError,
    assert_valid,
)

try:
    PROFILE, PROFILE_SPEC = resolve_profile()
except ProfileResolutionError as _e:
    print(f"FATAL: {_e}", file=sys.stderr)
    sys.exit(1)

PROFILE_LABEL = PROFILE.value.upper()

# ─── Route imports ────────────────────────────────────────────────────────────
# All routes are always imported. Auth is handled per-route via Depends().

from services.api.src.routes.health import router as health_router
from services.api.src.routes.governance import router as governance_router
from services.api.src.routes.validation import router as validation_router
from services.api.src.routes.p2p import router as p2p_router
from services.api.src.routes.public_library import router as public_library_router
from services.api.src.routes.contribute import router as contribute_router
from services.api.src.routes.public_explore import router as public_explore_router
from services.api.src.routes.library_notes import router as library_notes_router
from services.api.src.routes.news_heatmap import router as news_heatmap_router
from services.api.src.routes.gdelt import router as gdelt_router
from services.api.src.routes.watch_dashboard import router as watch_dashboard_router
from services.api.src.routes.signals import router as signals_router
from services.api.src.routes.cases import router as cases_router
from services.api.src.routes.detection import router as detection_router
from services.api.src.routes.connectors import router as connectors_router
from services.api.src.routes.ai import router as ai_router
from services.api.src.routes.gallery import router as gallery_router
from services.api.src.routes.knowledge import router as knowledge_router
from services.api.src.routes.profiles import router as profiles_router
from services.api.src.routes.invite import router as invite_router
from services.api.src.routes.card_templates import router as card_templates_router
from services.api.src.routes.processing import router as processing_router
from services.api.src.routes.workers import router as workers_router
from services.api.src.routes.geo import router as geo_router
from services.api.src.routes.tracks import router as tracks_router
from services.api.src.routes.geofences import router as geofences_router
from services.api.src.routes.webhooks import router as webhooks_router
from services.api.src.routes.knowledge_geo import router as knowledge_geo_router
from services.api.src.routes.annotations import router as annotations_router
from services.api.src.routes.devices import router as devices_router
from services.api.src.routes.recipes import router as recipes_router
from services.api.src.routes.downloads import router as downloads_router
from services.api.src.routes.exports import router as exports_router
from services.api.src.routes.secrets import router as secrets_router
from services.api.src.routes.satellite import router as satellite_router
from services.api.src.routes.refdata import router as refdata_router
from services.api.src.routes.documents import router as documents_router
from services.api.src.routes.onboarding import router as onboarding_router
from services.api.src.routes.feed import router as feed_router
from services.api.src.routes.feed_graph import router as feed_graph_router
from services.api.src.routes.feed_map import router as feed_map_router
from services.api.src.routes.feed_facets import router as feed_facets_router
from services.api.src.routes.sources import router as sources_router
from services.api.src.routes.hub_layout import router as hub_layout_router
from services.api.src.routes.tenant_settings import router as tenant_settings_router
from services.api.src.routes.tts import router as tts_router
from services.api.src.routes.live import router as live_router
from services.api.src.routes.thumbnails import router as thumbnails_router
from services.api.src.routes.cameras import router as cameras_router
from services.api.src.routes.reference import router as reference_router
from services.api.src.routes.space import router as space_router
from services.api.src.routes.reference_layers import router as reference_layers_router
from services.api.src.routes.setup import router as setup_router
from services.api.src.routes.intelligence_setup import router as intel_setup_router
from services.api.src.routes.wizard import router as wizard_router
from services.api.src.routes.fleet import router as fleet_router
from services.api.src.auth_local import router as auth_local_router
from services.api.src.routes.threads import router as threads_router
from services.api.src.routes.providers import router as providers_router
from services.api.src.routes.entity_profile import router as entity_profile_router
from services.api.src.routes.media import router as media_router
from services.api.src.routes.cards_api import router as cards_api_router
from services.api.src.routes.library_dossier import router as library_dossier_router
from services.api.src.routes.catalog import router as catalog_router
from services.api.src.routes.chat_ask import router as chat_ask_router
from services.api.src.routes.overrides import router as overrides_router

# Production-only routes (device auth endpoints)
from services.api.src.routes.admin import router as admin_router
from services.api.src.routes.ingest import router as ingest_router
from services.api.src.routes.query import router as query_router
from services.api.src.routes.receipts import router as receipts_router
from services.api.src.routes.audit import router as audit_router
from services.api.src.routes.chain import router as chain_router
from services.api.src.routes.artifacts import router as artifacts_router
from services.api.src.routes.access_log import router as access_log_router
from services.api.src.routes.worker_activity import router as worker_activity_router
from services.api.src.routes.chain_verify import router as chain_verify_router
from services.api.src.routes.correlate import router as correlate_router
from services.api.src.routes.node_refresh import router as node_refresh_router
from services.api.src.routes.permissions import router as permissions_router


# ─── Trace Logger ─────────────────────────────────────────────────────────────

trace_log = logging.getLogger("enki.trace")
trace_log.setLevel(logging.DEBUG)

if not trace_log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    trace_log.addHandler(handler)
    trace_log.propagate = False


# ─── Foreground pulse (debounced governor hint) ───────────────────────────────
# The learning governor throttles background workers when the user is actively
# using the UI. We tell it about activity by upserting learning_pulse on each
# non-health request — debounced via an in-memory timestamp so we write to the
# DB at most once every 3 seconds.
_FOREGROUND_PULSE_DEBOUNCE_SEC = 3.0
_last_pulse_ts = 0.0

def _pulse_foreground_if_due() -> None:
    global _last_pulse_ts
    now = time.monotonic()
    if now - _last_pulse_ts < _FOREGROUND_PULSE_DEBOUNCE_SEC:
        return
    _last_pulse_ts = now
    try:
        from core.learning.governor import Governor
        Governor.notice_foreground()
    except Exception:
        pass


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    mode_label = PROFILE_LABEL

    # 0. Profile validator — fail-closed if profile requirements aren't met.
    # This is the single chokepoint where ENKI_PROFILE gains real meaning.
    try:
        results = assert_valid(PROFILE, PROFILE_SPEC)
        trace_log.info(f"[{mode_label}] Profile {PROFILE.value} validated:")
        for _r in results:
            trace_log.info(f"[{mode_label}]   {_r}")
    except ProfileValidationError as _e:
        # Print every check (passed and failed) so the operator can see
        # exactly what's missing.
        trace_log.error(f"[{mode_label}] Profile {PROFILE.value} FAILED VALIDATION:")
        for _r in _e.results:
            trace_log.error(f"[{mode_label}]   {_r}")
        trace_log.error(f"[{mode_label}] See docs/PROFILES.md for required setup.")
        raise

    # 0b. Profile-specific credential warnings
    _warnings: list[str] = []
    if PROFILE_SPEC.requires_logto:
        _cookie_key = os.environ.get("LOGTO_COOKIE_ENCRYPTION_KEY", "")
        if "change-in-prod" in _cookie_key or "dev" in _cookie_key:
            _warnings.append("LOGTO_COOKIE_ENCRYPTION_KEY contains dev placeholder — rotate it")
        if not os.environ.get("LOGTO_APP_ID"):
            _warnings.append("LOGTO_APP_ID not set — JWT audience verification disabled")
    if PROFILE_SPEC.allows_emulated_root_key and not PROFILE_SPEC.requires_hardware_key:
        if not os.environ.get("ENKI_ROOT_PASSWORD"):
            _warnings.append("ENKI_ROOT_PASSWORD not set — emulated root key operations will fail")
    _db_pass = os.environ.get("POSTGRES_PASSWORD", "")
    if PROFILE != Profile.DEV and _db_pass in ("", "enki_pass", "CHANGE_ME_generate_with_openssl_rand_base64_48"):
        _warnings.append("POSTGRES_PASSWORD is weak or default — change it immediately")
    for _w in _warnings:
        trace_log.error(f"[SECURITY] {_w}")
    if _warnings:
        trace_log.error(
            "[SECURITY] %d credential issue(s) detected. "
            "Fix these before exposing this server to the internet.", len(_warnings)
        )

    # 1. Database schema
    from storage.postgres.db import initialize_schema, initialize_pool, close_pool
    try:
        initialize_schema()
        trace_log.info(f"[{mode_label}] Database schema initialized.")
    except Exception as e:
        trace_log.warning(f"[{mode_label}] Schema init: {e}")

    # 2. Connection pool
    try:
        initialize_pool()
        trace_log.info(f"[{mode_label}] Connection pool initialized.")
    except Exception as e:
        trace_log.warning(f"[{mode_label}] Pool init: {e}")

    # 3. Device auth stores (every profile except pure DEV needs them).
    # Even DEV initializes if the keys are present, so device-paired tools
    # can sign requests during local testing.
    from pathlib import Path as _AuthPath
    from storage.auth.config import GRANTS_DIR, ROOT_KEY_PATH, ROOT_KEY_PASSWORD
    # DEV and NODE_LOCAL: device auth is OPTIONAL. Set up if the encrypted
    # root key + password are present, skip cleanly otherwise. Pulse mobile
    # device pairing won't work without a properly-installed root key, but
    # the web login flow (/auth/local/*) doesn't depend on it.
    # Other profiles: device auth is REQUIRED.
    _device_auth_optional_profiles = {Profile.DEV, Profile.NODE_LOCAL}
    _need_device_auth = (
        PROFILE not in _device_auth_optional_profiles
        or (_AuthPath(ROOT_KEY_PATH).exists() and bool(ROOT_KEY_PASSWORD))
    )
    if _need_device_auth:
        try:
            from storage.auth.root_key import load_root_key
            from storage.auth.grant_store import GrantStore
            from storage.auth.challenge_store import InMemoryChallengeStore, PostgresChallengeStore

            grant_store = GrantStore(GRANTS_DIR)
            try:
                challenge_store = PostgresChallengeStore()
                trace_log.info(f"[{mode_label}] Using PostgreSQL challenge store.")
            except Exception:
                challenge_store = InMemoryChallengeStore()
                trace_log.warning(f"[{mode_label}] Falling back to in-memory challenge store.")

            # Stash challenge_store + grant_store + profile immediately. They are
            # independent of root_key, and device-signing endpoints (Pulse mobile
            # + desktop) need them even if root_key load later fails.
            app.state.grant_store = grant_store
            app.state.challenge_store = challenge_store
            app.state.profile = PROFILE
            app.state.profile_spec = PROFILE_SPEC

            # Hardware/hardened: use HardwareKeyAdapter; software root is forbidden.
            # Cloud/hub: use software root key.
            if PROFILE_SPEC.requires_hardware_key:
                from core.profile.hardware_adapter import load_hardware_adapter
                root_key = load_hardware_adapter()
            else:
                if not ROOT_KEY_PASSWORD:
                    raise RuntimeError(
                        "ROOT_KEY_PASSWORD is not set. "
                        "Set ENKI_ROOT_KEY_PASSWORD_ENV and the referenced env var."
                    )
                root_key = load_root_key(ROOT_KEY_PATH, ROOT_KEY_PASSWORD)

            app.state.root_key = root_key
            app.state.root_public_key_raw = root_key.public_key_bytes_raw()

            trace_log.info(f"[{mode_label}] Device auth stores initialized.")
        except Exception as e:
            trace_log.error(f"[{mode_label}] Device auth init FAILED: {e}")
            # DEV + NODE_LOCAL: soft-fail so the app still boots if device
            # auth keys aren't installed. The web login still works.
            # Other profiles (NODE, NODE_PUBLIC, EVIDENTIARY, HARDENED): fail
            # closed since they're production-grade.
            if PROFILE not in _device_auth_optional_profiles:
                raise
            app.state.profile = PROFILE
            app.state.profile_spec = PROFILE_SPEC
    else:
        # DEV: still expose profile so routes can introspect
        app.state.profile = PROFILE
        app.state.profile_spec = PROFILE_SPEC

    # 4. GDU Governance Context (loads active set + verifies pack integrity)
    try:
        from core.gdu.signal_vocabulary import load_signal_types_via_gdu
        types, canonical, verification = load_signal_types_via_gdu()
        app.state.gdu_canonical = canonical
        app.state.gdu_verification = verification
        if canonical:
            trace_log.info(
                f"[{mode_label}] GDU: signalvocab loaded CANONICALLY — "
                f"{len(types)} types, active set v{verification.get('active_set_version', '?')}, "
                f"hash verified."
            )
        else:
            # Loud, unmistakable banner — degraded governance is a serious operational
            # condition and must not blend into ordinary log noise. Every line is logged
            # at ERROR level so it survives any log filter that drops warnings.
            banner = "=" * 78
            trace_log.error(banner)
            trace_log.error(f"[{mode_label}] GOVERNANCE DEGRADED — GDU NOT CANONICAL")
            trace_log.error(banner)
            trace_log.error(f"[{mode_label}]   method:        {verification.get('method', '?')}")
            trace_log.error(f"[{mode_label}]   active_set:    {verification.get('active_set_version', '?')}")
            trace_log.error(f"[{mode_label}]   hash_verified: {verification.get('pack_hash_verified', False)}")
            trace_log.error(f"[{mode_label}]   types_loaded:  {len(types)}")
            trace_log.error(f"[{mode_label}]   error:         {verification.get('error', 'none')}")
            trace_log.error(f"[{mode_label}] Signals admitted while degraded carry gdu_canonical=false")
            trace_log.error(f"[{mode_label}] Run: python3 scripts/governance/verify_pack_set.py")
            trace_log.error(banner)
            # Hard-stop when strict mode is requested (set ENKI_GDU_STRICT=1
            # in enki.conf on production deployments). In dev we keep the
            # warn-and-continue behaviour so vocab iteration isn't blocked
            # by a stale active_set.
            if os.getenv("ENKI_GDU_STRICT", "").lower() in ("1", "true", "yes"):
                raise RuntimeError(
                    "GDU not canonical and ENKI_GDU_STRICT is set — refusing to boot. "
                    "Run the admission ceremony (scripts/governance/run_admission_ceremony.py) "
                    "or unset ENKI_GDU_STRICT."
                )
    except Exception as e:
        # GDU init exception path — even louder, since this means we couldn't
        # even reach the fallback. Service may still come up but signals will be
        # admitted under no governance. This is a code-red operational state.
        banner = "#" * 78
        trace_log.error(banner)
        trace_log.error(f"[{mode_label}] GOVERNANCE INIT FAILED — GDU UNREACHABLE")
        trace_log.error(banner)
        trace_log.error(f"[{mode_label}]   exception: {type(e).__name__}: {e}")
        trace_log.error(f"[{mode_label}] Service starting WITHOUT verified vocabulary.")
        trace_log.error(f"[{mode_label}] Investigate immediately — check active_set_v5.json and libraries/")
        trace_log.error(banner)
        app.state.gdu_canonical = False
        app.state.gdu_verification = {"error": str(e)}

    # 4a. Sync pack registry indices into SQL — required by migration 032's
    # mirror triggers + gdu_assign_address function. Runs after the pack
    # registry has loaded so we mirror the active state. Skip if migration
    # 032 hasn't been applied yet (sync is a no-op).
    try:
        from core.gdu.sql_pack_sync import sync_pack_positions_to_sql
        from storage.postgres.db import get_connection as _get_gdu_conn
        with _get_gdu_conn() as _conn:
            _result = sync_pack_positions_to_sql(_conn, log_summary=False)
            _conn.commit()
        if _result.get("skipped"):
            trace_log.warning(
                f"[{mode_label}] gdu_pack_positions table missing — "
                f"apply migration 032 to enable SQL-level GDU enforcement."
            )
        else:
            trace_log.info(
                f"[{mode_label}] gdu_pack_positions synced: "
                f"{_result['rows_written']} entries, pack={_result['pack_version']}"
            )
    except Exception as _e:
        trace_log.warning(f"[{mode_label}] pack-position SQL sync skipped: {_e}")

    # 4a-2. Edgevocab canonical load — register edge-type constraints + the
    # predicate dictionary (verb -> edge_type) at boot so assessment promotion
    # is never silently empty. Idempotent upserts; fast (seconds).
    try:
        from scripts.load_edge_type_constraints import main as _load_edges
        from scripts.load_predicate_seeds import main as _load_preds
        _er = _load_edges()
        _pr = _load_preds()
        trace_log.info(f"[{mode_label}] GDU edgevocab loaded: edge_types rc={_er}, predicate_seeds rc={_pr}.")
    except Exception as _e:
        trace_log.warning(f"[{mode_label}] edgevocab load skipped: {_e}")

    # 4b. GDU Integrity Audit — count ungoverned nodes at boot
    try:
        from storage.postgres.db import get_connection as _get_gdu_conn
        with _get_gdu_conn() as _gdu_conn:
            with _gdu_conn.cursor() as _gdu_cur:
                _gdu_cur.execute(
                    "SELECT COUNT(*) FROM knowledge_nodes WHERE gdu_canonical IS NULL OR gdu_canonical = FALSE"
                )
                _ungov = _gdu_cur.fetchone()[0]
                _gdu_cur.execute(
                    "SELECT COUNT(*) FROM knowledge_nodes WHERE base60_address IS NULL"
                )
                _no_addr = _gdu_cur.fetchone()[0]
                _gdu_cur.execute(
                    "SELECT COUNT(*) FROM knowledge_nodes"
                )
                _total = _gdu_cur.fetchone()[0]
        if _ungov > 0 or _no_addr > 0:
            trace_log.warning(
                f"[{mode_label}] GDU AUDIT: {_total} total nodes, "
                f"{_ungov} ungoverned (no gdu_canonical), "
                f"{_no_addr} without base-60 address"
            )
        else:
            trace_log.info(
                f"[{mode_label}] GDU AUDIT: {_total} nodes, all governed with addresses"
            )
    except Exception as _gdu_audit_e:
        trace_log.debug(f"[{mode_label}] GDU audit skipped: {_gdu_audit_e}")

    # Investigation worker is ALWAYS on — it drains USER-TRIGGERED public-record
    # research (the Research / Research-all-companies buttons), which is separate from
    # the collapse pipeline that ENKI_DISABLE_BACKGROUND_WORKERS seals. Idempotent.
    # It PACES itself (one investigation per PACE_SECONDS) and yields under load /
    # to the manager, so it trickles the queue without hitting API or GPU limits —
    # the GPU passes back and forth via Ollama's natural request serialization.
    try:
        from core.workers.investigation import start_worker as _start_investigation_always
        _start_investigation_always("default")
        trace_log.info(f"[{mode_label}] Investigation worker started (always-on, paced user research).")
    except Exception as e:
        trace_log.warning(f"[{mode_label}] Investigation worker (always-on): {e}")

    # Entity cleanup runs always-on too, trickling BEHIND discovery: it validates
    # thin entities or parks unambiguous junk (OCR/manifest names) in the LDU for
    # review — the self-healing half of discovery. Paced + yields the GPU, same as
    # the investigation worker.
    try:
        from core.workers.entity_cleanup import start_worker as _start_cleanup_always
        _start_cleanup_always("default")
        trace_log.info(f"[{mode_label}] Entity cleanup worker started (always-on, paced validate-or-park).")
    except Exception as e:
        trace_log.warning(f"[{mode_label}] Entity cleanup worker (always-on): {e}")

    # 5. Background workers — gated by ENKI_DISABLE_BACKGROUND_WORKERS.
    # Federation / controlled-collapse mode: a node mining or collapsing a SHARED
    # federation envelope must NEVER auto-re-extract or re-collapse it (the locked
    # model — collapse is a deterministic function of content; the LLM is fenced and
    # the envelope is shared, not re-derived). When the flag is set, NONE of the
    # background workers, the supervisor, or the learning governor start, so the
    # sealed deterministic graph is served read-only without mutation. Unset
    # (normal production) → everything starts as before.
    if os.getenv("ENKI_DISABLE_BACKGROUND_WORKERS", "").strip().lower() in ("1", "true", "yes"):
        trace_log.info(
            f"[{mode_label}] Background workers + supervisor + learning governor "
            f"DISABLED (ENKI_DISABLE_BACKGROUND_WORKERS) — controlled-collapse / federation mode."
        )
        # -- Acquisition + health daemons run EVEN under the collapse seal --
        # These are deterministic and do NOT re-derive the sealed graph:
        #   * discovery_continuous: acquire pending refs -> ingest PDFs to
        #     paragraphs + library_members. No LLM, no collapse. Round-robins
        #     ALL active libraries (drains the backlog), focus-aware if a run
        #     is active. Keeps mining running across topics.
        #   * manager: pressure-aware health monitor (Ollama/GPU/load). Only
        #     gates workers; under the seal there are none to un-gate, so it is
        #     a safe observability thread.
        # Governed LLM collapse stays sealed and runs only on the cron path.
        if os.getenv("ENKI_ENABLE_ACQUISITION_DAEMONS", "1").strip().lower() in ("1", "true", "yes"):
            try:
                from core.workers.discovery_continuous import start_worker as _start_disc
                _start_disc("default")
                trace_log.info(f"[{mode_label}] Discovery continuous started (sealed-mode acquisition drainer).")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Discovery continuous (sealed): {e}")
            try:
                from core.workers.manager import start_manager as _start_mgr
                _start_mgr("default")
                trace_log.info(f"[{mode_label}] Worker manager started (health/pressure monitor).")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Worker manager (sealed): {e}")
    else:
        # 5. Background workers
        try:
            from core.workers.runner import start_workers
            start_workers("default")
            trace_log.info(f"[{mode_label}] Background workers started.")
            from core.connectors.scheduler import start_connector_scheduler
            start_connector_scheduler("default")
            trace_log.info(f"[{mode_label}] Connector scheduler started.")
            try:
                from core.workers.reconciliation import start_reconciliation_worker
                start_reconciliation_worker("default")
                trace_log.info(f"[{mode_label}] Reconciliation worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Reconciliation worker: {e}")
    
            try:
                from core.workers.ldup_promoter import start_ldup_promoter
                start_ldup_promoter("default")
                trace_log.info(f"[{mode_label}] LDU-P promoter worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] LDU-P promoter: {e}")
    
            try:
                from core.workers.ldu_collapser import start_ldu_collapser
                start_ldu_collapser("default")
                trace_log.info(f"[{mode_label}] LDU collapser worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] LDU collapser: {e}")
    
            try:
                from core.workers.ldu_promoter import start_ldu_promoter
                start_ldu_promoter("default")
                trace_log.info(f"[{mode_label}] LDU promoter worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] LDU promoter: {e}")
    
            try:
                from core.workers.ldu_cluster import start_ldu_cluster
                start_ldu_cluster("default")
                trace_log.info(f"[{mode_label}] LDU cluster worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] LDU cluster: {e}")
    
            try:
                from core.workers.divisor_governance import start_divisor_governance
                start_divisor_governance("default")
                trace_log.info(f"[{mode_label}] Divisor governance worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Divisor governance: {e}")
    
            try:
                from core.workers.library_source_connector import start_library_source_connector
                start_library_source_connector("default")
                trace_log.info(f"[{mode_label}] Library source connector worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Library source connector: {e}")
    
            try:
                from core.workers.library_work_assigner import start_library_work_assigner
                start_library_work_assigner("default")
                trace_log.info(f"[{mode_label}] Library work assigner worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Library work assigner: {e}")
    
            try:
                from core.workers.yield_accountant import start_yield_accountant
                start_yield_accountant("default")
                trace_log.info(f"[{mode_label}] Yield accountant worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Yield accountant: {e}")
    
            try:
                from core.workers.personal_linker import start_personal_linker
                start_personal_linker("default")
                trace_log.info(f"[{mode_label}] Personal linker worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Personal linker: {e}")
    
            try:
                from core.workers.chain_verifier import start_chain_verifier
                start_chain_verifier("default")
                trace_log.info(f"[{mode_label}] Chain verifier worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Chain verifier: {e}")
    
            try:
                from core.workers.llm_extraction import start_llm_worker
                start_llm_worker("default")
                trace_log.info(f"[{mode_label}] LLM extraction worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] LLM extraction worker: {e}")
    
            try:
                from core.workers.feed_enrichment import start_worker as start_feed_enrichment
                start_feed_enrichment("default")
                trace_log.info(f"[{mode_label}] Feed enrichment worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Feed enrichment worker: {e}")
    
            try:
                from core.workers.node_builder import start_worker as start_node_builder
                start_node_builder("default")
                trace_log.info(f"[{mode_label}] Node builder worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Node builder worker: {e}")
    
            try:
                from core.workers.auto_validator import start_worker as start_auto_validator
                start_auto_validator("default")
                trace_log.info(f"[{mode_label}] Auto-validator worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Auto-validator worker: {e}")
    
            try:
                from core.workers.place_validator import start_worker as start_place_validator
                start_place_validator("default")
                trace_log.info(f"[{mode_label}] Place validator worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Place validator worker: {e}")
    
            try:
                from core.workers.data_integrity_validator import start_worker as start_di_validator
                start_di_validator("default")
                trace_log.info(f"[{mode_label}] Data-integrity validator worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Data-integrity validator worker: {e}")
    
            try:
                from core.workers.assessment_validator import start_worker as start_assess_validator
                start_assess_validator("default")
                trace_log.info(f"[{mode_label}] Assessment validator worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Assessment validator worker: {e}")
    
            try:
                from core.workers.document_validator import start_worker as start_doc_validator
                start_doc_validator("default")
                trace_log.info(f"[{mode_label}] Document validator worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Document validator worker: {e}")
    
            try:
                from core.workers.event_validator import start_worker as start_event_validator
                start_event_validator("default")
                trace_log.info(f"[{mode_label}] Event validator worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Event validator worker: {e}")
    
            # Research Pipeline A — multi-source public records (SEC, FEC, courts,
            # OFAC, etc.). Was never registered before May 8 2026; entity_investigations
            # table sat empty for weeks despite the auto-trigger inserting rows.
            try:
                from core.workers.investigation import start_worker as start_investigation
                start_investigation("default")
                trace_log.info(f"[{mode_label}] Investigation worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Investigation worker: {e}")

            # Deep-research worker — drains queued research_runs (LLM-directed
            # web-search → grab → collapse → cited report). See
            # core/research/orchestrator.py.
            try:
                from core.workers.research import start_worker as start_research
                start_research("default")
                trace_log.info(f"[{mode_label}] Research worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Research worker: {e}")
    
            # Research Pipeline B — Wikipedia + Wikidata + sanctions, drains
            # entity_enrichment_queue. Was never registered; queue had 2,741 pending
            # tasks and 0 ever processed before May 8 2026.
            try:
                from core.workers.enrichment_queue_worker import start_worker as start_enrichment_queue
                start_enrichment_queue("default")
                trace_log.info(f"[{mode_label}] Enrichment queue worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Enrichment queue worker: {e}")
    
            # Phase 7c — investigation quality validator. Scores completed
            # investigations as HEALTHY/MARGINAL/LOW and flags low-value entities
            # for human review (entity_review_queue).
            try:
                from core.workers.investigation_quality_validator import start_worker as start_quality_validator
                start_quality_validator("default")
                trace_log.info(f"[{mode_label}] Investigation quality validator started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Investigation quality validator: {e}")
    
            # Phase 7d — per-finding relevance scorer. LLM-driven; compares each
            # investigation finding to its parent entity and assigns a 0.0-1.0
            # score so the dossier UI can hide low-relevance noise (e.g. the
            # "Brian David Harlow bankruptcy" returned for a Acme Widgets CEO query).
            try:
                from core.workers.finding_relevance_scorer import start_worker as start_relevance_scorer
                start_relevance_scorer("default")
                trace_log.info(f"[{mode_label}] Finding relevance scorer started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Finding relevance scorer: {e}")
    
            # Phase 3 of the pipeline-restart plan (May 23 2026). The three
            # workers below close the boundary failures from the May 22
            # diagnosis. Tier 1 — no LLM dependency, always run.
            try:
                from core.workers.investigation_sweep import start_worker as start_investigation_sweep
                start_investigation_sweep("default")
                trace_log.info(f"[{mode_label}] Investigation sweep started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Investigation sweep: {e}")
    
            try:
                from core.workers.mention_backfill import start_worker as start_mention_backfill
                start_mention_backfill("default")
                trace_log.info(f"[{mode_label}] Mention backfill started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Mention backfill: {e}")
    
            try:
                from core.workers.entity_dedup_sweep import start_worker as start_entity_dedup
                start_entity_dedup("default")
                trace_log.info(f"[{mode_label}] Entity dedup sweep started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Entity dedup sweep: {e}")
    
            # Conflict scanner v1 is DISABLED as of May 8 2026 — it produced
            # 100% false-positive noise (3,532 rows): treated multi-place
            # articles as place mismatches and different events under the same
            # entity as time mismatches. Real conflict detection needs event-
            # cluster input first; see core/learning/workers/event_clusterer.py
            # for the planned upstream. The module + start_conflict_scanner()
            # are kept for the rewrite — re-enable here when v2 is ready.
            # Wipe of legacy rows: TRUNCATE node_conflicts (May 8 2026).
            # try:
            #     from core.extraction.conflict_scanner import start_conflict_scanner
            #     start_conflict_scanner("default")
            #     trace_log.info(f"[{mode_label}] Conflict scanner started.")
            # except Exception as e:
            #     trace_log.warning(f"[{mode_label}] Conflict scanner: {e}")
    
            try:
                from core.workers.watch_derivatives import start_watch_derivatives
                start_watch_derivatives(tenant_id="default", window_label="24h",
                                         interval_seconds=900)
                trace_log.info(f"[{mode_label}] Watch derivatives worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Watch derivatives: {e}")
    
            try:
                from core.workers.mentions_writer import start_mentions_writer
                start_mentions_writer(interval_seconds=2)
                trace_log.info(f"[{mode_label}] Mentions writer worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Mentions writer: {e}")
    
            # Signal → library routing. Keyword-matches RSS signals against each
            # library's focal_term so /feed cards carry server-side library chips
            # and /library/[id] shows live-news corroboration.
            try:
                from core.workers.library_router import start_worker as start_library_router
                start_library_router("default")
                trace_log.info(f"[{mode_label}] Library router worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Library router: {e}")
    
            # Signal → statement extraction. Runs LLM extraction on RSS title +
            # first_paragraph so news contributes to the cyclical knowledge
            # model the same way PDFs do. Statements feed assessment_promoter.
            try:
                from core.workers.signal_statement_extractor import start_worker as start_signal_stmt_extractor
                start_signal_stmt_extractor("default")
                trace_log.info(f"[{mode_label}] Signal statement extractor started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Signal statement extractor: {e}")
    
            # Predicate dictionary auto-extender. Tier 1 morphological + Tier 2 LLM
            # validation. High-confidence proposals auto-promote; low-confidence
            # queue into predicate_review_queue for /governance review.
            try:
                from core.workers.predicate_extender import start_worker as start_predicate_extender
                start_predicate_extender("default")
                trace_log.info(f"[{mode_label}] Predicate extender started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Predicate extender: {e}")
    
            # Library manager — orchestrates library-as-project runs. Reads
            # library_runs to pick the current focus + advances phases. Workers
            # that scope by current focus (collapse_worker for now) get prioritized
            # work when a project run is active.
            try:
                from core.workers.library_manager import start_worker as start_library_manager
                start_library_manager("default")
                trace_log.info(f"[{mode_label}] Library manager started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Library manager: {e}")
    
            # Discovery continuous — acquire pending refs + ingest acquired PDFs.
            # Focus-aware: prioritizes the library_manager's current run.
            # Without this worker, nothing drives the acquire/ingest pipeline
            # on a cadence — refs sit pending and acquired PDFs sit on disk.
            try:
                from core.workers.discovery_continuous import start_worker as start_discovery_continuous
                start_discovery_continuous("default")
                trace_log.info(f"[{mode_label}] Discovery continuous started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Discovery continuous: {e}")
    
            # Collapse worker — paragraph → mention → statement → promotion.
            # Runs every 30s, processes up to 3 docs/tick. THE LLM-feeding loop.
            # Without this, ingest fills the paragraph backlog forever without
            # extracting anything. Was missing from FastAPI startup until now.
            try:
                from core.workers.collapse_worker import start_worker as start_collapse_worker
                start_collapse_worker("default")
                trace_log.info(f"[{mode_label}] Collapse worker started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Collapse worker: {e}")
    
            # Topic auto-clusterer — proposes topics from signal_clusters,
            # libraries, and high-signal entities every 5 minutes.
            try:
                from core.workers.topic_auto_clusterer import start_worker as start_topic_clusterer
                start_topic_clusterer("default")
                trace_log.info(f"[{mode_label}] Topic auto-clusterer started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Topic auto-clusterer: {e}")
    
            # Fission detector — audits the substrate every 30 min for
            # entities that need to be split (fission) or rebound (merged
            # into the correct existing entity). Catches resolver failures.
            try:
                from core.workers.fission_detector import start_worker as start_fission_detector
                start_fission_detector("default")
                trace_log.info(f"[{mode_label}] Fission detector started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Fission detector: {e}")
    
            # Federation subscriber — polls the hub for redirect propagations
            # every 15 min and applies them locally. No-op when ENKI_HUB_URL
            # is unset.
            try:
                from core.workers.federation_subscriber import start_worker as start_federation_subscriber
                start_federation_subscriber("default")
                trace_log.info(f"[{mode_label}] Federation subscriber started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Federation subscriber: {e}")
    
            # Library subscriber — walks library_mirrors every 15 min, fetches
            # upstream manifests, pulls missing /p2p/content/{sha} files into
            # the local corpus. Counterpart to library_work_assigner on the
            # publisher side. No-op when there are no active mirrors.
            try:
                from core.workers.library_subscriber import start_worker as start_library_subscriber
                start_library_subscriber("default")
                trace_log.info(f"[{mode_label}] Library subscriber started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Library subscriber: {e}")
    
            # Extraction subscriber — pulls federation extraction envelopes for
            # docs we have but haven't extracted yet, applies them with the 10
            # verification gates from the library federation plan. No-op when
            # ENKI_HUB_URL isn't set or when no docs need pulling. Race-stop
            # against the local LLM extractor lives in
            # core/documents/extractor.py:extract_from_document (checks
            # documents.extraction_state at the top of the function).
            try:
                from core.workers.extraction_subscriber import start_worker as start_extraction_subscriber
                start_extraction_subscriber("default")
                trace_log.info(f"[{mode_label}] Extraction subscriber started.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Extraction subscriber: {e}")
    
            # Resume any interrupted backfills (threads die on API restart).
            # Read the stored backfill_months from the topic — never hardcode.
            try:
                from storage.postgres.db import get_connection
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT topic_id, keywords, COALESCE(backfill_months, 0) "
                            "FROM watch_topics WHERE backfill_status = 'running'"
                        )
                        stalled = cur.fetchall()
                if stalled:
                    from core.research.backfill import start_backfill
                    for topic_id, keywords, bf_months in stalled:
                        kw_list = keywords if isinstance(keywords, list) else []
                        if kw_list and bf_months > 0:
                            start_backfill(topic_id, kw_list, {}, backfill_months=bf_months, tenant_id="default")
                            trace_log.info(f"[{mode_label}] Resumed backfill for {topic_id} ({bf_months} months)")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Backfill resume: {e}")
            from core.connectors.watch_folder import start_watch_folder
            start_watch_folder("default")
            trace_log.info(f"[{mode_label}] Watch folder daemon started.")
        except Exception as e:
            trace_log.warning(f"[{mode_label}] Worker start: {e}")
    
        # Start reference data sync scheduler
        try:
            from core.reference.scheduler import start_reference_scheduler
            start_reference_scheduler()
            trace_log.info(f"[{mode_label}] Reference data sync scheduler started.")
        except Exception as e:
            trace_log.warning(f"[{mode_label}] Reference scheduler: {e}")
    
        # Image analyst — vision LLM + face detection + OCR on image signals
        try:
            from core.workers.image_analyst import start_worker as start_image_analyst
            start_image_analyst("default")
            trace_log.info(f"[{mode_label}] Image analyst started (vision LLM).")
        except Exception as e:
            trace_log.warning(f"[{mode_label}] Image analyst: {e}")
    
        # Security analyst — LLM-powered threat analysis on security events
        try:
            from core.workers.security_analyst import start_worker as start_security_analyst
            start_security_analyst("default")
            trace_log.info(f"[{mode_label}] Security analyst started (LLM threat analysis).")
        except Exception as e:
            trace_log.warning(f"[{mode_label}] Security analyst: {e}")
    
        # Federation auth probe — log the JWT state at boot so operators
        # see immediately whether peer auth is configured. A node without
        # a JWT silently degrades (all federation calls fail), so we make
        # the state loud at startup.
        if os.getenv("ENKI_HUB_URL"):
            try:
                from core.federation.peer_identity import status as _peer_status
                ps = _peer_status()
                if ps.get("has_jwt") and ps.get("jwt_seconds_left", 0) > 0:
                    secs = ps["jwt_seconds_left"]
                    trace_log.info(
                        f"[{mode_label}] Federation peer JWT VALID — "
                        f"node={ps.get('jwt_peer_node_id')} "
                        f"roles={ps.get('jwt_roles')} "
                        f"expires_in={secs // 3600}h{(secs % 3600) // 60}m"
                    )
                elif ps.get("has_keypair"):
                    trace_log.warning(
                        f"[{mode_label}] Federation peer JWT MISSING — keypair "
                        f"present but no handshake done. Federation calls will "
                        f"fail. Run: systemctl start enki-peer-bootstrap.service "
                        f"(or `python -m core.federation.peer_identity ensure`)"
                    )
                else:
                    trace_log.warning(
                        f"[{mode_label}] Federation peer keypair MISSING. "
                        f"Generate with: python -m core.federation.peer_identity keygen "
                        f"(then bootstrap with an invitation token)"
                    )
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Federation auth probe: {e}")
    
        # Hub heartbeat — phones home to example.com if ENKI_HUB_URL is set
        if os.getenv("ENKI_HUB_URL"):
            try:
                from core.workers.hub_heartbeat import start_worker as start_hub_heartbeat
                start_hub_heartbeat("default")
                trace_log.info(f"[{mode_label}] Hub heartbeat started → {os.getenv('ENKI_HUB_URL')}")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Hub heartbeat: {e}")
    
        # Hub announce — periodically signs + POSTs library catalog so the hub
        # registry stays warm. 30-min cadence; signs each library individually.
        try:
            from core.workers.hub_announce_worker import start_worker as start_hub_announce
            start_hub_announce()
            trace_log.info(f"[{mode_label}] Hub announce worker started (30-min cadence)")
        except Exception as e:
            trace_log.warning(f"[{mode_label}] Hub announce worker: {e}")
    
        # GDELT GKG sync — pulls geo-coded news mentions every 5 min so the
        # /map global-news layer stays fresh. Tier 0: no LLM, no GPU, just a
        # 3 MB HTTP fetch and a few thousand row inserts every cycle.
        try:
            from core.workers.gdelt_gkg import start_worker as _start_gdelt
            _start_gdelt("default")
            trace_log.info(f"[{mode_label}] GDELT GKG worker started")
        except Exception as e:
            trace_log.warning(f"[{mode_label}] GDELT GKG worker: {e}")
    
        # ── Worker Supervisor — auto-restart daemon workers on failure ───
        try:
            from core.workers.supervisor import register_worker, start_supervisor
    
            # Register all daemon workers for supervision
            try:
                from core.workers.feed_enrichment import (
                    start_worker as _fe_start, stop_worker as _fe_stop, get_status as _fe_status,
                )
                register_worker("feed_enrichment", _fe_start, _fe_status, _fe_stop)
            except Exception:
                pass
    
            try:
                from core.workers.node_builder import (
                    start_worker as _nb_start, stop_worker as _nb_stop, get_status as _nb_status,
                )
                register_worker("node_builder", _nb_start, _nb_status, _nb_stop)
            except Exception:
                pass
    
            try:
                from core.workers.reconciliation import (
                    start_reconciliation_worker as _rc_start,
                    stop_reconciliation_worker as _rc_stop,
                    get_worker_status as _rc_status,
                )
                register_worker("reconciliation", _rc_start, _rc_status, _rc_stop)
            except Exception:
                pass
    
            try:
                from core.workers.ldup_promoter import (
                    start_ldup_promoter as _ldup_start,
                    stop_ldup_promoter as _ldup_stop,
                    get_ldup_promoter_status as _ldup_status,
                )
                register_worker("ldup_promoter", _ldup_start, _ldup_status, _ldup_stop)
            except Exception:
                pass
    
            try:
                from core.workers.ldu_collapser import (
                    start_ldu_collapser as _ldu_c_start,
                    stop_ldu_collapser as _ldu_c_stop,
                    get_ldu_collapser_status as _ldu_c_status,
                )
                register_worker("ldu_collapser", _ldu_c_start, _ldu_c_status, _ldu_c_stop)
            except Exception:
                pass
    
            try:
                from core.workers.ldu_promoter import (
                    start_ldu_promoter as _ldu_p_start,
                    stop_ldu_promoter as _ldu_p_stop,
                    get_ldu_promoter_status as _ldu_p_status,
                )
                register_worker("ldu_promoter", _ldu_p_start, _ldu_p_status, _ldu_p_stop)
            except Exception:
                pass
    
            try:
                from core.workers.ldu_cluster import (
                    start_ldu_cluster as _ldu_cl_start,
                    stop_ldu_cluster as _ldu_cl_stop,
                    get_ldu_cluster_status as _ldu_cl_status,
                )
                register_worker("ldu_cluster", _ldu_cl_start, _ldu_cl_status, _ldu_cl_stop)
            except Exception:
                pass
    
            try:
                from core.workers.divisor_governance import (
                    start_divisor_governance as _dg_start,
                    stop_divisor_governance as _dg_stop,
                    get_divisor_governance_status as _dg_status,
                )
                register_worker("divisor_governance", _dg_start, _dg_status, _dg_stop)
            except Exception:
                pass
    
            try:
                from core.workers.library_source_connector import (
                    start_library_source_connector as _lsc_start,
                    stop_library_source_connector as _lsc_stop,
                    get_library_source_connector_status as _lsc_status,
                )
                register_worker("library_source_connector", _lsc_start, _lsc_status, _lsc_stop)
            except Exception:
                pass
    
            try:
                from core.workers.library_work_assigner import (
                    start_library_work_assigner as _lwa_start,
                    stop_library_work_assigner as _lwa_stop,
                    get_library_work_assigner_status as _lwa_status,
                )
                register_worker("library_work_assigner", _lwa_start, _lwa_status, _lwa_stop)
            except Exception:
                pass
    
            try:
                from core.workers.yield_accountant import (
                    start_yield_accountant as _ya_start,
                    stop_yield_accountant as _ya_stop,
                    get_yield_accountant_status as _ya_status,
                )
                register_worker("yield_accountant", _ya_start, _ya_status, _ya_stop)
            except Exception:
                pass
    
            try:
                from core.workers.personal_linker import (
                    start_personal_linker as _pl_start,
                    stop_personal_linker as _pl_stop,
                    get_personal_linker_status as _pl_status,
                )
                register_worker("personal_linker", _pl_start, _pl_status, _pl_stop)
            except Exception:
                pass
    
            try:
                from core.workers.chain_verifier import (
                    start_chain_verifier as _cv_start,
                    stop_chain_verifier as _cv_stop,
                    get_chain_verifier_status as _cv_status,
                )
                register_worker("chain_verifier", _cv_start, _cv_status, _cv_stop)
            except Exception:
                pass
    
            try:
                from core.workers.auto_validator import (
                    start_worker as _av_start, stop_worker as _av_stop, get_status as _av_status,
                )
                register_worker("auto_validator", _av_start, _av_status, _av_stop)
            except Exception:
                pass
    
            try:
                from core.workers.place_validator import (
                    start_worker as _pv_start, stop_worker as _pv_stop, get_status as _pv_status,
                )
                register_worker("place_validator", _pv_start, _pv_status, _pv_stop)
            except Exception:
                pass
    
            try:
                from core.workers.assessment_validator import (
                    start_worker as _av2_start, stop_worker as _av2_stop, get_status as _av2_status,
                )
                register_worker("assessment_validator", _av2_start, _av2_status, _av2_stop)
            except Exception:
                pass
    
            try:
                from core.workers.document_validator import (
                    start_worker as _dv_start, stop_worker as _dv_stop, get_status as _dv_status,
                )
                register_worker("document_validator", _dv_start, _dv_status, _dv_stop)
            except Exception:
                pass
    
            try:
                from core.workers.event_validator import (
                    start_worker as _ev_start, stop_worker as _ev_stop, get_status as _ev_status,
                )
                register_worker("event_validator", _ev_start, _ev_status, _ev_stop)
            except Exception:
                pass
    
            # Geocoder — runs Nominatim lookups for facility/address knowledge_nodes.
            # Was getting killed on every API restart; supervising it keeps the
            # coordinate substrate alive so the map loop keeps closing.
            try:
                from core.workers.geocoder import (
                    start_worker as _gc_start, stop_worker as _gc_stop, get_status as _gc_status,
                )
                register_worker("geocoder", _gc_start, _gc_status, _gc_stop)
                _gc_start("default")
            except Exception:
                pass
    
            # Phase 3 boundary workers (May 23 2026). Closing the three
            # boundary failures from the pipeline-restart diagnosis.
            try:
                from core.workers.investigation_sweep import (
                    start_worker as _is_start, stop_worker as _is_stop, get_status as _is_status,
                )
                register_worker("investigation_sweep", _is_start, _is_status, _is_stop)
            except Exception:
                pass
    
            try:
                from core.workers.mention_backfill import (
                    start_worker as _mb_start, stop_worker as _mb_stop, get_status as _mb_status,
                )
                register_worker("mention_backfill", _mb_start, _mb_status, _mb_stop)
            except Exception:
                pass
    
            try:
                from core.workers.entity_dedup_sweep import (
                    start_worker as _eds_start, stop_worker as _eds_stop, get_status as _eds_status,
                )
                register_worker("entity_dedup_sweep", _eds_start, _eds_status, _eds_stop)
            except Exception:
                pass
    
            # Signal clusterer — collapses duplicate news stories across sources
            # so the feed shows one cluster with N articles instead of N items.
            try:
                from core.workers.signal_clusterer import (
                    start_worker as _sc_start, stop_worker as _sc_stop, get_status as _sc_status,
                )
                register_worker("signal_clusterer", _sc_start, _sc_status, _sc_stop)
                _sc_start("default")
            except Exception:
                pass
    
            # Phase 2.5 OCR worker — fills raw_text for pages flagged
            # extraction_method='pending_ocr' by the PDF ingester. Tier 3,
            # auto-paused by the manager under pressure.
            try:
                from core.workers.ocr_worker import (
                    start_worker as _ocr_start, stop_worker as _ocr_stop, get_status as _ocr_status,
                )
                register_worker("ocr_worker", _ocr_start, _ocr_status, _ocr_stop)
            except Exception:
                pass
    
            # Register personal signal workers for supervision
            try:
                from core.workers.personal_location import (
                    start_worker as _pl_start, stop_worker as _pl_stop, get_status as _pl_status,
                )
                register_worker("personal_location", _pl_start, _pl_status, _pl_stop)
            except Exception:
                pass
    
            try:
                from core.workers.personal_contacts import (
                    start_worker as _pc_start, stop_worker as _pc_stop, get_status as _pc_status,
                )
                register_worker("personal_contacts", _pc_start, _pc_status, _pc_stop)
            except Exception:
                pass
    
            try:
                from core.workers.personal_comms import (
                    start_worker as _pm_start, stop_worker as _pm_stop, get_status as _pm_status,
                )
                register_worker("personal_comms", _pm_start, _pm_status, _pm_stop)
            except Exception:
                pass
    
            try:
                from core.workers.personal_calendar import (
                    start_worker as _pca_start, stop_worker as _pca_stop, get_status as _pca_status,
                )
                register_worker("personal_calendar", _pca_start, _pca_status, _pca_stop)
            except Exception:
                pass
    
            # Register the 6 news-pipeline daemons (library_manager,
            # discovery_continuous, collapse_worker, library_router,
            # signal_statement_extractor, predicate_extender) with the
            # supervisor so they get auto-restart on thread death + Ollama
            # health awareness. Without these registrations, a worker thread
            # crash silently kills the pipeline until the API container restarts.
            try:
                from core.workers.library_manager import (
                    start_worker as _lm_start, stop_worker as _lm_stop, get_status as _lm_status,
                )
                register_worker("library_manager", _lm_start, _lm_status, _lm_stop)
            except Exception:
                pass
            try:
                from core.workers.discovery_continuous import (
                    start_worker as _dc_start, stop_worker as _dc_stop, get_status as _dc_status,
                )
                register_worker("discovery_continuous", _dc_start, _dc_status, _dc_stop)
            except Exception:
                pass
            try:
                from core.workers.collapse_worker import (
                    start_worker as _cw_start, stop_worker as _cw_stop, get_status as _cw_status,
                )
                register_worker("collapse_worker", _cw_start, _cw_status, _cw_stop)
            except Exception:
                pass
            try:
                from core.workers.library_router import (
                    start_worker as _lr_start, stop_worker as _lr_stop, get_status as _lr_status,
                )
                register_worker("library_router", _lr_start, _lr_status, _lr_stop)
            except Exception:
                pass
            try:
                from core.workers.signal_statement_extractor import (
                    start_worker as _sse_start, stop_worker as _sse_stop, get_status as _sse_status,
                )
                register_worker("signal_statement_extractor", _sse_start, _sse_status, _sse_stop)
            except Exception:
                pass
            try:
                from core.workers.predicate_extender import (
                    start_worker as _pe_start, stop_worker as _pe_stop, get_status as _pe_status,
                )
                register_worker("predicate_extender", _pe_start, _pe_status, _pe_stop)
            except Exception:
                pass
    
            start_supervisor("default")
            trace_log.info(f"[{mode_label}] Worker supervisor started — auto-restart enabled (23 workers monitored).")
    
            # Pressure-aware scheduler: decides which workers are eligible to run
            # based on Ollama health and load. Sits above the supervisor — when
            # the manager disables a worker, the supervisor stops trying to keep
            # it alive until the manager re-enables it.
            try:
                from core.workers.manager import start_manager
                start_manager("default")
                trace_log.info(f"[{mode_label}] Worker manager started — pressure-aware scheduling enabled.")
            except Exception as e:
                trace_log.warning(f"[{mode_label}] Worker manager: {e}")
        except Exception as e:
            trace_log.warning(f"[{mode_label}] Worker supervisor: {e}")
    
        # ── Personal Signal Workers — process phone data into intelligence ──
        try:
            from core.workers.personal_supervisor import start_all_personal_workers
            start_all_personal_workers("default")
            trace_log.info(f"[{mode_label}] Personal signal workers started (location, contacts, comms, calendar).")
        except Exception as e:
            trace_log.warning(f"[{mode_label}] Personal workers: {e}")
    
        # Self-learning substrate — continuous node collapse + reconciliation.
        # Governor schedules registered workers by priority tier, pausing low-
        # priority work when foreground activity is detected via the trace
        # middleware's notice_foreground() pulse.
        try:
            from core.learning.governor import get_governor
            from core.learning.workers import (
                event_clusterer, merge_auto_resolver, media_backfill,
                entity_validator,
            )
            from core.research import governor as research_governor
            gov = get_governor()
            event_clusterer.register(gov)
            merge_auto_resolver.register(gov)
            media_backfill.register(gov)
            entity_validator.register(gov)
            research_governor.register(gov)
            gov.start()
            trace_log.info(f"[{mode_label}] Learning governor started with 5 worker(s).")
    
            # Resume any pending deep research from previous session
            try:
                from storage.postgres.db import get_connection
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT topic_id FROM watch_topics
                            WHERE deep_research_status = 'running'
                              AND tenant_id = 'default'
                        """)
                        resuming = [r[0] for r in cur.fetchall()]
                        if resuming:
                            trace_log.info(f"[{mode_label}] Resuming deep research for {len(resuming)} topic(s)")
            except Exception:
                pass  # Table may not exist on first run
    
        except Exception as e:
            trace_log.warning(f"[{mode_label}] Learning governor: {e}")

    trace_log.info(f"[{mode_label}] ──────────────────────────────────────────")
    trace_log.info(f"[{mode_label}] ENKI API ready (profile={PROFILE.value}).")
    trace_log.info(f"[{mode_label}] Swagger UI:  http://localhost:8000/docs")
    trace_log.info(f"[{mode_label}] Health:      http://localhost:8000/health")
    trace_log.info(f"[{mode_label}] ──────────────────────────────────────────")
    yield

    # Shutdown
    try:
        from core.reference.scheduler import stop_reference_scheduler
        stop_reference_scheduler()
    except Exception:
        pass
    try:
        close_pool()
        trace_log.info(f"[{mode_label}] Connection pool closed.")
    except Exception:
        pass

    try:
        from core.workers.manager import stop_manager
        stop_manager()
    except Exception:
        pass
    try:
        from core.workers.supervisor import stop_supervisor
        stop_supervisor()
    except Exception:
        pass
    try:
        from core.workers.runner import stop_workers
        stop_workers()
        from core.connectors.scheduler import stop_connector_scheduler
        stop_connector_scheduler()
        from core.connectors.watch_folder import stop_watch_folder
        stop_watch_folder()
        trace_log.info(f"[{mode_label}] Workers stopped.")
    except Exception:
        pass

    try:
        from core.learning.governor import get_governor
        get_governor().stop()
        trace_log.info(f"[{mode_label}] Learning governor stopped.")
    except Exception:
        pass

    if os.getenv("ENKI_HUB_URL"):
        try:
            from core.workers.hub_heartbeat import stop_worker as stop_hub_heartbeat
            stop_hub_heartbeat()
        except Exception:
            pass


# ─── App Factory ──────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="ENKI Command Center",
        version="0.3.0",
        lifespan=lifespan,
    )

    # ─── Request Tracing Middleware ───────────────────────────────────────
    @app.middleware("http")
    async def trace_requests(request: Request, call_next):
        start = time.perf_counter()
        method = request.method
        path = request.url.path
        tenant = request.headers.get("x-tenant-id", "-")
        identity = request.headers.get("x-identity-id", "-")

        is_health = path == "/health"

        if not is_health:
            trace_log.info(
                f"[TRACE] --> {method} {path}  "
                f"tenant={tenant}  identity={identity}"
            )
            # Tell the learning governor the user is active (debounced).
            _pulse_foreground_if_due()

        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000
            trace_log.error(
                f"[TRACE] <-- {method} {path}  500 UNHANDLED  "
                f"{elapsed:.0f}ms  tenant={tenant}"
            )
            trace_log.error(f"[TRACE] {traceback.format_exc()}")
            return JSONResponse(
                status_code=500,
                content={"error": "INTERNAL_ERROR", "detail": str(exc)},
            )

        elapsed = (time.perf_counter() - start) * 1000
        status = response.status_code

        if not is_health:
            if status < 300:
                level = "info"
                tag = "OK"
            elif status < 400:
                level = "info"
                tag = "REDIRECT"
            elif status < 500:
                level = "warning"
                tag = "CLIENT_ERR"
            else:
                level = "error"
                tag = "SERVER_ERR"

            msg = (
                f"[TRACE] <-- {method} {path}  {status} {tag}  "
                f"{elapsed:.0f}ms  tenant={tenant}"
            )
            getattr(trace_log, level)(msg)

        return response

    # ─── Validation Error Handler ────────────────────────────────────────
    # Converts missing auth headers (422) into ENKI 401 envelope
    @app.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        errors = exc.errors()
        missing_headers = set()
        for e in errors:
            loc = e.get("loc") or ()
            if (
                len(loc) >= 2
                and loc[0] == "header"
                and e.get("type") in ("missing", "value_error.missing")
            ):
                missing_headers.add(str(loc[1]).lower())

        required_auth_headers = {
            "x-device-id", "x-challenge-id", "x-tenant-id", "x-signature",
        }

        if missing_headers & required_auth_headers:
            return JSONResponse(
                status_code=401,
                content={
                    "error_code": "API.UNAUTHORIZED",
                    "detail": "Authentication required",
                    "meta": {
                        "missing_headers": sorted(list(missing_headers & required_auth_headers))
                    },
                },
            )

        return JSONResponse(status_code=422, content={"detail": errors})

    # ─── CORS ────────────────────────────────────────────────────────────
    origin = os.getenv("ENKI_ORIGIN", "*")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin] if origin != "*" else ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ─── Rate Limiting ───────────────────────────────────────────────────
    from services.api.src.middleware.rate_limit import RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware)

    # ─── Routers ─────────────────────────────────────────────────────────
    # Health (no auth)
    app.include_router(health_router)
    app.include_router(governance_router)
    app.include_router(validation_router)
    app.include_router(p2p_router)
    app.include_router(public_library_router)
    app.include_router(contribute_router)
    app.include_router(public_explore_router)
    app.include_router(library_notes_router)
    app.include_router(news_heatmap_router)
    app.include_router(gdelt_router)
    app.include_router(watch_dashboard_router)
    # Cards API — feeds the card kit v2 UI (libraries, assessments,
    # events, references, neighbors). See cards_api.py + cards2/.
    app.include_router(cards_api_router)
    app.include_router(library_dossier_router)
    app.include_router(catalog_router)
    # Chat ask — universal entry-point that routes questions to tools
    # which execute against the cyclical model. See chat_ask.py.
    app.include_router(chat_ask_router)

    # Geo routes BEFORE signals (path ordering)
    app.include_router(geo_router)

    # Core data routes
    app.include_router(signals_router)
    app.include_router(cases_router)
    app.include_router(detection_router)
    app.include_router(connectors_router)
    app.include_router(ai_router)
    app.include_router(gallery_router)
    app.include_router(knowledge_router)
    app.include_router(profiles_router)
    app.include_router(devices_router)

    # Feature routes
    app.include_router(invite_router)
    app.include_router(card_templates_router)
    app.include_router(processing_router)
    app.include_router(workers_router)
    app.include_router(tracks_router)
    app.include_router(geofences_router)
    app.include_router(webhooks_router)
    app.include_router(knowledge_geo_router)
    app.include_router(annotations_router)
    app.include_router(recipes_router)
    app.include_router(downloads_router)
    app.include_router(exports_router)
    from routes.imports import router as imports_router
    app.include_router(imports_router)
    from routes.reports import router as reports_router
    app.include_router(reports_router)
    from routes.timeline import router as timeline_router
    app.include_router(timeline_router)
    from routes.contacts import router as contacts_router
    app.include_router(contacts_router)
    from routes.assets import router as assets_router
    app.include_router(assets_router)
    from routes.watch_topics import router as watch_topics_router
    app.include_router(watch_topics_router)
    from routes.graph import router as graph_router
    app.include_router(graph_router)
    app.include_router(live_router)
    app.include_router(cameras_router)
    app.include_router(reference_router)
    app.include_router(space_router)
    from routes.envelope_cards import router as envelope_cards_router
    app.include_router(envelope_cards_router)
    from routes.bookmarks import router as bookmarks_router
    app.include_router(bookmarks_router)
    from routes.story_research import router as story_research_router
    app.include_router(story_research_router)
    from routes.selection_research import router as selection_research_router
    app.include_router(selection_research_router)
    from routes.graph_reasoning import router as graph_reasoning_router
    app.include_router(graph_reasoning_router)
    from routes.node_recall import router as node_recall_router
    app.include_router(node_recall_router)
    from routes.chronicle import router as chronicle_router
    app.include_router(chronicle_router)
    from routes.unknown_types import router as unknown_types_router
    app.include_router(unknown_types_router)
    from routes.nodes import router as nodes_router
    app.include_router(nodes_router)
    app.include_router(reference_layers_router)
    app.include_router(setup_router)
    app.include_router(intel_setup_router)
    app.include_router(wizard_router)
    app.include_router(fleet_router)
    app.include_router(auth_local_router)
    app.include_router(threads_router)
    app.include_router(providers_router)
    app.include_router(entity_profile_router)
    app.include_router(media_router)
    app.include_router(overrides_router)
    app.include_router(secrets_router)
    app.include_router(satellite_router)
    app.include_router(refdata_router)
    app.include_router(documents_router)
    app.include_router(onboarding_router)
    app.include_router(feed_facets_router)
    app.include_router(sources_router)
    app.include_router(hub_layout_router)
    app.include_router(tenant_settings_router)
    app.include_router(tts_router)
    app.include_router(feed_graph_router)
    app.include_router(feed_map_router)
    app.include_router(feed_router)
    app.include_router(thumbnails_router)

    # Investigation routes (entity deep-dive: SEC, FEC, courts, etc.)
    from routes.investigation import router as investigation_router
    app.include_router(investigation_router)

    # Deep-research routes (LLM-directed web-search → grab → collapse → report)
    from routes.research import router as research_router
    app.include_router(research_router)

    # Corporate research routes (entity → companies + addresses + officers + filings)
    from routes.corporate import router as corporate_router
    app.include_router(corporate_router)

    # Device auth routes (challenge issuance works in any mode;
    # enrollment/ingest/query require grant_store on app.state)
    app.include_router(admin_router)
    app.include_router(ingest_router)
    app.include_router(query_router)
    app.include_router(receipts_router)
    app.include_router(audit_router)
    app.include_router(chain_router)
    app.include_router(artifacts_router)
    app.include_router(access_log_router)
    app.include_router(worker_activity_router)
    app.include_router(chain_verify_router)
    app.include_router(correlate_router)
    app.include_router(node_refresh_router)
    app.include_router(permissions_router)

    return app


app = create_app()
