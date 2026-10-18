<script lang="ts">
	import { onDestroy } from 'svelte';
	import { page } from '$app/stores';
	import ChronoRow from '$lib/components/cards2/ChronoRow.svelte';
	import ConnectionWeb from '$lib/components/ConnectionWeb.svelte';
	import Disclosure from '$lib/components/Disclosure.svelte';
	import Search from 'lucide-svelte/icons/search';
	import Loader2 from 'lucide-svelte/icons/loader-2';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Satellite from 'lucide-svelte/icons/satellite';
	import User from 'lucide-svelte/icons/user';
	import Building2 from 'lucide-svelte/icons/building-2';
	import Globe from 'lucide-svelte/icons/globe';
	import FileText from 'lucide-svelte/icons/file-text';

	const entity_id = $derived(parseInt($page.params.id, 10));
	const lens = $derived($page.url.searchParams.get('lens'));
	const lensQuery = $derived(lens ? `&lens=${encodeURIComponent(lens)}` : '');
	const lensQueryFirst = $derived(lens ? `?lens=${encodeURIComponent(lens)}` : '');
	const lensLabel = $derived.by(() => {
		if (!lens) return null;
		const [scope, value] = lens.split('/');
		if (scope === 'library') return `Library #${value}`;
		if (scope === 'topic') return `Topic #${value}`;
		return lens;
	});

	let entity = $state<any>(null);
	let story = $state<any>({ groups: [], total: 0 });
	let neighbors = $state<any[]>([]);
	let companies = $state<any[]>([]);
	let researchingCompanies = $state(false);
	let corroboration = $state<any>(null);
	let facts = $state<any[]>([]);
	let propertyGroups = $state<any[]>([]);
	let assetGroups = $state<any[]>([]);
	let assetCount = $state<number>(0);
	let topicMemberships = $state<any[]>([]);
	let documentAppearances = $state<any[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let researchBusy = $state(false);
	let researchPollTimer: ReturnType<typeof setInterval> | null = null;
	let investigations = $state<any[]>([]);
	let geo = $state<{ lat: number; lon: number } | null>(null);
	// ─── Satellite (ESA/Copernicus) overhead capture ─────────────────
	let satelliteConfigured = $state(false);
	let satCaptures = $state<any[]>([]);
	let satCapturing = $state(false);
	let satError = $state<string | null>(null);

	async function loadSatellite() {
		try {
			const st = await fetch('/api/satellite/status').then(r => r.ok ? r.json() : null);
			satelliteConfigured = !!st?.configured;
			if (satelliteConfigured) {
				const d = await fetch(`/api/entities/${entity_id}/documents?entity_type=node&doc_category=imagery`)
					.then(r => r.ok ? r.json() : { documents: [] });
				satCaptures = d.documents || [];
			}
		} catch {}
	}

	async function captureOverhead(layer = 'TRUE_COLOR') {
		if (satCapturing) return;
		satCapturing = true;
		satError = null;
		try {
			const res = await fetch('/api/satellite/capture', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ entity_id: String(entity_id), entity_type: 'node', layer }),
			});
			const data = await res.json();
			if (!res.ok || data.error) {
				satError = data.detail || data.error || 'Capture failed';
			} else {
				await loadSatellite();
			}
		} catch (e: any) {
			satError = e?.message || 'Capture failed';
		}
		satCapturing = false;
	}

	let liveResearch = $state(false);          // background research in flight from auto-trigger
	let lastFindingsCount = $state(0);          // for change-detection during poll

	// Per-type enrichment (operator-triggered). Replaces the old auto-queue
	// model: instead of every entity getting every enrichment in the background,
	// the operator picks the specific lookup that matters for THIS dossier.
	type EnrichmentItem = {
		type: string;
		description: string;
		priority: number;
		sources: string[];
		last_run: {
			status: string;
			created_at: string | null;
			completed_at: string | null;
			error: string | null;
		} | null;
	};
	let enrichmentItems = $state<EnrichmentItem[]>([]);
	let enrichmentBusy = $state<Record<string, boolean>>({});
	let storyRefreshTimer: ReturnType<typeof setInterval> | null = null;
	let expandedPredicate = $state<string | null>(null);
	let showAllRelations = $state(false);

	// Persona clusters (Phase 1 of persona-clustering work). Each cluster
	// is a candidate real-world referent under this entity_id; the anchor
	// keeps the parent entity, the rest can be split off into siblings.
	// See project_persona_clustering memory.
	type PersonaCluster = {
		cluster_id: string;
		label: string;
		anchor: boolean;
		confidence: number;
		canonical_subtype: string;
		subtype_path: string;
		identifiers: Record<string, string>;
		dominant_attrs: Record<string, any>;
		evidence_tier: string;
		finding_ids: number[];
		findings: Array<{
			finding_id: number | null;
			source: string; source_label: string; finding_type: string;
			title: string; claim: string | null; source_url: string | null;
			confidence: number | null;
		}>;
		findings_by_source: Array<{
			source: string;
			source_label: string;
			count: number;          // total findings from this source (with dupes)
			unique_count: number;   // distinct claims
			unique_claims: Array<{
				title: string;
				claim: string | null;
				source_url: string | null;
				confidence: number | null;
				finding_id: number | null;
				finding_type: string | null;
				count: number;       // how many ingest passes surfaced this claim
			}>;
		}>;
		properties: Array<{
			predicate: string;            // 'occupation' / 'date of birth' / ...
			value: string;
			value_url: string | null;     // where the VALUE navigates (website / Twitter / ...)
			source: string;
			source_label: string;
			source_url: string | null;    // provenance — where the fact was asserted
		}>;
	};
	let personaClusters = $state<PersonaCluster[]>([]);
	// Org clusters that already resolve to their own identifier-keyed entity —
	// shown as linked organizations, not namesake candidates.
	let resolvedOrgs = $state<any[]>([]);
	let foldedSameName = $state<number>(0);
	// Substrate-level view of the entity's surface form binding distribution.
	type Substrate = {
		entity_id: number;
		canonical_name: string;
		form: { form_id: number; form_address: string; surface_form: string; signal_count: number } | null;
		bindings: Array<{
			concept_id: number;
			binding_source: string;
			binding_confidence: number | null;
			concept_address: string | null;
			canonical_name: string | null;
			canonical_subtype: string | null;
			promoted_to_entity_id: number | null;
			is_self: boolean;
		}>;
	};
	let substrate = $state<Substrate | null>(null);
	let substrateOpen = $state(false);
	let personaEditNames = $state<Record<string, string>>({});  // cluster_id → operator-edited label
	let personaSplitting = $state<string | null>(null);          // cluster_id currently being split
	let personaSplitError = $state<string | null>(null);

	// Federation provenance for this entity — cross-node trust story
	let federation = $state<{
		canonical_entity_key: string | null;
		base60_address: string | null;
		pulled_from_node_id: string | null;
		trust_story: string;
		mention_total: number;
		local_mention_count: number;
		pulled_mention_count: number;
		mention_sources: Array<{ source: string; count: number }>;
		envelopes: Array<{
			envelope_sha: string;
			producer_node_id: string;
			mentions_from_envelope: number;
			first_seen: string | null;
		}>;
		envelope_count: number;
		notes: Array<any>;
		note_count: number;
	} | null>(null);

	async function loadFederation() {
		try {
			const r = await fetch(`/api/entities/${entity_id}/federation`);
			if (r.ok) federation = await r.json();
		} catch {}
	}

	/** Split a persona cluster off into its own entity. Carries the
	 * cluster's binding evidence so the split endpoint also writes the
	 * base-60 substrate (form/concept/binding per migration 093). */
	// The backend now labels each cluster with its own registered name (FEC
	// committee / 990 nonprofit / corp), so trust it; only fall back for bare
	// person-namesakes with no distinguishing name of their own.
	function betterClusterName(c: any): string {
		return (c.label || 'this name').trim();
	}

	let personaSplitNewId = $state<number | null>(null);
	let personaSplitNewName = $state<string>('');

	async function submitClusterSplit(c: PersonaCluster) {
		if (c.anchor) return;
		const newName = (personaEditNames[c.cluster_id] || c.label).trim();
		if (!newName || c.finding_ids.length === 0) return;
		personaSplitError = null;
		personaSplitting = c.cluster_id;
		try {
			const r = await fetch(`/api/nodes/entity:${entity_id}/split`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					new_entity_name: newName,
					new_entity_type: c.canonical_subtype,
					finding_ids: c.finding_ids,
					reason: `persona split: cluster ${c.cluster_id} (${c.evidence_tier}) — ${c.finding_ids.length} findings`,
					cluster: {
						cluster_id: c.cluster_id,
						confidence: c.confidence,
						canonical_subtype: c.canonical_subtype,
						subtype_path: c.subtype_path,
						identifiers: c.identifiers,
						dominant_attrs: c.dominant_attrs,
						evidence_tier: c.evidence_tier,
					},
				}),
			});
			if (!r.ok) {
				personaSplitError = `split failed (${r.status})`;
				return;
			}
			const result = await r.json();
			// Refresh personas — the split removes findings from this
			// entity so the cluster disappears.
			try {
				const refresh = await fetch(`/api/entities/${entity_id}/personas`);
				if (refresh.ok) {
					const data = await refresh.json();
					personaClusters = data?.clusters || [];
				}
			} catch {}
			// Surface a clickable link to the new entity's profile.
			personaSplitNewId = result.new_entity_id;
			personaSplitNewName = newName;
		} catch (e: any) {
			personaSplitError = `error: ${e?.message || e}`;
		} finally {
			personaSplitting = null;
		}
	}

	// Pull the most useful quote/excerpt out of a finding's data jsonb.
	// Findings come from many heterogeneous sources (Wikipedia, SEC, FEC,
	// CourtListener, FDA, ProPublica, etc.) and each one has its own
	// "here's the actual prose" field. We try them in priority order and
	// return the first non-empty hit, trimmed to a reasonable length.
	const QUOTE_FIELDS = [
		'summary', 'description', 'abstract',
		'remarks', 'selftext_snippet',
		'product_description', 'sic_description',
		'opinion', 'holding', 'text',
	];
	function extractQuote(data: any): string | null {
		if (!data || typeof data !== 'object') return null;
		for (const k of QUOTE_FIELDS) {
			const v = data[k];
			if (typeof v === 'string' && v.trim().length > 0) {
				const trimmed = v.trim().replace(/\s+/g, ' ');
				return trimmed.length > 280 ? trimmed.slice(0, 277) + '…' : trimmed;
			}
		}
		return null;
	}

	// Filter for the Story stream
	type FilterKind = 'all' | 'fact' | 'event' | 'finding' | 'mention';
	let filter = $state<FilterKind>('all');

	async function loadAll() {
		loading = true;
		try {
			// Probe the main entity endpoint first so a 410 (cross-category
			// reclassification: this entity_id has been migrated to a place /
			// topic / event / chronicle / document substrate) short-circuits
			// before the 9 other fetches waste a round-trip each.
			const probe = await fetch(`/api/entities/${entity_id}${lensQueryFirst}`);
			if (probe.status === 410) {
				const detail = await probe.json().catch(() => ({}));
				const d = detail?.detail || detail || {};
				const target = d?.reclassified_to as string | undefined;   // e.g. "place:3787"
				const cat    = d?.target_category as string | undefined;
				const sub    = d?.target_subtype as string | undefined;
				const pending = d?.pending === true;
				const sweep_action = d?.sweep_action as string | undefined; // unmapped_residual | dropped_garbage
				// Four shapes:
				//   1. Tier-2 migration: target + cat with a route slug → navigate
				//   2. Tier-2 migration, no route slug yet (place, chronicle) → placeholder
				//   3. Tier-1 pending: cat set, no target yet → "queued for migration"
				//   4. Decontamination tombstone: sweep_action set, no cat → "tombstoned" placeholder
				if (target && cat) {
					const id = target.split(':', 2)[1];
					const slugByCat: Record<string, string> = {
						topic:    'topic',
						event:    'event',
						document: 'document',
					};
					const slug = slugByCat[cat];
					if (slug && id) {
						window.location.replace(`/${slug}/${id}`);
						return;
					}
					error = `This entity is now a ${cat}.${sub || '?'} (${target}). `
					      + `The ${cat} dossier page isn't built yet.`;
					return;
				}
				if (pending && cat) {
					error = `This entity is queued for migration to ${cat}.${sub || '?'}. `
					      + `Once the ${cat} migrator runs, the new dossier will be linked here.`;
					return;
				}
				if (sweep_action === 'dropped_garbage') {
					error = `This row was flagged as noise during decontamination `
					      + `(${d?.sweep_reason || 'no reason'}) and has no dossier.`;
					return;
				}
				if (sweep_action === 'unmapped_residual') {
					error = `Decontamination couldn't classify this entity `
					      + `(${d?.sweep_reason || 'no mapping'}). It's tombstoned `
					      + `but not yet routed to a substrate.`;
					return;
				}
				error = `Entity ${entity_id} has been reclassified but the redirect target is missing.`;
				return;
			}
			if (!probe.ok && probe.status !== 404) {
				error = `entity load failed: ${probe.status}`;
				return;
			}
			const ent = probe.ok ? await probe.json() : null;
			const [st, nb, inv, geoData, fc, props, ast, tops, docs, personas, sub, corr, comp] = await Promise.all([
				fetch(`/api/entities/${entity_id}/story?limit=300${lensQuery}`).then(r => r.ok ? r.json() : { groups: [], total: 0 }),
				fetch(`/api/entities/${entity_id}/neighbors?limit=40${lensQuery}`).then(r => r.ok ? r.json() : { neighbors: [] }),
				fetch(`/api/entities/${entity_id}/investigations?limit=10`).then(r => r.ok ? r.json() : { investigations: [] }),
				fetch(`/api/entities/${entity_id}/geo`).then(r => r.ok ? r.json() : null).catch(() => null),
				fetch(`/api/entities/${entity_id}/facts?limit=300${lensQuery}`).then(r => r.ok ? r.json() : { facts: [] }),
				fetch(`/api/entities/${entity_id}/properties`).then(r => r.ok ? r.json() : { groups: [] }),
				fetch(`/api/entities/${entity_id}/assets`).then(r => r.ok ? r.json() : { groups: [], total_assets: 0 }),
				fetch(`/api/entities/${entity_id}/topic_memberships`).then(r => r.ok ? r.json() : { topics: [] }),
				fetch(`/api/entities/${entity_id}/document_appearances?limit=20`).then(r => r.ok ? r.json() : { documents: [] }),
				fetch(`/api/entities/${entity_id}/personas`).then(r => r.ok ? r.json() : { clusters: [] }),
				fetch(`/api/entities/${entity_id}/substrate`).then(r => r.ok ? r.json() : null),
				fetch(`/api/entities/${entity_id}/corroboration`).then(r => r.ok ? r.json() : null),
				fetch(`/api/entities/${entity_id}/companies`).then(r => r.ok ? r.json() : { companies: [] }),
			]);
			companies = comp?.companies || [];
			corroboration = corr;
			personaClusters = personas?.clusters || [];
			resolvedOrgs = personas?.resolved_orgs || [];
			foldedSameName = personas?.folded_same_name_count || 0;
			substrate = sub;
			personaEditNames = {};
			for (const c of personaClusters) personaEditNames[c.cluster_id] = betterClusterName(c);
			entity = ent;
			story = st || { groups: [], total: 0 };
			neighbors = nb?.neighbors || [];
			investigations = inv?.investigations || [];
			lastFindingsCount = investigations[0]?.findings_count || 0;
			geo = geoData?.latitude != null
				? { lat: geoData.latitude, lon: geoData.longitude }
				: null;
			facts = fc?.facts || [];
			propertyGroups = props?.groups || [];
			assetGroups = ast?.groups || [];
			assetCount = ast?.total_assets || 0;
			topicMemberships = tops?.topics || [];
			documentAppearances = docs?.documents || [];
			if (!entity) error = `entity ${entity_id} not found`;
			loadFederation();
			loadEnrichment();
			loadSatellite();
			// After initial load, decide whether to auto-fire live research
			if (entity && !error) maybeAutoResearch();
		} catch (e: any) {
			error = e.message;
		} finally {
			loading = false;
		}
	}

	/** Fire a high-priority background research run if this entity is sparse
	 * and hasn't been researched recently. Idempotent: backend cools down
	 * for 24 hours on auto-triggered runs. */
	async function maybeAutoResearch() {
		if (!entity) return;
		const fact_count = entity.fact_count ?? 0;
		const event_count = entity.event_count ?? 0;
		const finding_count = lastFindingsCount;
		// Skip if entity already has rich data
		if (fact_count + event_count + finding_count >= 10) return;
		// Skip noisy/short canonical names
		const name = entity.canonical_name || '';
		if (name.length < 4 || /[!?]$/.test(name) || /[-_:;]$/.test(name)) return;
		try {
			// sync=true: run the investigation inline so the user sees results
			// in 5-10s instead of waiting behind the 90K+ pending-item queue.
			// Server returns the complete result (findings included) when it
			// finishes. We still call startLivePolling for the live spinner
			// while the request is in flight.
			liveResearch = true;
			const r = await fetch(`/api/entities/${entity_id}/research`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ auto: true, priority: 10, sync: true }),
			});
			if (!r.ok) {
				liveResearch = false;
				return;
			}
			const j = await r.json();
			if (j.sync) {
				// Synchronous run finished — refetch the data immediately so
				// Story / Investigations / facts pick up the new findings.
				try {
					const [stRes, invRes, entRes] = await Promise.all([
						fetch(`/api/entities/${entity_id}/story?limit=300${lensQuery}`).then((r) => r.ok ? r.json() : null),
						fetch(`/api/entities/${entity_id}/investigations?limit=10`).then((r) => r.ok ? r.json() : null),
						fetch(`/api/entities/${entity_id}${lensQueryFirst}`).then((r) => r.ok ? r.json() : null),
					]);
					if (stRes) story = stRes;
					if (invRes) {
						investigations = invRes.investigations || [];
						lastFindingsCount = investigations[0]?.findings_count || 0;
					}
					if (entRes) entity = entRes;
				} catch {}
				liveResearch = false;
			} else if (j.queued || j.status === 'pending' || j.status === 'running') {
				// Sync wasn't honoured (older API) — fall back to polling.
				startLivePolling();
			} else {
				liveResearch = false;
			}
		} catch {
			liveResearch = false;
		}
	}

	/** Poll /investigations every 6s; when findings_count increments,
	 * refetch /story so the new rows slot in. Stops when the
	 * investigation reaches a terminal state. */
	function startLivePolling() {
		if (researchPollTimer) clearInterval(researchPollTimer);
		researchPollTimer = setInterval(async () => {
			try {
				const r = await fetch(`/api/entities/${entity_id}/investigations?limit=5`);
				if (!r.ok) return;
				const j = await r.json();
				investigations = j.investigations || [];
				const cur = investigations[0]?.findings_count || 0;
				if (cur > lastFindingsCount) {
					lastFindingsCount = cur;
					// Refresh Story to show new findings
					const st = await fetch(`/api/entities/${entity_id}/story?limit=300${lensQuery}`);
					if (st.ok) story = await st.json();
					// Also refresh entity (description may now exist)
					const ent = await fetch(`/api/entities/${entity_id}${lensQueryFirst}`);
					if (ent.ok) entity = await ent.json();
				}
				const top = investigations[0];
				if (!top || !['pending', 'running'].includes(top.status)) {
					liveResearch = false;
					if (researchPollTimer) {
						clearInterval(researchPollTimer);
						researchPollTimer = null;
					}
				}
			} catch {}
		}, 6000);
	}

	async function loadEnrichment() {
		try {
			const r = await fetch(`/api/entities/${entity_id}/enrichment`);
			if (!r.ok) return;
			const j = await r.json();
			enrichmentItems = j.available || [];
		} catch {}
	}

	async function triggerEnrichment(t: string) {
		if (enrichmentBusy[t]) return;
		enrichmentBusy = { ...enrichmentBusy, [t]: true };
		try {
			await fetch(`/api/entities/${entity_id}/enrich/${encodeURIComponent(t)}`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: '{}',
			});
			await loadEnrichment();
		} finally {
			enrichmentBusy = { ...enrichmentBusy, [t]: false };
		}
	}

	async function triggerResearch(force = false) {
		researchBusy = true;
		try {
			const res = await fetch(`/api/entities/${entity_id}/research`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ force, priority: 5 }),  // user-initiated → highest priority
			});
			if (res.ok) {
				const j = await res.json();
				investigations = await fetch(`/api/entities/${entity_id}/investigations?limit=5`)
					.then(r => r.ok ? r.json() : { investigations: [] })
					.then(d => d.investigations || []);
				if (j.queued || j.status === 'pending' || j.status === 'running') {
					liveResearch = true;
					startLivePolling();
				}
			}
		} finally {
			researchBusy = false;
		}
	}

	async function researchCompanies() {
		researchingCompanies = true;
		try {
			await fetch(`/api/entities/${entity_id}/research-companies`, { method: 'POST' });
			investigations = await fetch(`/api/entities/${entity_id}/investigations?limit=5`)
				.then(r => r.ok ? r.json() : { investigations: [] }).then(d => d.investigations || []);
			liveResearch = true; startLivePolling();
			// refresh the companies list shortly after (findings stream in)
			setTimeout(async () => {
				companies = await fetch(`/api/entities/${entity_id}/companies`)
					.then(r => r.ok ? r.json() : { companies: [] }).then(d => d.companies || []);
			}, 4000);
		} finally {
			researchingCompanies = false;
		}
	}

	const activeInvestigation = $derived(investigations.find(i => ['pending', 'running'].includes(i.status)));
	const lastComplete = $derived(investigations.find(i => i.status === 'complete'));

	const TypeIcon = $derived.by(() => {
		const t = (entity?.canonical_type || '').toLowerCase();
		if (t === 'person') return User;
		if (['country', 'city', 'region'].includes(t)) return Globe;
		if (['facility', 'address', 'building', 'compound', 'residence'].includes(t)) return MapPin;
		if (['organization', 'company', 'government_agency'].includes(t)) return Building2;
		return FileText;
	});

	const hasIdentifiers = $derived(entity?.identifiers && Object.keys(entity.identifiers).length > 0);
	const hasAliases = $derived((entity?.aliases || []).length > 0);
	const hasConcept = $derived(!!entity?.concept_parent);

	// ─── Predicate-grouped relations (verb graph) ───────────────────────────
	// /facts returns one row per (subj, predicate, obj) assessment. Group by
	// (direction, predicate) so the panel reads like a fact sheet:
	//   "Kennedy → traveled_to → Berlin, Italy, West Germany, Ireland, UK"
	//   "Kennedy → met_with → Khrushchev"
	// Outgoing = this entity is subject. Incoming = this entity is object.
	type RelTarget = {
		entity_id: number | null;
		name: string;
		obj_value: string | null;
		corroboration: number;
		assessment_id: number;
		earliest_event_time: string | null;
		sources: any[];
	};
	type RelGroup = {
		key: string;
		direction: 'out' | 'in';
		predicate: string;
		targets: RelTarget[];
		total_corroboration: number;
	};

	function _prettyPredicate(p: string | null): string {
		if (!p) return '(related to)';
		return p.replace(/_/g, ' ');
	}

	/** Build a short provenance chip for a Relations target. Prefers the
	 * signal source outlet (Fox News, NYT) for news-derived facts; falls
	 * back to the document title for archive-derived facts. */
	function _provenanceFor(t: RelTarget): { label: string; href: string | null } {
		const s = t.sources?.[0];
		if (!s) return { label: '', href: null };
		// Signal-source for news (e.g. "Fox News — Latest" -> "Fox News")
		if (s.signal_source) {
			const cleanSrc = (s.signal_source as string).split('—')[0].trim();
			return {
				label: cleanSrc,
				href: s.signal_rid ? `/signals/${s.signal_rid}` : null,
			};
		}
		// Document title for archive facts (CIA FOIA, etc.)
		if (s.document_title) {
			const title = s.document_title as string;
			return {
				label: title.length > 32 ? title.slice(0, 30) + '…' : title,
				href: s.document_id ? `/document/${s.document_id}` : null,
			};
		}
		return { label: '', href: null };
	}

	const relationGroups = $derived.by<RelGroup[]>(() => {
		const map = new Map<string, RelGroup>();
		for (const f of facts) {
			const isOut = f.subj_entity_id === entity_id;
			const direction: 'out' | 'in' = isOut ? 'out' : 'in';
			const predicate = f.predicate_edge_type || 'related_to';
			const key = `${direction}::${predicate}`;
			let group = map.get(key);
			if (!group) {
				group = {
					key,
					direction,
					predicate,
					targets: [],
					total_corroboration: 0,
				};
				map.set(key, group);
			}
			const other = isOut
				? { id: f.obj_entity_id, name: f.obj_name }
				: { id: f.subj_entity_id, name: f.subj_name };
			group.targets.push({
				entity_id: other.id ?? null,
				name: other.name || f.obj_value || '(unnamed)',
				obj_value: f.obj_value,
				corroboration: f.corroborated_by_count || 1,
				assessment_id: f.assessment_id,
				earliest_event_time: f.earliest_event_time || null,
				sources: f.sources || [],
			});
			group.total_corroboration += f.corroborated_by_count || 1;
		}
		// Sort targets within each group by corroboration desc
		for (const g of map.values()) {
			g.targets.sort((a, b) => b.corroboration - a.corroboration);
		}
		// Sort groups: outgoing first, then by total corroboration desc, then by target count
		return [...map.values()].sort((a, b) => {
			if (a.direction !== b.direction) return a.direction === 'out' ? -1 : 1;
			if (b.total_corroboration !== a.total_corroboration)
				return b.total_corroboration - a.total_corroboration;
			return b.targets.length - a.targets.length;
		});
	});

	const visibleRelations = $derived(
		showAllRelations ? relationGroups : relationGroups.slice(0, 8)
	);

	// ─── Timeline strip ────────────────────────────────────────────────────
	// Flat list of every dated row across the Story stream, plotted on a
	// horizontal band that runs from minYear → maxYear. A glance shows the
	// entity's recorded lifespan; click a dot to scroll to the matching row.
	type TimelinePoint = {
		key: string;         // DOM id of the target row
		kind: 'fact' | 'event' | 'finding' | 'mention';
		when: string;
		year: number;
		title: string;
	};

	const timelinePoints = $derived.by<TimelinePoint[]>(() => {
		const pts: TimelinePoint[] = [];
		for (const g of story.groups || []) {
			for (const r of g.rows || []) {
				if (!r.when) continue;
				const y = Number((r.when as string).slice(0, 4));
				if (!Number.isFinite(y) || y < 1800 || y > 2100) continue;
				let id: string | undefined;
				if (r.kind === 'fact'    && r.assessment_id) id = `story-fact-${r.assessment_id}`;
				if (r.kind === 'event'   && r.event_id)      id = `story-event-${r.event_id}`;
				if (r.kind === 'finding' && r.finding_id)    id = `story-finding-${r.finding_id}`;
				if (r.kind === 'mention' && r.paragraph_id)  id = `story-mention-${r.paragraph_id}`;
				if (!id) continue;
				pts.push({
					key: id,
					kind: r.kind,
					when: r.when,
					year: y,
					title: r.title || r.kind,
				});
			}
		}
		pts.sort((a, b) => a.when.localeCompare(b.when));
		return pts;
	});

	const timelineBounds = $derived.by(() => {
		if (timelinePoints.length === 0) return null;
		const minY = timelinePoints[0].year;
		const maxY = timelinePoints[timelinePoints.length - 1].year;
		// Pad to a 5-year minimum span so single-year entities don't render
		// as one dot at 50%.
		const span = Math.max(maxY - minY, 5);
		const adjMin = minY;
		const adjMax = minY + span;
		// Year ticks: every 10 if span > 20, every 5 if span > 8, else every 1
		const tickStep = span > 30 ? 10 : span > 12 ? 5 : 1;
		const ticks: number[] = [];
		// Round the first tick up to the next multiple of step
		const firstTick = Math.ceil(adjMin / tickStep) * tickStep;
		for (let y = firstTick; y <= adjMax; y += tickStep) ticks.push(y);
		return { min: adjMin, max: adjMax, span, ticks, tickStep };
	});

	function pointLeftPct(p: TimelinePoint): string {
		if (!timelineBounds) return '0%';
		const t = new Date(p.when).getTime();
		const minT = new Date(`${timelineBounds.min}-01-01`).getTime();
		const maxT = new Date(`${timelineBounds.max}-12-31`).getTime();
		if (!Number.isFinite(t) || maxT <= minT) return '0%';
		const pct = ((t - minT) / (maxT - minT)) * 100;
		return `${Math.max(0, Math.min(100, pct))}%`;
	}

	let hoveredPoint = $state<TimelinePoint | null>(null);

	function scrollToPoint(p: TimelinePoint) {
		if (typeof document === 'undefined') return;
		const el = document.getElementById(p.key);
		if (el) {
			el.scrollIntoView({ behavior: 'smooth', block: 'center' });
			// Briefly flash via :target
			window.history.replaceState(null, '', `#${p.key}`);
		}
	}

	// Filtered groups for the Story stream
	const filteredGroups = $derived.by(() => {
		if (filter === 'all') return story.groups;
		return story.groups
			.map((g: any) => ({
				...g,
				rows: g.rows.filter((r: any) => r.kind === filter),
				count: g.rows.filter((r: any) => r.kind === filter).length,
			}))
			.filter((g: any) => g.rows.length > 0);
	});

	// Counts per kind for the filter chips
	const kindCounts = $derived.by(() => {
		const counts = { all: 0, fact: 0, event: 0, finding: 0, mention: 0 } as Record<FilterKind, number>;
		for (const g of story.groups) {
			for (const r of g.rows) {
				counts.all++;
				if (r.kind in counts) counts[r.kind as FilterKind]++;
			}
		}
		return counts;
	});

	$effect(() => {
		void lens;
		if (!Number.isFinite(entity_id)) return;
		loadAll();
	});

	onDestroy(() => { if (researchPollTimer) clearInterval(researchPollTimer); });
