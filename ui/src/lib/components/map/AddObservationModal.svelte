<script lang="ts">
	/**
	 * Modal for creating a knowledge node at a map location.
	 * Triggered by right-click on the map in Explore mode.
	 */
	import { api } from '$lib/api/mock';
	import X from 'lucide-svelte/icons/x';
	import MapPin from 'lucide-svelte/icons/map-pin';

	interface Props {
		latlng: { lat: number; lng: number };
		projects: any[];
		onCreated: (node: any) => void;
		onCancel: () => void;
	}

	let { latlng, projects, onCreated, onCancel }: Props = $props();

	let title = $state('');
	let description = $state('');
	let epistemicType = $state('observation');
	let projectId = $state(projects[0]?.project_id || '');
	let conceptsText = $state('');
	let saving = $state(false);
	let error = $state('');

	const epistemicTypes = [
		{ value: 'observation', label: 'Observation' },
		{ value: 'fact', label: 'Fact' },
		{ value: 'claim', label: 'Claim' },
		{ value: 'question', label: 'Question' },
		{ value: 'recommendation', label: 'Recommendation' },
		{ value: 'opinion', label: 'Opinion' },
		{ value: 'definition', label: 'Definition' },
	];

	async function save() {
		if (!title.trim()) {
			error = 'Title is required';
			return;
		}
		if (!projectId) {
			error = 'Select a project';
			return;
		}

		saving = true;
		error = '';

		const concepts = conceptsText
			.split(',')
			.map(c => c.trim().toLowerCase())
			.filter(c => c.length > 0);

		try {
			const node = await api.createGeoNode({
				project_id: projectId,
				title: title.trim(),
				latitude: latlng.lat,
				longitude: latlng.lng,
				description: description.trim() || undefined,
				epistemic_type: epistemicType,
				concepts: concepts.length > 0 ? concepts : undefined,
			});
			onCreated(node);
		} catch (e: any) {
			error = e?.message || 'Failed to save';
			saving = false;
		}
	}
</script>

<!-- Backdrop -->
<button onclick={onCancel} class="fixed inset-0 z-[2000] bg-black/30"></button>

<!-- Modal -->
<div class="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 z-[2001]
			w-96 bg-card border border-border rounded-xl shadow-2xl overflow-hidden">

	<!-- Header -->
	<div class="flex items-center justify-between px-4 py-3 border-b border-border">
		<div class="flex items-center gap-2">
			<MapPin class="w-4 h-4 text-primary" />
			<h3 class="text-sm font-medium text-foreground">New Observation</h3>
		</div>
		<button onclick={onCancel} class="p-1 rounded hover:bg-muted transition-colors">
			<X class="w-4 h-4 text-muted-foreground" />
		</button>
	</div>

	<!-- Form -->
	<div class="px-4 py-3 space-y-3">
		<!-- Title -->
		<div>
			<label class="text-[10px] uppercase tracking-wide text-muted-foreground block mb-1">Title *</label>
			<input bind:value={title} placeholder="What did you observe here?"
				class="w-full text-sm bg-muted border border-border rounded-lg px-3 py-2 text-foreground placeholder:text-muted-foreground focus:border-primary/50 outline-none" />
		</div>

		<!-- Type + Project row -->
		<div class="grid grid-cols-2 gap-2">
			<div>
				<label class="text-[10px] uppercase tracking-wide text-muted-foreground block mb-1">Type</label>
				<select bind:value={epistemicType}
					class="w-full text-xs bg-muted border border-border rounded-lg px-2 py-2 text-foreground">
					{#each epistemicTypes as t}
						<option value={t.value}>{t.label}</option>
					{/each}
				</select>
			</div>
			<div>
				<label class="text-[10px] uppercase tracking-wide text-muted-foreground block mb-1">Project *</label>
				<select bind:value={projectId}
					class="w-full text-xs bg-muted border border-border rounded-lg px-2 py-2 text-foreground">
					{#if projects.length === 0}
						<option value="">No projects</option>
					{/if}
					{#each projects as p}
						<option value={p.project_id}>{p.name}</option>
					{/each}
				</select>
			</div>
		</div>

		<!-- Description -->
		<div>
			<label class="text-[10px] uppercase tracking-wide text-muted-foreground block mb-1">Notes</label>
			<textarea bind:value={description} placeholder="Describe what you found..."
				rows="3"
				class="w-full text-sm bg-muted border border-border rounded-lg px-3 py-2 text-foreground placeholder:text-muted-foreground focus:border-primary/50 outline-none resize-none"></textarea>
		</div>

		<!-- Tags -->
		<div>
			<label class="text-[10px] uppercase tracking-wide text-muted-foreground block mb-1">Tags (comma-separated)</label>
			<input bind:value={conceptsText} placeholder="neolithic, megalith, pillar"
				class="w-full text-sm bg-muted border border-border rounded-lg px-3 py-2 text-foreground placeholder:text-muted-foreground focus:border-primary/50 outline-none" />
		</div>

		<!-- Location display -->
		<div class="flex items-center gap-1.5 text-[10px] text-muted-foreground font-mono">
			<MapPin class="w-3 h-3" />
			{latlng.lat.toFixed(6)}, {latlng.lng.toFixed(6)}
		</div>

		<!-- Error -->
		{#if error}
			<p class="text-[10px] text-enki-red">{error}</p>
		{/if}
	</div>

	<!-- Actions -->
	<div class="flex items-center justify-end gap-2 px-4 py-3 border-t border-border">
		<button onclick={onCancel}
			class="px-3 py-1.5 rounded-lg text-xs text-muted-foreground hover:bg-muted transition-colors">
			Cancel
		</button>
		<button onclick={save} disabled={saving}
			class="px-4 py-1.5 rounded-lg text-xs bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50">
			{saving ? 'Saving...' : 'Save Pin'}
		</button>
	</div>
</div>
