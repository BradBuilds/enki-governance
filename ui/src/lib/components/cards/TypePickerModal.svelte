<script lang="ts">
	/**
	 * TypePickerModal — the single primitive for manual classification
	 * overrides (Phase 5C). Summoned from node detail pages, chips,
	 * cards, graph nodes, watch buckets, and map markers. Three actions:
	 *
	 *   Apply      — change canonical_type to the selected type, lock it
	 *   Reject     — mark as not-a-real-entity, lock it
	 *   Confirm    — approve current type as human-validated, lock it
	 *
	 * The backend endpoint writes to kb_user_contributions so the choice
	 * propagates to future candidates with the same normalized_name, and
	 * to manual_override_log for the audit trail.
	 */
	import { goto, invalidateAll } from '$app/navigation';
	import X from 'lucide-svelte/icons/x';
	import Search from 'lucide-svelte/icons/search';
	import Check from 'lucide-svelte/icons/check';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';

	interface Props {
		open: boolean;
		nodeId: string;
		currentType?: string | null;
		currentName?: string | null;
		currentStatus?: string | null;
		currentSource?: string | null;
		onClose: () => void;
		/** Optional hook that fires after a successful action so the
		 * calling page can refresh its data. If null, we reload the route
		 * via SvelteKit's invalidateAll(). */
		onApplied?: (result: { action: string; new_type?: string }) => void;
	}

	let {
		open, nodeId, currentType = null, currentName = null,
		currentStatus = null, currentSource = null,
		onClose, onApplied = undefined,
	}: Props = $props();

	// Grouped taxonomy — matches the backend's ALLOWED_TYPES. Icon + label
	// for each type so the picker is scannable.
	type TypeOption = { id: string; label: string; icon: string };
	const TYPE_GROUPS: { group: string; types: TypeOption[] }[] = [
		{ group: 'People & Organizations', types: [
			{ id: 'person',             label: 'Person',              icon: '👤' },
			{ id: 'organization',       label: 'Organization',        icon: '🏢' },
			{ id: 'military_unit',      label: 'Military Unit',       icon: '🎖' },
			{ id: 'government_agency',  label: 'Government Agency',   icon: '🏛' },
			{ id: 'university',         label: 'University',          icon: '🎓' },
			{ id: 'mass_media',         label: 'Mass Media',          icon: '📺' },
			{ id: 'research_institute', label: 'Research Institute',  icon: '🔬' },
		]},
		{ group: 'Places — Political', types: [
			{ id: 'country', label: 'Country', icon: '🌍' },
			{ id: 'city',    label: 'City',    icon: '🏙' },
			{ id: 'region',  label: 'Region',  icon: '📍' },
		]},
		{ group: 'Places — Natural & Infrastructure', types: [
			{ id: 'strait',         label: 'Strait',          icon: '🌊' },
			{ id: 'bay',            label: 'Bay',             icon: '🌊' },
			{ id: 'gulf',           label: 'Gulf',            icon: '🌊' },
			{ id: 'canal',          label: 'Canal',           icon: '🌊' },
			{ id: 'river',          label: 'River',           icon: '🏞' },
			{ id: 'lake',           label: 'Lake',            icon: '🏞' },
			{ id: 'sea',            label: 'Sea',             icon: '🌊' },
			{ id: 'ocean',          label: 'Ocean',           icon: '🌊' },
			{ id: 'peninsula',      label: 'Peninsula',       icon: '🏔' },
			{ id: 'island',         label: 'Island',          icon: '🏝' },
			{ id: 'island_group',   label: 'Island Group',    icon: '🏝' },
			{ id: 'archipelago',    label: 'Archipelago',     icon: '🏝' },
			{ id: 'mountain',       label: 'Mountain',        icon: '🏔' },
			{ id: 'mountain_range', label: 'Mountain Range',  icon: '🏔' },
			{ id: 'desert',         label: 'Desert',          icon: '🏜' },
			{ id: 'continent',      label: 'Continent',       icon: '🗺' },
			{ id: 'airport',        label: 'Airport',         icon: '✈' },
			{ id: 'seaport',        label: 'Seaport',         icon: '⚓' },
			{ id: 'railway_station',label: 'Railway Station', icon: '🚉' },
			{ id: 'pipeline',       label: 'Pipeline',        icon: '🛢' },
			{ id: 'bridge',         label: 'Bridge',          icon: '🌉' },
			{ id: 'dam',            label: 'Dam',             icon: '💧' },
			{ id: 'power_plant',    label: 'Power Plant',     icon: '⚡' },
			{ id: 'refinery',       label: 'Refinery',        icon: '🏭' },
		]},
		{ group: 'Equipment & Assets', types: [
			{ id: 'aircraft',                label: 'Aircraft',     icon: '✈' },
			{ id: 'helicopter',              label: 'Helicopter',   icon: '🚁' },
			{ id: 'airliner',                label: 'Airliner',     icon: '✈' },
			{ id: 'vessel',                  label: 'Vessel',       icon: '🚢' },
			{ id: 'vehicle',                 label: 'Vehicle',      icon: '🚗' },
			{ id: 'weapon',                  label: 'Weapon',       icon: '⚔' },
			{ id: 'spacecraft',              label: 'Spacecraft',   icon: '🛰' },
			{ id: 'unmanned_aerial_vehicle', label: 'Drone / UAV',  icon: '🛸' },
		]},
		{ group: 'Abstract', types: [
			{ id: 'event',      label: 'Event',      icon: '●' },
			{ id: 'topic',      label: 'Topic',      icon: '▪' },
			{ id: 'document',   label: 'Document',   icon: '📄' },
			{ id: 'chronicle',  label: 'Chronicle',  icon: '◇' },
			{ id: 'assessment', label: 'Assessment', icon: '◎' },
			{ id: 'pattern',    label: 'Pattern',    icon: '✦' },
			{ id: 'other',      label: 'Other',      icon: '·' },
		]},
	];

	let selectedType = $state<string>(currentType ?? '');
	let search = $state('');
	let note = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);

	$effect(() => {
		if (open) {
			selectedType = currentType ?? '';
			search = '';
			note = '';
			error = null;
		}
	});

	const filteredGroups = $derived(
		search.trim()
			? TYPE_GROUPS.map(g => ({
				group: g.group,
				types: g.types.filter(t =>
					t.id.includes(search.toLowerCase()) ||
					t.label.toLowerCase().includes(search.toLowerCase())
				)
			})).filter(g => g.types.length > 0)
			: TYPE_GROUPS
	);

	async function apply() {
		if (!selectedType) { error = 'Pick a type first'; return; }
		busy = true; error = null;
		try {
			const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/reclassify`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ new_type: selectedType, note }),
			});
			if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
			const data = await res.json();
			onApplied?.({ action: 'reclassify', new_type: selectedType });
			onClose();
			if (!onApplied) await invalidateAll();
		} catch (e: any) {
			error = e.message ?? String(e);
		} finally {
			busy = false;
		}
	}

	async function confirm() {
		busy = true; error = null;
		try {
			const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/confirm`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ note }),
			});
			if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
			onApplied?.({ action: 'confirm' });
			onClose();
			if (!onApplied) await invalidateAll();
		} catch (e: any) {
			error = e.message ?? String(e);
		} finally {
			busy = false;
		}
	}

	async function reject() {
		if (!confirmWindow('Mark this node as not a real entity? It will be hidden from the graph.')) return;
		busy = true; error = null;
		try {
			const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/reject`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ note }),
			});
			if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
			onApplied?.({ action: 'reject' });
			onClose();
			if (!onApplied) await invalidateAll();
		} catch (e: any) {
			error = e.message ?? String(e);
		} finally {
			busy = false;
		}
	}

	function confirmWindow(msg: string): boolean {
		if (typeof window === 'undefined') return true;
		return window.confirm(msg);
	}

	function onBackdropClick(e: MouseEvent) {
		if (e.target === e.currentTarget && !busy) onClose();
	}

	function onKeyDown(e: KeyboardEvent) {
		if (e.key === 'Escape' && !busy) onClose();
	}
</script>

<svelte:window onkeydown={onKeyDown} />

{#if open}
	<div
		class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"
		onclick={onBackdropClick}
		role="dialog"
		aria-modal="true"
		aria-labelledby="typepicker-title"
	>
		<div class="bg-card border rounded-lg shadow-xl max-w-2xl w-full max-h-[85vh] overflow-hidden flex flex-col">
			<!-- Header -->
			<div class="px-4 py-3 border-b flex items-start justify-between gap-3">
				<div class="min-w-0">
					<div class="text-xs text-muted-foreground mb-0.5" id="typepicker-title">Reclassify</div>
					<div class="text-base font-semibold truncate">{currentName ?? nodeId}</div>
					<div class="text-xs text-muted-foreground mt-1 flex flex-wrap gap-2">
						<span>
							current: <span class="font-mono">{currentType ?? '—'}</span>
						</span>
						{#if currentStatus}
							<span>· {currentStatus}</span>
						{/if}
						{#if currentSource}
							<span>· via {currentSource}</span>
						{/if}
					</div>
				</div>
				<button
					type="button"
					onclick={onClose}
					disabled={busy}
					class="p-1 text-muted-foreground hover:text-foreground"
					aria-label="Close"
				>
					<X class="h-4 w-4" />
				</button>
			</div>

			<!-- Search -->
			<div class="px-4 py-2 border-b">
				<div class="relative">
					<Search class="h-3.5 w-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
					<input
						type="text"
						bind:value={search}
						placeholder="Search types…"
						class="w-full pl-8 pr-2 py-1.5 text-sm bg-muted/30 border rounded focus:outline-none focus:border-foreground/30"
						disabled={busy}
					/>
				</div>
			</div>

			<!-- Type grid -->
			<div class="flex-1 overflow-y-auto px-4 py-3 space-y-4">
				{#each filteredGroups as group (group.group)}
					<div>
						<div class="text-[10px] uppercase tracking-wide text-muted-foreground mb-1.5">
							{group.group}
						</div>
						<div class="grid grid-cols-2 md:grid-cols-3 gap-1">
							{#each group.types as t (t.id)}
								<button
									type="button"
									onclick={() => selectedType = t.id}
									disabled={busy}
									class="text-left px-2 py-1.5 rounded border text-xs flex items-center gap-1.5
										{selectedType === t.id
											? 'bg-primary/10 border-primary text-foreground font-medium'
											: 'hover:bg-muted/50 border-border'}"
								>
									<span class="shrink-0">{t.icon}</span>
									<span class="truncate">{t.label}</span>
									{#if selectedType === t.id}
										<Check class="h-3 w-3 ml-auto shrink-0" />
									{/if}
								</button>
							{/each}
						</div>
					</div>
				{/each}
				{#if filteredGroups.length === 0}
					<div class="text-sm text-muted-foreground text-center py-8">
						No types match "{search}"
					</div>
				{/if}
			</div>

			<!-- Note -->
			<div class="px-4 py-3 border-t">
				<textarea
					bind:value={note}
					placeholder="Optional note for the audit log…"
					class="w-full px-2 py-1.5 text-xs bg-muted/30 border rounded focus:outline-none focus:border-foreground/30 resize-none"
					rows="2"
					disabled={busy}
				></textarea>
			</div>

			<!-- Error + Actions -->
			{#if error}
				<div class="px-4 py-2 text-xs text-red-400 border-t flex items-center gap-1.5">
					<AlertTriangle class="h-3.5 w-3.5" />
					{error}
				</div>
			{/if}
			<div class="px-4 py-3 border-t flex items-center gap-2">
				<button
					type="button"
					onclick={reject}
					disabled={busy}
					class="px-3 py-1.5 text-xs rounded border border-red-500/30 bg-red-500/5 text-red-400 hover:bg-red-500/10"
				>
					✗ Reject
				</button>
				<button
					type="button"
					onclick={confirm}
					disabled={busy || !currentType}
					title={currentType ? `Confirm current type: ${currentType}` : 'No current type to confirm'}
					class="px-3 py-1.5 text-xs rounded border border-muted text-foreground hover:bg-muted/50"
				>
					✓ Confirm current
				</button>
				<div class="flex-1"></div>
				<button
					type="button"
					onclick={onClose}
					disabled={busy}
					class="px-3 py-1.5 text-xs rounded text-muted-foreground hover:text-foreground"
				>
					Cancel
				</button>
				<button
					type="button"
					onclick={apply}
					disabled={busy || !selectedType || selectedType === currentType}
					class="px-3 py-1.5 text-xs rounded bg-primary text-primary-foreground disabled:opacity-50"
				>
					{busy ? 'Saving…' : 'Apply'}
				</button>
			</div>
		</div>
	</div>
{/if}