</script>

<div class="max-w-7xl mx-auto space-y-4">
	<a href="/library" class="text-xs text-muted-foreground hover:text-foreground inline-block">◀ Libraries</a>

	{#if lens}
		<div class="flex items-baseline justify-between gap-3 px-3 py-2 rounded-md border border-amber-500/30 bg-amber-500/5 text-xs">
			<div class="text-muted-foreground">
				Filtered to:
				<span class="text-foreground ml-1">{lensLabel}</span>
				<span class="ml-2">— only facts from this corpus shown</span>
			</div>
			<a href={`/entity/${entity_id}`} class="text-amber-300 hover:text-amber-200 underline-offset-2 hover:underline">
				View {entity?.canonical_name || 'entity'} everywhere →
			</a>
		</div>
	{/if}

	{#if entity?.redirected_from}
		<div class="px-3 py-2 rounded-md border border-blue-500/30 bg-blue-500/5 text-xs text-muted-foreground">
			<span class="text-blue-300">↪</span> Redirected from
			<span class="font-mono">#{entity.redirected_from}</span>
			— that entity was merged into <span class="text-foreground">{entity.canonical_name}</span>
		</div>
	{/if}

	{#if loading}
		<p class="text-sm text-muted-foreground">Loading…</p>
	{:else if error}
		<p class="text-sm text-destructive">⚠ {error}</p>
	{:else if entity}
		<!-- ─── Compact entity header ─── -->
		<header class="rounded-xl border border-border bg-card px-5 py-4">
			<div class="flex items-start justify-between gap-4">
				{#if entity.avatar_url}
					<a href={entity.avatar_url} target="_blank" rel="noopener noreferrer"
						class="shrink-0 group"
						title={entity.avatar_caption || `${entity.avatar_source ?? ''} thumbnail`}>
						<img src={entity.avatar_url}
							alt={`${entity.canonical_name} thumbnail (${entity.avatar_source ?? 'wiki'})`}
							class="w-16 h-16 rounded-lg object-cover border border-border
								group-hover:border-primary/60 transition-colors bg-muted"
							loading="lazy"
							onerror={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none'; }} />
					</a>
				{/if}
				<div class="min-w-0 flex-1">
					<div class="flex items-center gap-2.5 mb-1 flex-wrap">
						<svelte:component this={TypeIcon} size={20} class="text-primary shrink-0" />
						<h1 class="text-xl font-semibold text-foreground truncate">
							{entity.kb_canonical_name || entity.canonical_name}
						</h1>
						{#if entity.kb_canonical_name && entity.kb_canonical_name !== entity.canonical_name}
							<span class="text-xs text-muted-foreground"
								title={`Stored in substrate as "${entity.canonical_name}"`}>
								(stored as "{entity.canonical_name}")
							</span>
						{/if}
						{#if entity.base60_address}
							<span class="text-[11px] font-mono px-2 py-0.5 rounded bg-primary/10 text-primary/90 border border-primary/20"
								title="Base-60 substrate address — stable identifier for federation">
								{entity.base60_address}
							</span>
						{/if}
						{#if entity.canonical_type}
							<span class="text-[10px] uppercase tracking-wider text-muted-foreground/80 bg-muted px-1.5 py-0.5 rounded">
								{entity.canonical_type}
							</span>
						{/if}
						{#if entity.entity_kind && entity.entity_kind !== 'instance'}
							<span class="text-[10px] uppercase tracking-wider text-amber-400/80 bg-amber-500/10 px-1.5 py-0.5 rounded">
								{entity.entity_kind}
							</span>
						{/if}
						{#if entity.federation && entity.federation.n_nodes > 1}
							<span class="text-[10px] px-2 py-0.5 rounded bg-primary/15 text-primary border border-primary/30"
								title={`Federation: ${entity.federation.n_nodes} nodes recognize this entity (network signal count: ${entity.federation.network_signal_count})`}>
								🌐 +{entity.federation.n_nodes - 1} node{entity.federation.n_nodes === 2 ? '' : 's'}
							</span>
						{/if}
						{#if substrate?.form && substrate.bindings.length >= 1}
							<button type="button"
								onclick={() => (substrateOpen = !substrateOpen)}
								class="text-[10px] px-2 py-0.5 rounded font-mono
									{substrate.bindings.length > 1 ? 'bg-amber-500/15 text-amber-300 border border-amber-500/30 hover:bg-amber-500/25' : 'bg-muted text-muted-foreground/80 border border-border hover:bg-muted/60'}"
								title={substrate.bindings.length > 1
									? `Form "${substrate.form.surface_form}" binds to ${substrate.bindings.length} concepts in the substrate — multiple referents share this surface form`
									: `Form "${substrate.form.surface_form}" binds to ${substrate.bindings.length} concept`}>
								⊕ {substrate.bindings.length} binding{substrate.bindings.length === 1 ? '' : 's'}
							</button>
						{/if}
					</div>

					{#if entity.description}
						<p class="text-sm text-foreground/80 leading-snug mb-1.5"
							data-researchable
							data-node-id={`entity:${entity_id}`}>
							{entity.description}
							{#if entity.description_source}
								<a href={`https://en.wikipedia.org/wiki/${encodeURIComponent(entity.description_source.split(':').pop())}`}
									target="_blank" rel="noopener"
									class="text-[10px] text-muted-foreground/60 hover:text-primary ml-1">
									Wikipedia ↗
								</a>
							{/if}
						</p>
					{:else if !loading && (entity.signal_count ?? 0) > 5}
						<p class="text-xs text-muted-foreground/70 italic mb-1.5">
							No description yet —
							<button type="button" onclick={() => triggerResearch(false)}
								disabled={researchBusy || !!activeInvestigation}
								class="text-primary hover:underline disabled:opacity-50">
								run research to pull from Wikipedia/SEC/FEC →
							</button>
						</p>
					{/if}

					<div class="flex items-center gap-4 text-xs text-muted-foreground">
						<span>{entity.canonical_type || 'unknown'} · {entity.entity_kind}</span>
						<span>{entity.signal_count ?? 0} mentions</span>
						<span class={(entity.fact_count ?? 0) > 0 ? 'text-foreground' : ''}>{entity.fact_count ?? 0} facts</span>
						<span class={(entity.event_count ?? 0) > 0 ? 'text-foreground' : ''}>{entity.event_count ?? 0} events</span>
						{#if hasAliases}
							<span>{entity.aliases.length} alias{entity.aliases.length === 1 ? '' : 'es'}</span>
						{/if}
					</div>
				</div>

				<div class="flex items-center gap-1.5 shrink-0">
					{#if geo}
						<a href={`/map?focus=${entity_id}&lat=${geo.lat}&lon=${geo.lon}`}
							class="inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded-md border border-border hover:border-primary/50 hover:bg-primary/5 text-foreground/80 hover:text-primary">
							<MapPin size={12} />
							Map
						</a>
						{#if satelliteConfigured}
							<button onclick={() => captureOverhead('TRUE_COLOR')} disabled={satCapturing}
								title="Pull recent Sentinel-2 overhead imagery for this location and file it as signed evidence"
								class="inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded-md border border-border hover:border-primary/50 hover:bg-primary/5 text-foreground/80 hover:text-primary disabled:opacity-50">
								{#if satCapturing}
									<Loader2 size={12} class="animate-spin" /> Capturing…
								{:else}
									<Satellite size={12} /> Capture overhead
								{/if}
							</button>
						{/if}
					{/if}
					{#if activeInvestigation || liveResearch}
						<span class="inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px] text-emerald-400 border border-emerald-500/30 rounded-md bg-emerald-500/5"
							title="Auto-triggered research from this view — findings appear as they arrive">
							<Loader2 size={12} class="animate-spin" />
							{activeInvestigation?.status === 'running' ? 'researching live' : (activeInvestigation?.status || 'researching')}
						</span>
					{:else}
						<button onclick={() => triggerResearch(!!lastComplete)}
							disabled={researchBusy}
							title="Run the full investigation pipeline (all live sources). Use the Enrichment panel below to run a single source."
							class="inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
							{#if researchBusy}
								<Loader2 size={12} class="animate-spin" /> Queuing…
							{:else if lastComplete}
								<Search size={12} /> Re-investigate
							{:else}
								<Search size={12} /> Investigate
							{/if}
						</button>
					{/if}
				</div>
			</div>

			{#if satError || satCaptures.length}
				<div class="mt-3 pt-3 border-t border-border/40">
					<div class="flex items-center gap-2 mb-2">
						<Satellite size={13} class="text-sky-400" />
						<span class="text-xs font-medium text-foreground">Overhead imagery</span>
						<span class="text-[10px] text-muted-foreground">Sentinel-2 · signed evidence</span>
					</div>
					{#if satError}<p class="text-[11px] text-red-400 mb-2">⚠ {satError}</p>{/if}
					{#if satCaptures.length}
						<div class="flex gap-2 overflow-x-auto pb-1">
							{#each satCaptures as cap}
								<a href={cap.file_url} target="_blank" rel="noopener" class="shrink-0 w-40 group">
									<img src={cap.file_url} alt={cap.title} loading="lazy"
										class="w-40 h-40 object-cover rounded-md border border-border group-hover:border-primary/50" />
									<p class="text-[10px] text-muted-foreground mt-1 truncate" title={cap.title}>{cap.title}</p>
									{#if cap.issued_date}<p class="text-[9px] text-muted-foreground/70">{cap.issued_date}</p>{/if}
								</a>
							{/each}
						</div>
					{/if}
				</div>
			{/if}

			{#if hasIdentifiers || hasAliases || hasConcept}
				<div class="flex flex-wrap items-baseline gap-x-5 gap-y-1 mt-3 pt-3 border-t border-border/40 text-xs">
					{#if hasConcept}
						<span class="text-muted-foreground">
							concept:
							<a href={`/entity/${entity.concept_parent.entity_id}${lensQueryFirst}`}
								class="text-primary hover:underline ml-1">{entity.concept_parent.canonical_name}</a>
						</span>
					{/if}
					{#if hasIdentifiers}
						{#each Object.entries(entity.identifiers) as [k, v]}
							<span class="text-muted-foreground">
								{k}: <span class="text-foreground font-mono">{String(v)}</span>
							</span>
						{/each}
					{/if}
					{#if hasAliases && entity.aliases.length <= 6}
						<span class="text-muted-foreground">
							aliases: <span class="text-foreground">{entity.aliases.join(', ')}</span>
						</span>
					{/if}
				</div>
			{/if}
		</header>

		<!-- SUBSTRATE drill-down (toggled by the binding chip in header).
		     Shows the form's binding distribution — which concepts share
		     this surface form. Operator can navigate to each. -->
		{#if substrateOpen && substrate?.form}
			<section class="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 mb-4">
				<div class="flex items-baseline justify-between mb-2">
					<h3 class="text-sm font-semibold text-foreground">
						Substrate · form
						<span class="font-mono text-foreground/80">"{substrate.form.surface_form}"</span>
					</h3>
					<span class="text-[10px] uppercase tracking-wider text-muted-foreground">
						{substrate.form.form_address}
					</span>
				</div>
				<p class="text-xs text-muted-foreground mb-3">
					This surface form has {substrate.bindings.length} active binding{substrate.bindings.length === 1 ? '' : 's'} in the substrate.
					Each binding maps to one real-world referent (concept). Future ingestion of this name routes
					to one of these concepts via Layer 0 binding consumption.
				</p>
				<ul class="space-y-1.5">
					{#each substrate.bindings as b (b.concept_id)}
						<li class="flex items-baseline gap-3 text-xs">
							<span class="text-[10px] font-mono text-muted-foreground/70 w-20 shrink-0">
								{b.concept_address || '—'}
							</span>
							{#if b.promoted_to_entity_id}
								<a href={`/entity/${b.promoted_to_entity_id}`}
									class="text-foreground hover:text-primary hover:underline flex-1 truncate"
									class:font-semibold={b.is_self}>
									{b.canonical_name || `concept ${b.concept_id}`}
									{#if b.is_self}<span class="text-[10px] text-emerald-400 ml-1">★ this entity</span>{/if}
								</a>
							{:else}
								<span class="text-muted-foreground/80 italic flex-1 truncate">
									{b.canonical_name || `concept ${b.concept_id}`} (not promoted)
								</span>
							{/if}
							{#if b.canonical_subtype}
								<span class="text-[10px] text-muted-foreground/60 shrink-0">
									{b.canonical_subtype}
								</span>
							{/if}
							<span class="text-[10px] text-muted-foreground/50 shrink-0">
								{b.binding_source}
							</span>
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		<!-- ENRICHMENT — operator-triggered, per-type. Replaces the old
		     auto-queue that fired every enrichment on every entity. The
		     operator picks the specific lookup that matters for this case. -->
		{#if enrichmentItems.length > 0}
			<section class="rounded-xl border border-border bg-card p-4 mb-4">
				<div class="flex items-baseline justify-between mb-3">
					<h2 class="text-sm font-semibold text-foreground">Enrichment</h2>
					<span class="text-[10px] uppercase tracking-wider text-muted-foreground">
						operator-triggered
					</span>
				</div>
				<ul class="space-y-1.5">
					{#each enrichmentItems as it (it.type)}
						{@const lr = it.last_run}
						{@const isBusy = !!enrichmentBusy[it.type]}
						{@const inFlight = !!lr && ['pending','running'].includes(lr.status)}
						<li class="flex items-center gap-3 py-1.5 border-b border-border/30 last:border-0">
							<div class="flex-1 min-w-0">
								<div class="flex items-baseline gap-2">
									<span class="text-xs font-medium text-foreground capitalize">
										{it.type.replace(/_/g, ' ')}
									</span>
									{#if lr}
										<span class="text-[10px] px-1.5 py-0.5 rounded {lr.status === 'complete' ? 'bg-emerald-500/15 text-emerald-300' : lr.status === 'failed' ? 'bg-rose-500/15 text-rose-300' : lr.status === 'skipped' ? 'bg-muted text-muted-foreground' : 'bg-amber-500/15 text-amber-300'}">
											{lr.status}
										</span>
									{/if}
								</div>
								<div class="text-[11px] text-muted-foreground truncate">
									{it.description}
									{#if it.sources?.length}
										<span class="text-muted-foreground/60"> · {it.sources.join(', ')}</span>
									{/if}
								</div>
							</div>
							<button onclick={() => triggerEnrichment(it.type)}
								disabled={isBusy || inFlight}
								class="shrink-0 inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded-md border border-border hover:border-primary/50 hover:bg-primary/5 text-foreground/80 hover:text-primary disabled:opacity-40">
								{#if isBusy || inFlight}
									<Loader2 size={12} class="animate-spin" />
									{inFlight ? lr.status : 'queuing'}
								{:else if lr && lr.status === 'complete'}
									<Search size={12} /> Re-run
								{:else}
									<Search size={12} /> Run
								{/if}
							</button>
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		<!-- PROFILE — the anchor cluster rendered as a surface-level read
		     about THE PERSON (or place / org / event). Source contrast is
		     shown inline so the dossier reads as a profile from first
		     glance, not a "candidates to evaluate" list. -->
		{@const anchorCluster = personaClusters.find(c => c.anchor)}
		{@const namesakeClusters = personaClusters.filter(c => !c.anchor)}
		{#if anchorCluster}
			<section class="rounded-xl border border-border bg-card p-5 mb-4"
				data-researchable
				data-node-id={`entity:${entity_id}`}>
				<div class="flex items-baseline justify-between mb-3">
					<h2 class="text-sm font-semibold text-foreground">Profile</h2>
					<span class="text-[10px] uppercase tracking-wider text-muted-foreground">
						{anchorCluster.canonical_subtype} · tier {anchorCluster.evidence_tier}
					</span>
				</div>
				{#if foldedSameName > 0}
					<p class="text-[11px] text-muted-foreground/70 -mt-2 mb-3">
						+{foldedSameName} same-name mention{foldedSameName === 1 ? '' : 's'} folded in (same referent, differing only by context)
					</p>
				{/if}

				<!-- Surface attribute chips inline so DOB / QID / region are
				     visible without expanding anything. -->
				<div class="flex flex-wrap gap-1.5 mb-4 text-[11px]">
					{#each Object.entries(anchorCluster.dominant_attrs) as [k, v]}
						{#if Array.isArray(v)}
							{#if v.length > 0}
								<span class="px-1.5 py-0.5 rounded bg-muted/40 text-muted-foreground">
									{k}: {v.join(', ')}
								</span>
							{/if}
						{:else if v && typeof v === 'object'}
							<span class="px-1.5 py-0.5 rounded bg-muted/40 text-muted-foreground">
								{k}: {Object.entries(v).map(([kk, vv]) => `${kk} ${vv}`).join(', ')}
							</span>
						{:else if v != null && v !== false}
							<span class="px-1.5 py-0.5 rounded bg-muted/40 text-muted-foreground">
								{k}: {String(v)}
							</span>
						{/if}
					{/each}
					{#each Object.entries(anchorCluster.identifiers) as [k, v]}
						<span class="px-1.5 py-0.5 rounded bg-sky-500/15 text-sky-300 font-mono">
							{k}={v}
						</span>
					{/each}
				</div>

				<!-- Structured facts — Wikidata + FEC + OpenCorporates +
				     etc. properties extracted with provenance. Grouped by
				     predicate so multi-source corroboration shows side-by-
				     side (e.g. occupation per Wikidata + per LOC). -->
				{#if anchorCluster.properties && anchorCluster.properties.length > 0}
					{@const propsByPredicate = anchorCluster.properties.reduce((acc, p) => {
						(acc[p.predicate] ||= []).push(p);
						return acc;
					}, {} as Record<string, typeof anchorCluster.properties>)}
					<div class="rounded-md border border-border/60 bg-background/30 p-3 mb-4">
						<div class="text-[10px] uppercase tracking-wider text-muted-foreground mb-2">
							Facts ({Object.keys(propsByPredicate).length})
						</div>
						<dl class="grid grid-cols-[140px_1fr] gap-x-3 gap-y-1.5 text-xs">
							{#each Object.entries(propsByPredicate) as [pred, rows]}
								<dt class="text-muted-foreground self-start pt-0.5">{pred}</dt>
								<dd class="flex flex-wrap gap-x-3 gap-y-1 items-baseline">
									{#each rows as r, i}
										<span class="inline-flex items-baseline gap-1.5">
											<!-- VALUE: links to value_url (the thing itself) when present -->
											{#if r.value_url}
												<a href={r.value_url} target="_blank" rel="noopener"
													class="text-foreground hover:text-primary hover:underline break-all">
													{r.value}
												</a>
											{:else}
												<span class="text-foreground">{r.value}</span>
											{/if}
											<!-- PROVENANCE: source chip links to source_url -->
											{#if r.source_url}
												<a href={r.source_url} target="_blank" rel="noopener"
													class="text-[10px] text-muted-foreground hover:text-primary hover:underline"
													title="See source: {r.source_label}">
													[{r.source_label}]
												</a>
											{:else}
												<span class="text-[10px] text-muted-foreground">
													[{r.source_label}]
												</span>
											{/if}
											{#if i < rows.length - 1}
												<span class="text-muted-foreground/40">·</span>
											{/if}
										</span>
									{/each}
								</dd>
							{/each}
						</dl>
					</div>
				{/if}

				<!-- Source contrast — claim prose visible by default, one
				     section per source. This IS the dossier read; nothing
				     hidden behind a click. -->
				{#if anchorCluster.findings_by_source && anchorCluster.findings_by_source.length > 0}
					<div class="space-y-4">
						{#each anchorCluster.findings_by_source as group (group.source)}
							<div class="pl-3 border-l-2 border-border">
								<div class="flex items-baseline gap-2 mb-1.5">
									<span class="text-xs uppercase tracking-wider font-semibold text-foreground/80">
										{group.source_label}
									</span>
									{#if group.count > group.unique_count}
										<span class="text-[10px] text-muted-foreground">
											{group.unique_count} unique · {group.count} mentions
										</span>
									{:else if group.count > 1}
										<span class="text-[10px] text-muted-foreground">
											{group.count} mentions
										</span>
									{/if}
								</div>
								<ul class="space-y-2">
									{#each group.unique_claims as item}
										<li class="text-xs">
											{#if item.claim}
												<p class="text-foreground/90 leading-relaxed">
													{item.claim.length > 600 ? item.claim.slice(0, 600) + '…' : item.claim}
												</p>
											{:else if item.title}
												<p class="text-foreground/90">{item.title}</p>
											{/if}
											{#if item.source_url}
												<a href={item.source_url} target="_blank" rel="noopener"
													class="text-[11px] text-primary hover:underline">
													{item.title || group.source_label} ↗
												</a>
											{:else if item.title && item.claim}
												<span class="text-[11px] text-muted-foreground">{item.title}</span>
											{/if}
										</li>
									{/each}
								</ul>
							</div>
						{/each}
					</div>
				{/if}
			</section>
		{/if}

		<!-- LINKED ORGANIZATIONS — clusters that carry their own strong
		     identifier (committee_id / EIN / CIK …) and already resolve to a
		     distinct entity. They reference this entity but ARE NOT it, so they
		     are shown as links, not namesake-split candidates. -->
		{#if resolvedOrgs.length > 0}
			<section class="rounded-xl border border-sky-500/30 bg-sky-500/5 p-5 mb-4">
				<div class="flex items-baseline justify-between mb-2">
					<h2 class="text-sm font-semibold text-foreground">
						Linked organizations
						<span class="text-sky-300 ml-1">· {resolvedOrgs.length}</span>
					</h2>
					<span class="text-[10px] uppercase tracking-wider text-muted-foreground">
						own identifier → distinct entity
					</span>
				</div>
				<p class="text-xs text-muted-foreground mb-3">
					These carry a registry identifier of their own, so they collapse
					as separate entities that reference this one — not aliases. Full
					detail (filings, officers, addresses) is in <span class="text-primary">Companies</span>.
				</p>
				<ul class="space-y-1.5">
					{#each resolvedOrgs as o (o.cluster_id)}
						<li class="flex items-center gap-2 flex-wrap rounded-md border border-border bg-background/40 px-3 py-2">
							<Building2 size={13} class="text-muted-foreground shrink-0" />
							<a href="/entity/{o.resolved_entity_id}" class="text-sm font-medium text-foreground hover:text-primary truncate">
								{o.resolved_entity_name || o.label}
							</a>
							{#each Object.entries(o.resolved_via || {}) as [k, v]}
								<span class="text-[9px] font-mono px-1 py-0.5 rounded bg-sky-500/15 text-sky-300 shrink-0">{k}={v}</span>
							{/each}
							<span class="text-[10px] text-muted-foreground/70">{o.canonical_subtype}</span>
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		<!-- NAMESAKES — other clusters sharing this form. Framed as
		     "this name is also held by N other referents" so the operator
		     understands the anchor is contextual, not authoritative. -->
		{#if namesakeClusters.length > 0}
			<section class="rounded-xl border border-amber-500/40 bg-amber-500/5 p-5 mb-4"
				data-researchable
				data-node-id={`entity:${entity_id}`}>
				<div class="flex items-baseline justify-between mb-2">
					<h2 class="text-sm font-semibold text-foreground">
						Also called <span class="font-mono">"{personaClusters[0]?.label?.split(' (')[0] || 'this name'}"</span>
						<span class="text-amber-300 ml-1">· {namesakeClusters.length} other referent{namesakeClusters.length === 1 ? '' : 's'}</span>
					</h2>
					<span class="text-[10px] uppercase tracking-wider text-muted-foreground">
						form binds → {personaClusters.length} concepts
					</span>
				</div>
				<p class="text-xs text-muted-foreground mb-3">
					The current anchor is contextual (whichever real-world
					referent Wikipedia disambiguates this surface form to).
					Review the others — split any that belong as their own
					entities.
				</p>

				<ul class="space-y-2">
					{#each namesakeClusters as c (c.cluster_id)}
						<li class="rounded-md border border-border bg-background/40 p-3">
							<div class="flex items-start justify-between gap-3">
								<div class="flex-1 min-w-0">
									<div class="flex items-center gap-2 mb-1 flex-wrap">
										<span class="text-[10px] uppercase tracking-wider text-muted-foreground">
											{c.canonical_subtype} · tier {c.evidence_tier} · {c.finding_ids.length} finding{c.finding_ids.length === 1 ? '' : 's'}
										</span>
									</div>
									<input
										type="text"
										bind:value={personaEditNames[c.cluster_id]}
										class="w-full text-sm font-medium bg-transparent border-b border-border/40 focus:border-amber-400 focus:outline-none py-0.5"
									/>
									<div class="flex flex-wrap gap-1.5 mt-2 text-[11px]">
										{#each Object.entries(c.dominant_attrs) as [k, v]}
											{#if Array.isArray(v)}
												{#if v.length > 0}
													<span class="px-1.5 py-0.5 rounded bg-muted/40 text-muted-foreground">
														{k}: {v.join(', ')}
													</span>
												{/if}
											{:else if v && typeof v === 'object'}
												<span class="px-1.5 py-0.5 rounded bg-muted/40 text-muted-foreground">
													{k}: {Object.entries(v).map(([kk, vv]) => `${kk} ${vv}`).join(', ')}
												</span>
											{:else if v != null && v !== false}
												<span class="px-1.5 py-0.5 rounded bg-muted/40 text-muted-foreground">
													{k}: {String(v)}
												</span>
											{/if}
										{/each}
										{#each Object.entries(c.identifiers) as [k, v]}
											<span class="px-1.5 py-0.5 rounded bg-sky-500/15 text-sky-300 font-mono">
												{k}={v}
											</span>
										{/each}
									</div>
									{#if c.findings_by_source && c.findings_by_source.length > 0}
										<details class="mt-2">
											<summary class="text-[11px] text-muted-foreground cursor-pointer hover:text-foreground select-none">
												{c.finding_ids.length} mention{c.finding_ids.length === 1 ? '' : 's'} across {c.findings_by_source.length} source{c.findings_by_source.length === 1 ? '' : 's'} →
											</summary>
											<div class="mt-2 space-y-2">
												{#each c.findings_by_source as group (group.source)}
													<div class="pl-2 border-l border-border/50">
														<div class="text-[10px] uppercase tracking-wider font-semibold text-foreground/80 mb-0.5">
															{group.source_label}{group.count > 1 ? ` (${group.count})` : ''}
														</div>
														<ul class="space-y-1">
															{#each group.unique_claims as item}
																<li class="text-[11px]">
																	{#if item.claim}
																		<p class="text-foreground/90 leading-snug">
																			{item.claim.length > 200 ? item.claim.slice(0, 200) + '…' : item.claim}
																		</p>
																	{:else if item.title}
																		<p class="text-foreground/90">{item.title}</p>
																	{/if}
																	{#if item.source_url}
																		<a href={item.source_url} target="_blank" rel="noopener"
																			class="text-[10px] text-primary hover:underline">
																			{item.title || group.source_label} ↗
																		</a>
																	{/if}
																</li>
															{/each}
														</ul>
													</div>
												{/each}
											</div>
										</details>
									{/if}
								</div>
								<div class="shrink-0 flex flex-col items-end gap-1">
									<button
										class="text-xs px-2.5 py-1 rounded border border-amber-500/50 text-amber-300 hover:bg-amber-500/15 disabled:opacity-50"
										disabled={personaSplitting === c.cluster_id}
										onclick={() => submitClusterSplit(c)}
									>
										{personaSplitting === c.cluster_id ? 'splitting…' : 'Split off →'}
									</button>
								</div>
							</div>
						</li>
					{/each}
				</ul>

				{#if personaSplitError}
					<p class="mt-2 text-xs text-amber-300">{personaSplitError}</p>
				{/if}
				{#if personaSplitNewId}
					<p class="mt-2 text-xs text-emerald-300">
						Split into its own entity →
						<a href="/entity/{personaSplitNewId}" class="font-medium text-primary hover:underline">{personaSplitNewName} ↗</a>
					</p>
				{/if}
			</section>
		{/if}


		<!-- ─── Overview: at-a-glance summary (default open). 6 high-signal
		     cells + a context strip for things that don't deserve their own cell. -->
		<Disclosure id="overview" scope={`entity-${entity_id}`}
			title="Overview"
			count={null}
			subtitle="at a glance"
			defaultOpen={true}>
			<!-- Stat tiles: only show non-zero ones so the card doesn't fill
			     with empty cells for entities that don't have that data type
			     (a country has 0 Properties/Relations/Assets and they used
			     to dominate the header — now they're hidden until there's
			     something to count). -->
			{@const tiles = [
				{ label: 'Properties', value: propertyGroups.length },
				{ label: 'Relations',  value: facts.length },
				{ label: 'Assets',     value: assetCount },
				{ label: 'Documents',  value: documentAppearances.length },
				{ label: 'Topics',     value: topicMemberships.length },
				{ label: 'Story',      value: story.total ?? 0 },
			].filter(t => (t.value || 0) > 0)}
			{#if tiles.length > 0}
				<div class="grid gap-2 text-xs mb-2 grid-cols-3 md:grid-cols-6">
					{#each tiles as t}
						<div class="rounded-md bg-muted/30 p-2.5">
							<div class="text-[10px] uppercase tracking-wider text-muted-foreground/70 mb-1">
								{t.label}
							</div>
							<div class="text-lg font-semibold tabular-nums text-foreground">
								{t.value}
							</div>
						</div>
					{/each}
				</div>
			{/if}
			<!-- Context strip: facts that don't deserve their own cell -->
			<div class="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[11px] text-muted-foreground/80 pl-1">
				{#if timelineBounds}
					<span>{timelineBounds.min}–{timelineBounds.max} timeline span</span>
				{/if}
				{#if assetGroups.length > 0}
					<span>· {assetGroups.map((g: any) => `${g.count} ${g.label.toLowerCase()}`).join(', ')}</span>
				{/if}
				{#if story.total > 0}
					<span>· {kindCounts.fact}f · {kindCounts.event}e · {kindCounts.finding}fi</span>
				{/if}
				{#if topicMemberships.length > 0}
					<span class="truncate max-w-[40ch]">· top topic: {topicMemberships[0].canonical_name}</span>
				{/if}
				{#if lastComplete}
					<span>· {investigations.length} investigation{investigations.length===1?'':'s'} (latest: {lastComplete.findings_count ?? 0} findings)</span>
				{:else if activeInvestigation}
					<span class="text-emerald-400/90">· researching live…</span>
				{/if}
			</div>
		</Disclosure>

		<!-- ─── Properties panel (typed property assertions w/ collapse algebra) ─── -->
		{#if propertyGroups.length > 0}
			<Disclosure id="properties" scope={`entity-${entity_id}`}
				title="Properties"
				count={propertyGroups.length}
				subtitle="typed via propertyvocab · canonical · attestations · alternatives"
				defaultOpen={true}>
				<div class="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-3 text-sm">
					{#each propertyGroups as g (g.property_key)}
						<div class="border-l-2 border-emerald-500/40 pl-3 py-1">
							<div class="flex items-baseline gap-2">
								<span class="text-foreground font-medium">{g.property_label}</span>
								<span class="text-[10px] text-muted-foreground/60 uppercase tracking-wider">{g.property_key}</span>
								<span class="text-[10px] text-muted-foreground/60 ml-auto">
									{g.total_assertions} assertion{g.total_assertions === 1 ? '' : 's'}
								</span>
							</div>

							<!-- CANONICAL (deepest assertion in the dominant chain) -->
							<div class="mt-1.5 flex items-baseline gap-2">
								<span class="text-foreground text-sm">{g.canonical.value_label}</span>
								<span class="text-[9px] uppercase tracking-wider px-1 py-0.5 rounded
									{g.canonical.precision_tier === 'spectral' ? 'bg-purple-500/20 text-purple-300'
									: g.canonical.precision_tier === 'formal' ? 'bg-blue-500/20 text-blue-300'
									: g.canonical.precision_tier === 'qualified' ? 'bg-emerald-500/20 text-emerald-300'
									: g.canonical.precision_tier === 'enumerated' ? 'bg-amber-500/20 text-amber-300'
									: 'bg-muted text-muted-foreground'}"
									title={`Precision depth ${g.canonical.precision_depth}`}>
									{g.canonical.precision_tier}
								</span>
								{#if g.canonical.corroborated_by_count > 1}
									<span class="text-[10px] text-emerald-400/80 tabular-nums">
										{g.canonical.corroborated_by_count}× corroborated
									</span>
								{/if}
								{#if g.canonical.contradicted_by_count > 0}
									<span class="text-[10px] text-amber-400/80 tabular-nums" title="Contested by other assertions">
										{g.canonical.contradicted_by_count}× contested
									</span>
								{/if}
							</div>

							<!-- ATTESTATIONS (shallower values in same chain) -->
							{#if g.attestations && g.attestations.length > 0}
								<div class="mt-1 pl-3 text-[11px] text-muted-foreground/80">
									<span class="opacity-60">↳ also asserted: </span>
									{#each g.attestations as a, i}
										<span class="text-foreground/85">{a.value_label}</span><span class="text-[9px] opacity-60 ml-0.5">({a.precision_tier})</span>{#if i < g.attestations.length - 1}<span>, </span>{/if}
									{/each}
								</div>
							{/if}

							<!-- ALTERNATIVES (different chain, contested) -->
							{#if g.alternatives && g.alternatives.length > 0}
								<div class="mt-1 pl-3 text-[11px]">
									<span class="text-amber-400/70 opacity-80">⚠ also claimed: </span>
									{#each g.alternatives as a, i}
										<span class="text-amber-300/90">{a.value_label}</span><span class="text-[9px] text-amber-300/60 ml-0.5">({a.precision_tier})</span>{#if i < g.alternatives.length - 1}<span class="text-amber-300/60">, </span>{/if}
									{/each}
								</div>
							{/if}
						</div>
					{/each}
				</div>
			</Disclosure>
		{/if}

		<!-- MERGED_RELATIONS ─── Relations panel (verb graph: predicate-grouped facts) ─── -->
		{#if relationGroups.length > 0}
			<Disclosure id="network" scope={`entity-${entity_id}`}
				title="Connections"
				count={`${relationGroups.length} predicates · ${facts.length} facts`}
				defaultOpen={true}>
				<ConnectionWeb entityId={entity_id} focalName={entity?.canonical_name || ''} {lensQueryFirst} />
			</Disclosure>
		{/if}
		<!-- MERGED_CHRONO ─── Story stream (chronological year-grouped rows) ─── -->
		<Disclosure id="story" scope={`entity-${entity_id}`}
			title="Timeline"
			count={story.total ?? 0}
			subtitle="what happened, when">
			<!-- Filter chips at top of section -->
			<div class="flex items-center justify-end gap-1 text-[10px] mb-3">
				{#each ['all', 'fact', 'event', 'finding', 'mention'] as const as k}
					<button onclick={() => filter = k}
						class={`px-2 py-0.5 rounded-md transition-colors ${
							filter === k ? 'bg-primary/15 text-primary' : 'text-muted-foreground hover:text-foreground'
						}`}>
						{k} <span class="opacity-60 tabular-nums">{kindCounts[k]}</span>
					</button>
				{/each}
			</div>

			<!-- Processing coverage — makes incompleteness explicit rather than
			     showing a thin story as if it were the whole picture. -->
			{#if story.processing}
				{@const pr = story.processing}
				{#if pr.pending_documents > 0}
					<div class="mb-3 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-[11px] text-amber-200/90 flex items-center gap-2">
						<span class="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse shrink-0"></span>
						<span>
							<span class="font-medium tabular-nums">{pr.documents_collapsed} of {pr.documents}</span> documents processed —
							<span class="tabular-nums">{pr.pending_documents}</span> still collapsing.
							{#if pr.structured_rows === 0}This story is showing raw mentions; facts, events and locations will fill in as extraction completes.{:else}More facts and events will appear as it finishes.{/if}
						</span>
					</div>
				{:else if pr.documents > 0 && pr.structured_rows === 0 && pr.mentions > 0}
					<div class="mb-3 rounded-md border border-border bg-muted/20 px-3 py-2 text-[11px] text-muted-foreground flex items-center gap-2">
						<span class="w-1.5 h-1.5 rounded-full bg-slate-400 shrink-0"></span>
						<span>All {pr.documents} document{pr.documents === 1 ? '' : 's'} processed, but nothing collapsed into structured facts or events yet — showing {pr.mentions} raw mention{pr.mentions === 1 ? '' : 's'}.</span>
					</div>
				{/if}
			{/if}

			{#if filteredGroups.length === 0}
				<div class="rounded-md border border-dashed border-border px-4 py-8 text-center">
					<p class="text-sm text-muted-foreground">Nothing in the story yet.</p>
					{#if !activeInvestigation && !lastComplete}
						<button onclick={() => triggerResearch(false)}
							class="mt-2 text-xs text-primary hover:underline">
							Run research to populate from external sources →
						</button>
					{/if}
				</div>
			{:else}
				<div class="space-y-5">
					{#each filteredGroups as group (group.year)}
						<div>
							<div class="flex items-baseline gap-2 mb-1.5 sticky top-0 bg-background/95 backdrop-blur-sm py-1 z-10">
								<h3 class="text-lg font-semibold text-primary tabular-nums">{group.year}</h3>
								<span class="text-[10px] text-muted-foreground uppercase tracking-wider">
									{group.count} {group.count === 1 ? 'item' : 'items'}
								</span>
							</div>
							<div>
								{#each group.rows as row}
									<ChronoRow {row} {lens} />
								{/each}
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</Disclosure>
		<!-- Relationships (verb graph: predicate-grouped facts) — moved below Timeline -->
		{#if relationGroups.length > 0}
			<Disclosure id="relations" scope={`entity-${entity_id}`}
				title="Relationships"
				count={`${relationGroups.length} predicates · ${facts.length} facts`}
				defaultOpen={true}>
				<div class="flex items-baseline justify-end mb-2">
					{#if relationGroups.length > 8}
						<button onclick={() => showAllRelations = !showAllRelations}
							class="text-[11px] text-muted-foreground hover:text-primary">
							{showAllRelations ? 'show top 8' : `show all ${relationGroups.length}`}
						</button>
					{/if}
				</div>
				<div class="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-3 text-sm">
					{#each visibleRelations as g (g.key)}
						{@const isExpanded = expandedPredicate === g.key}
						{@const displayLimit = isExpanded ? 50 : 3}
						<div class="border-l-2 border-border/40 pl-3 py-1
							{isExpanded ? 'border-primary/60' : ''}">
							<button onclick={() => expandedPredicate = isExpanded ? null : g.key}
								class="flex items-baseline gap-1.5 text-left w-full group">
								<span class="text-[10px] text-muted-foreground/70 tabular-nums w-3 shrink-0">
									{g.direction === 'out' ? '→' : '←'}
								</span>
								<span class="text-foreground font-medium group-hover:text-primary truncate">
									{_prettyPredicate(g.predicate)}
								</span>
								<span class="text-[10px] text-muted-foreground/60 tabular-nums ml-auto shrink-0">
									{g.targets.length}{g.total_corroboration > g.targets.length ? ` · ${g.total_corroboration}×` : ''}
								</span>
							</button>
							<!-- One row per target, with source-statement context inline.
							     The predicate alone (e.g. "accused") is sterile; the raw_text
							     answers the implicit "of what / how / about" — surfacing it
							     here so each row reads as a near-headline. -->
							<ul class="mt-1.5 pl-4 space-y-1.5">
								{#each g.targets.slice(0, displayLimit) as t (t.assessment_id)}
									{@const firstSrc = t.sources?.[0]}
									{@const ctx = firstSrc?.raw_text
										|| firstSrc?.summarized_text
										|| t.obj_value || ''}
									{@const provenance = _provenanceFor(t)}
									<li>
										<div class="flex items-baseline gap-1.5 text-xs">
											{#if t.entity_id}
												<a href={`/entity/${t.entity_id}${lensQueryFirst}`}
													class="text-foreground hover:text-primary hover:underline shrink-0">
													{t.name}
												</a>
											{:else}
												<span class="text-foreground shrink-0">{t.name}</span>
											{/if}
											{#if t.corroboration > 1}
												<span class="text-[10px] text-amber-400/80 tabular-nums shrink-0"
													title={`${t.corroboration} corroborating source statements`}>
													{t.corroboration}×
												</span>
											{/if}
										</div>
										{#if ctx}
											<div class="text-[11px] text-muted-foreground/75 leading-snug italic line-clamp-2">
												"{ctx.slice(0, 180)}{ctx.length > 180 ? '…' : ''}"
											</div>
										{/if}
										{#if provenance.label}
											<!-- Provenance chip — answers "is this real / where from".
											     Clickable to source signal/document when we have a route. -->
											<div class="mt-0.5 flex items-center gap-1 text-[10px] text-muted-foreground/70">
												<span class="opacity-60">↳</span>
												{#if provenance.href}
													<a href={provenance.href}
														class="px-1.5 py-px rounded bg-muted/50 hover:bg-primary/10
															text-foreground/70 hover:text-primary
															border border-border/40 hover:border-primary/30">
														{provenance.label}
													</a>
												{:else}
													<span class="px-1.5 py-px rounded bg-muted/50 text-foreground/70
														border border-border/40">
														{provenance.label}
													</span>
												{/if}
												{#if t.corroboration > 1}
													<span class="text-muted-foreground/50">
														+ {t.corroboration - 1} more
														{t.corroboration === 2 ? 'source' : 'sources'}
													</span>
												{/if}
											</div>
										{/if}
									</li>
								{/each}
								{#if !isExpanded && g.targets.length > displayLimit}
									<li class="text-[11px] text-muted-foreground/60">
										+{g.targets.length - displayLimit} more — click to expand
									</li>
								{/if}
							</ul>
						</div>
					{/each}
				</div>
			</Disclosure>
		{/if}

		<!-- ─── Addresses (place memberships) ─── -->
		<!-- Addresses section: only renders when the entity is the KIND
		     of thing that can be at an address (people, organizations,
		     facilities). Geographic entities (country, continent, region,
		     ocean, planet) are themselves places and have no street
		     address — and the ingest pipelines mistakenly attribute every
		     nonprofit/corporate address to the country they're in, so
		     showing that section on /entity/54 (US) is pure noise. -->
		{@const _geographicTypes = new Set(['country', 'continent', 'region',
			'ocean', 'sea', 'planet', 'state', 'province'])}
		{#if entity?.addresses && entity.addresses.length > 0
			&& !_geographicTypes.has((entity.canonical_type || '').toLowerCase())}
			<Disclosure id="addresses" scope={`entity-${entity_id}`}
				title="Addresses"
				count={entity.addresses.length}>
				<ul class="space-y-2 text-xs">
					{#each entity.addresses.slice(0, 12) as a}
						<li class="border-l-2 border-border pl-2.5">
							<div class="text-foreground/90 leading-snug">
								{#if a.lat != null && a.lon != null}
									<a href={`/map?focus=${entity_id}&lat=${a.lat}&lon=${a.lon}`}
										class="hover:text-primary hover:underline">
										{a.street || a.raw_address || `${a.city || ''} ${a.region || ''}`.trim() || '—'}
									</a>
								{:else}
									{a.street || a.raw_address || `${a.city || ''} ${a.region || ''}`.trim() || '—'}
								{/if}
							</div>
							<div class="text-[10px] text-muted-foreground/70 mt-0.5">
								{#if a.city || a.region || a.country}
									{[a.city, a.region, a.country].filter(Boolean).join(', ')}
								{/if}
								{#if a.address_type}
									<span class="ml-1 px-1 py-0.5 text-[9px] uppercase tracking-wider bg-muted rounded">{a.address_type}</span>
								{/if}
								{#if a.source}
									<span class="ml-1 text-muted-foreground/60">· {a.source}</span>
								{/if}
							</div>
						</li>
					{/each}
				</ul>
			</Disclosure>
		{/if}

		<!-- ─── Cross-library corroboration ─── -->
		{#if corroboration && corroboration.library_count > 0}
			{@const tier = corroboration.tier}
			<Disclosure id="corroboration" scope={`entity-${entity_id}`}
				title="Corroboration"
				count={corroboration.library_count}
				subtitle="independent libraries">
				<div class="mb-2 flex items-center gap-2">
					<span class="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded
						{tier === 'well_corroborated' ? 'bg-emerald-500/20 text-emerald-300' :
						 tier === 'corroborated' ? 'bg-sky-500/20 text-sky-300' :
						 'bg-amber-500/15 text-amber-300'}">
						{tier.replace(/_/g, ' ')}
					</span>
					<span class="text-[11px] text-muted-foreground">
						{corroboration.library_count} librar{corroboration.library_count === 1 ? 'y' : 'ies'} ·
						{corroboration.independent_documents} independent source{corroboration.independent_documents === 1 ? '' : 's'}
					</span>
				</div>
				<ul class="space-y-1 text-xs">
					{#each corroboration.sources as s}
						<li class="flex items-baseline gap-2">
							<span class="w-1.5 h-1.5 rounded-full bg-primary/60 shrink-0 mt-1.5"></span>
							<a href={`/library/${s.library_id}`} class="text-foreground hover:text-primary truncate flex-1">
								{s.title || `Library ${s.library_id}`}
							</a>
							<span class="text-muted-foreground/60 text-[10px] tabular-nums shrink-0">
								{s.mentions}m · {s.documents}d
							</span>
						</li>
					{/each}
				</ul>
			</Disclosure>
		{/if}
		<!-- ─── Assets — only when there's data; otherwise the empty-state
		     text is noise on country/place pages that don't own assets ─── -->
		{#if assetGroups.length > 0}
		<Disclosure id="assets" scope={`entity-${entity_id}`}
			title="Assets"
			count={assetCount}
			subtitle={assetGroups.map(g => `${g.count} ${g.label.toLowerCase()}`).join(' · ')}>
			{#if assetGroups.length === 0}
				<p class="text-xs text-muted-foreground/70 italic py-2">
					No typed asset edges in the substrate for this entity yet.
					Asset relationships (<span class="font-mono">owns / founded_by /
					controls / operated_by / manufactured_by / headquartered_in</span>)
					come from extraction; if they're not here, the corpus didn't yet
					assert them or the extractor missed them.
				</p>
			{:else}
				<div class="space-y-4">
					{#each assetGroups as g (g.category)}
						<div>
							<div class="flex items-baseline gap-2 mb-1.5">
								<h3 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground/90">
									{g.label}
								</h3>
								<span class="text-[10px] text-muted-foreground/60 tabular-nums">{g.count}</span>
							</div>
							<ul class="space-y-1 text-xs">
								{#each g.items as item (item.target_entity_id + '-' + item.edge)}
									<li class="flex items-baseline gap-2 group">
										<span class="text-muted-foreground/60 text-[10px] w-4 shrink-0">
											{item.direction === 'out' ? '→' : '←'}
										</span>
										<a href={`/entity/${item.target_entity_id}${lensQueryFirst}`}
											class="text-foreground hover:text-primary hover:underline truncate flex-1"
											title={item.target_base60 || item.target_type}>
											{item.target_name}
										</a>
										<span class="text-[10px] text-muted-foreground/60 tabular-nums shrink-0">
											{item.edge.replace(/_/g, ' ')}
										</span>
										{#if item.corroboration > 1}
											<span class="text-[10px] text-emerald-400/80 tabular-nums shrink-0"
												title={`Corroborated by ${item.corroboration} source statements`}>
												{item.corroboration}×
											</span>
										{/if}
										{#if item.contested > 0}
											<span class="text-[10px] text-amber-400/80 tabular-nums shrink-0"
												title={`Contested by ${item.contested} alternative assertions`}>
												{item.contested}!
											</span>
										{/if}
									</li>
								{/each}
							</ul>
						</div>
					{/each}
				</div>
			{/if}
		</Disclosure>
		{/if}

		{#if topicMemberships.length > 0}
		<Disclosure id="topics" scope={`entity-${entity_id}`}
			title="Topics"
			count={topicMemberships.length}
			subtitle={topicMemberships.length === 1 ? 'topic membership' : 'topic memberships'}>
			{#if false}
				<p class="text-xs text-muted-foreground/70 italic py-2">
					This entity isn't yet a member of any topic. Topics surface
					when the auto-clustering worker proposes them — or you can
					create one explicitly and add this entity to its
					<span class="font-mono">user_curated</span> membership rule.
				</p>
			{:else}
				<ul class="space-y-1.5 text-xs">
					{#each topicMemberships as t (t.topic_id)}
						<li class="flex items-baseline gap-2">
							<span class="text-muted-foreground/60 text-[10px] uppercase tracking-wider w-16 shrink-0">
								{t.topic_subtype || t.topic_kind || 'topic'}
							</span>
							<a href={`/topic/${t.topic_id}${lensQueryFirst}`}
								class="text-foreground hover:text-primary hover:underline truncate flex-1">
								{t.canonical_name}
							</a>
							<span class="text-[10px] text-muted-foreground/60 tabular-nums shrink-0"
								title={`${t.topic_member_count} total members in this topic`}>
								{t.topic_member_count} members
							</span>
							{#if t.federation && t.federation.n_nodes > 1}
								<span class="text-[10px] px-1.5 py-px rounded
									bg-primary/10 text-primary border border-primary/20
									tabular-nums shrink-0"
									title={`+${t.federation.n_nodes - 1} other nodes corroborate; network member count ${t.federation.network_member_count}`}>
									+{t.federation.n_nodes - 1} corroborate
								</span>
							{/if}
							{#if t.membership_score && t.membership_score > 1}
								<span class="text-[10px] text-emerald-400/80 tabular-nums shrink-0"
									title="Membership score (higher = stronger evidence)">
									{t.membership_score.toFixed(1)}
								</span>
							{/if}
						</li>
					{/each}
				</ul>
			{/if}
		</Disclosure>
		{/if}

		{#if documentAppearances.length > 0}
		<Disclosure id="documents" scope={`entity-${entity_id}`}
			title="Documents"
			count={documentAppearances.length}
			subtitle={`corpus documents · sorted by mention count`}>
			{#if false}
				<p class="text-xs text-muted-foreground/70 italic py-2">
					No documents in the corpus mention this entity yet.
				</p>
			{:else}
				<ul class="space-y-1.5 text-xs">
					{#each documentAppearances as d (d.document_id)}
						<li class="flex items-baseline gap-2">
							<span class="text-[10px] text-muted-foreground/70 tabular-nums w-12 text-right shrink-0">
								{d.mention_count}×
							</span>
							<a href={`/document/${d.document_id}`}
								class="text-foreground hover:text-primary hover:underline flex-1 truncate"
								title={d.title}>
								{d.title}
							</a>
						</li>
					{/each}
				</ul>
			{/if}
		</Disclosure>
		{/if}


		<!-- ─── Companies (corporate footprint: public-record data + filings + officers) ─── -->
		{#if entity?.canonical_type === 'person' || companies.length > 0}
			<Disclosure id="companies-section" scope={`entity-${entity_id}`}
				title="Companies"
				count={companies.length}
				subtitle="owned / run / founded — public-record data, filings, officers"
				defaultOpen={companies.length > 0}>
				<div class="flex items-center justify-between mb-2">
					<span class="text-[11px] text-muted-foreground">Corporate data from SEC, ProPublica, OpenCorporates, state registries.</span>
					<button onclick={researchCompanies} disabled={researchingCompanies}
						class="text-[11px] px-2 py-1 rounded-lg bg-primary/10 text-primary hover:bg-primary/20 disabled:opacity-40">
						{researchingCompanies ? 'researching…' : 'Research all companies →'}
					</button>
				</div>
				{#if companies.length === 0}
					<p class="text-xs text-muted-foreground/70 italic py-2">
						No companies yet — click <span class="text-primary">Research all companies</span> to pull SEC/ProPublica/registry data.
					</p>
				{:else}
					<div class="space-y-2">
						{#each companies as co}
							<div class="rounded-lg border border-border bg-card/50 p-2.5">
								<div class="flex items-start justify-between gap-2">
									<div class="flex items-center gap-1.5 min-w-0">
										<Building2 size={13} class="text-muted-foreground shrink-0" />
										{#if co.entity_id}
											<a href="/entity/{co.entity_id}" class="text-sm font-medium text-foreground hover:text-primary truncate">{co.name}</a>
										{:else}
											<span class="text-sm font-medium text-foreground truncate">{co.name}</span>
										{/if}
										<span class="text-[9px] uppercase tracking-wider text-muted-foreground/70 shrink-0">{co.relationship?.replace(/_/g, ' ')}</span>
										{#if co.affiliation === 'authorized'}
											<span class="text-[9px] px-1 py-0.5 rounded bg-emerald-500/15 text-emerald-300 shrink-0" title={co.designation || 'FEC: authorized by / principal committee of this entity'}>authorized</span>
										{:else if co.affiliation === 'independent'}
											<span class="text-[9px] px-1 py-0.5 rounded bg-amber-500/10 text-amber-300/80 shrink-0" title={co.designation || 'FEC: independent committee — names this entity but is not authorized by them'}>independent · names them</span>
										{/if}
									</div>
									{#each Object.entries(co.identifiers || {}) as [k, v]}
										<span class="text-[9px] font-mono px-1 py-0.5 rounded bg-sky-500/15 text-sky-300 shrink-0">{k}={v}</span>
									{/each}
								</div>
								{#if co.address}
									<div class="text-[11px] text-muted-foreground mt-1 flex items-center gap-1">
										<MapPin size={10} class="text-rose-400/70 shrink-0" />
										{#if co.address.lat != null}
											<a href={`/map?lat=${co.address.lat}&lng=${co.address.lon}&zoom=13`} class="hover:text-primary hover:underline">{co.address.raw || [co.address.city, co.address.region].filter(Boolean).join(', ')}</a>
										{:else}
											<span>{co.address.raw || [co.address.city, co.address.region].filter(Boolean).join(', ')}</span>
										{/if}
										<span class="text-muted-foreground/50">· {co.address.source}</span>
									</div>
								{/if}
								{#if (co.officers || []).length}
									<div class="text-[10px] text-muted-foreground mt-1">
										officers: {#each co.officers.slice(0, 6) as o, i}{#if o.entity_id}<a href="/entity/{o.entity_id}" class="text-primary hover:underline">{o.name}</a>{:else}{o.name}{/if}{#if i < Math.min(6, co.officers.length) - 1}, {/if}{/each}
									</div>
								{/if}
								{#if (co.filings || []).length}
									<div class="mt-1 space-y-0.5">
										{#each co.filings.slice(0, 5) as f}
											<div class="text-[10px] flex items-center gap-1">
												<span class="text-muted-foreground/60">{f.source}</span>
												{#if f.url}
													<a href={f.url} target="_blank" rel="noopener" class="text-primary hover:underline truncate">{f.title}</a>
												{:else}
													<span class="text-foreground/80 truncate">{f.title}</span>
												{/if}
											</div>
										{/each}
									</div>
								{/if}
							</div>
						{/each}
					</div>
				{/if}
			</Disclosure>
		{/if}

		<Disclosure id="investigations-section" scope={`entity-${entity_id}`}
			title="Investigations"
			count={investigations.length}
			subtitle={lastComplete ? `last completed ${lastComplete.completed_at?.slice(0,10) ?? ''}` : 'no completed runs'}>
			{#if investigations.length === 0}
				<p class="text-xs text-muted-foreground/70 italic py-2">
					No investigations yet. Click <span class="text-primary">Research</span> in the header to queue one.
				</p>
			{:else}
				<ul class="space-y-2 text-xs">
					{#each investigations as inv}
						<li class="flex flex-col gap-1 py-1">
							<div class="flex items-baseline gap-3">
								<span class="text-[10px] uppercase tracking-wider
									{inv.status === 'complete' ? 'text-emerald-400' :
									 inv.status === 'running' ? 'text-blue-400' :
									 inv.status === 'pending' ? 'text-amber-400' : 'text-muted-foreground'}">
									{inv.status}
								</span>
								<span class="text-foreground/90 flex-1">
									{inv.findings_count ?? 0} findings, {inv.entities_discovered ?? 0} entities discovered
								</span>
								<span class="text-[10px] text-muted-foreground/60">
									depth {inv.depth ?? 0}
								</span>
								{#if inv.completed_at}
									<span class="text-[10px] text-muted-foreground/60 tabular-nums">
										{inv.completed_at.slice(0, 10)}
									</span>
								{/if}
							</div>
							{#if inv.sources_requested && inv.sources_requested.length}
								<div class="flex flex-wrap gap-1 pl-2 mt-0.5">
									{#each inv.sources_requested as src}
										{@const completed = (inv.sources_completed || []).includes(src)}
										<span class={`text-[9px] uppercase tracking-wider px-1.5 py-px rounded
											${completed
												? 'bg-emerald-500/10 text-emerald-300/90 border border-emerald-500/30'
												: 'bg-muted text-muted-foreground/70 border border-border'}`}
											title={completed ? 'completed' : 'pending or skipped'}>
											{src}
										</span>
									{/each}
								</div>
							{/if}
						</li>
					{/each}
				</ul>
			{/if}
		</Disclosure>

		<!-- Federation provenance — cross-node trust story for this entity -->
		{#if federation}
			<Disclosure id="federation" scope={`entity-${entity_id}`}
				title="Federation provenance"
				count={federation.mention_total}
				subtitle={federation.trust_story}>
				<div class="space-y-4 pb-2">
					<!-- Identity strip -->
					<div class="grid grid-cols-2 gap-3 text-xs">
						{#if federation.canonical_entity_key}
							<div>
								<div class="text-[10px] uppercase tracking-wider text-muted-foreground">Canonical key</div>
								<div class="font-mono text-foreground" title={federation.canonical_entity_key}>
									{federation.canonical_entity_key.slice(0, 24)}…
								</div>
							</div>
						{/if}
						{#if federation.base60_address}
							<div>
								<div class="text-[10px] uppercase tracking-wider text-muted-foreground">GDU address</div>
								<div class="font-mono text-foreground">{federation.base60_address}</div>
							</div>
						{/if}
						{#if federation.pulled_from_node_id}
							<div class="col-span-2">
								<div class="text-[10px] uppercase tracking-wider text-muted-foreground">Origin</div>
								<div class="text-foreground">
									This entity row was created from a federation pull from
									<code class="font-mono">{federation.pulled_from_node_id}</code>
								</div>
							</div>
						{/if}
					</div>

					<!-- Mention source breakdown -->
					{#if federation.mention_sources.length > 0}
						<div>
							<div class="text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5">
								Mentions by source
							</div>
							<ul class="space-y-1 text-xs">
								{#each federation.mention_sources as src}
									<li class="flex items-baseline gap-2">
										<span class="tabular-nums w-12 text-right text-foreground">{src.count}</span>
										<span class="text-muted-foreground">×</span>
										{#if src.source === 'local'}
											<span class="text-emerald-300">local extraction</span>
										{:else}
											<span class="text-sky-300">pulled from</span>
											<code class="font-mono text-foreground">{src.source}</code>
										{/if}
									</li>
								{/each}
							</ul>
						</div>
					{/if}

					<!-- Envelopes that referenced this entity -->
					{#if federation.envelopes.length > 0}
						<div>
							<div class="text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5">
								Envelopes ({federation.envelope_count})
							</div>
							<ul class="space-y-1 text-xs">
								{#each federation.envelopes as env}
									<li class="flex items-baseline gap-2">
										<span class="tabular-nums w-12 text-right text-muted-foreground">{env.mentions_from_envelope}×</span>
										<code class="font-mono text-foreground" title={env.envelope_sha}>
											{env.envelope_sha.slice(0, 16)}…
										</code>
										<span class="text-muted-foreground">from</span>
										<code class="font-mono text-muted-foreground">{env.producer_node_id}</code>
									</li>
								{/each}
							</ul>
						</div>
					{/if}

					<!-- Notes about this entity -->
					{#if federation.notes.length > 0}
						<div>
							<div class="text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5">
								Analyst notes ({federation.note_count})
							</div>
							<ul class="space-y-2 text-xs">
								{#each federation.notes as n}
									<li class="rounded border border-border/50 bg-background/30 p-2">
										<div class="flex items-center gap-2 text-[10px] text-muted-foreground mb-1">
											<span class="text-foreground">{n.author_display_name || n.author_user_id || 'system'}</span>
											{#if n.library_id}
												·
												<a href={`/library/${n.library_id}`} class="hover:text-primary">library #{n.library_id}</a>
											{/if}
											{#if n.pulled_from_node_id}
												· <span>pulled from {n.pulled_from_node_id}</span>
											{/if}
										</div>
										<div class="text-foreground whitespace-pre-wrap">{n.body}</div>
									</li>
								{/each}
							</ul>
						</div>
					{/if}

					{#if federation.mention_total === 0 && federation.envelope_count === 0 && federation.note_count === 0}
						<p class="text-xs text-muted-foreground italic">
							No federation activity for this entity yet. As peers publish extractions
							referencing this entity (matched by canonical_entity_key), the
							evidence trail will appear here.
						</p>
					{/if}
				</div>
			</Disclosure>
		{/if}

	{/if}
</div>
