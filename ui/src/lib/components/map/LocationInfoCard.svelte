<script lang="ts">
	import { onMount } from 'svelte';
	import { api } from '$lib/api/mock';
	import { screen } from '$lib/stores/screen.svelte';
	import { swipeDismiss } from '$lib/actions/swipeDismiss';
	import Badge from '$lib/components/shared/Badge.svelte';
	import X from 'lucide-svelte/icons/x';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import FolderPlus from 'lucide-svelte/icons/folder-plus';
	import Paperclip from 'lucide-svelte/icons/paperclip';
	import Image from 'lucide-svelte/icons/image';
	import FileText from 'lucide-svelte/icons/file-text';
	import Link2 from 'lucide-svelte/icons/link-2';
	import Radio from 'lucide-svelte/icons/radio';
	import Briefcase from 'lucide-svelte/icons/briefcase';
	import MessageCircle from 'lucide-svelte/icons/message-circle';
	import Plus from 'lucide-svelte/icons/plus';
	import Navigation from 'lucide-svelte/icons/navigation';
	import ExternalLink from 'lucide-svelte/icons/external-link';

	interface Props {
		node: any;
		onClose: () => void;
		onFlyTo?: (lat: number, lon: number) => void;
	}

	let { node, onClose, onFlyTo }: Props = $props();

	const props = $derived(node?.properties || node || {});
	const nodeId = $derived(props.node_id || '');
	const lat = $derived(props.latitude);
	const lon = $derived(props.longitude);

	// Loaded data
	let attachments = $state<any[]>([]);
	let linkedCases = $state<any[]>([]);
	let nearbySignals = $state<any[]>([]);
	let loadingNearby = $state(false);

	// UI state
	let activeSection = $state<'info' | 'media' | 'signals' | 'cases'>('info');
	let showAttachModal = $state(false);
	let showCaseLinkModal = $state(false);
	let availableCases = $state<any[]>([]);
	let attachForm = $state({ type: 'link', url: '', name: '', caption: '', source: 'manual' });

	const epistemicColors: Record<string, string> = {
		fact: 'success', observation: 'info', claim: 'medium',
		hypothesis: 'high', question: 'critical', evidence: 'low',
	};

	onMount(async () => {
		if (!nodeId) return;
		// Load all related data in parallel
		const [attData, caseData] = await Promise.all([
			api.nodeAttachments(nodeId).catch(() => ({ attachments: [] })),
			api.nodeCases(nodeId).catch(() => ({ cases: [] })),
		]);
		attachments = attData?.attachments || [];
		linkedCases = caseData?.cases || [];

		// Load nearby signals
		if (lat != null && lon != null) {
			loadingNearby = true;
			try {
				const nearby = await api.nearbySignals(nodeId, 500);
				nearbySignals = nearby?.signals || [];
			} catch {}
			loadingNearby = false;
		}
	});

	async function addAttachment() {
		if (!attachForm.url && !attachForm.name) return;
		try {
			await api.addNodeAttachment(nodeId, attachForm);
			attachments = [...attachments, { ...attachForm, added_at: new Date().toISOString() }];
		} catch {}
		showAttachModal = false;
		attachForm = { type: 'link', url: '', name: '', caption: '', source: 'manual' };
	}

	async function openCaseLinkModal() {
		showCaseLinkModal = true;
		try {
			const d = await api.cases();
			availableCases = (d?.cases || []).filter((c: any) => c.status === 'open' || c.status === 'investigating');
		} catch {}
	}

	async function linkToCase(caseId: string) {
		try {
			await api.linkNodeToCase(nodeId, caseId);
			const c = availableCases.find(x => x.case_id === caseId);
			linkedCases = [...linkedCases, { case_id: caseId, title: c?.title || caseId, status: c?.status, linked_at: new Date().toISOString() }];
		} catch {}
		showCaseLinkModal = false;
	}

	function timeAgo(ts: string): string {
		if (!ts) return '';
		const diff = Date.now() - new Date(ts).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}

	const images = $derived(attachments.filter(a => a.type === 'image'));
	const documents = $derived(attachments.filter(a => a.type === 'document'));
	const links = $derived(attachments.filter(a => a.type === 'link'));
	const metadata = $derived(props.metadata || {});
</script>

{#if screen.isMobile}
	<div class="fixed bottom-0 left-0 right-0 z-50 bg-card border-t border-border rounded-t-2xl shadow-2xl safe-bottom max-h-[75vh] overflow-y-auto"
		use:swipeDismiss={{ onDismiss: onClose }}>
		<div class="flex justify-center pt-2 pb-1"><div class="w-8 h-1 rounded-full bg-muted-foreground/30"></div></div>
		<div class="px-4 pb-4">{@render cardContent()}</div>
	</div>
{:else}
	<div class="w-[360px] bg-card/95 backdrop-blur-sm border-l border-border shadow-xl overflow-y-auto shrink-0 flex flex-col">
		<div class="flex items-center justify-between px-4 py-3 border-b border-border sticky top-0 bg-card/95 z-10">
			<span class="text-sm font-medium text-foreground truncate">{props.title || 'Location'}</span>
			<button onclick={onClose} class="p-1 rounded hover:bg-muted text-muted-foreground"><X size={16} /></button>
		</div>
		<div class="flex-1 overflow-y-auto">{@render cardContent()}</div>
	</div>
{/if}

{#snippet cardContent()}
	<!-- Header -->
	<div class="px-4 py-3">
		<div class="flex items-start gap-3">
			<div class="w-10 h-10 rounded-lg bg-emerald-500/10 flex items-center justify-center shrink-0">
				<MapPin size={20} class="text-emerald-400" />
			</div>
			<div class="flex-1 min-w-0">
				<h3 class="text-sm font-medium text-foreground">{props.title || 'Location'}</h3>
				{#if props.epistemic_type}
					<Badge text={props.epistemic_type} variant={epistemicColors[props.epistemic_type] || 'neutral'} size="sm" />
				{/if}
				{#if lat != null && lon != null}
					<button onclick={() => onFlyTo?.(lat, lon)}
						class="flex items-center gap-1 mt-1 text-[10px] text-primary hover:underline font-mono">
						<Navigation size={9} />{lat.toFixed(4)}, {lon.toFixed(4)}
					</button>
				{/if}
			</div>
		</div>
		{#if props.description}
			<p class="text-xs text-muted-foreground mt-2">{props.description}</p>
		{/if}
		{#if props.concepts?.length}
			<div class="flex flex-wrap gap-1 mt-2">
				{#each props.concepts as tag}
					<span class="text-[9px] px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground">{tag}</span>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Section tabs -->
	<div class="flex border-b border-border px-4">
		{#each [['info', 'Info'], ['media', `Media (${attachments.length})`], ['signals', `Nearby (${nearbySignals.length})`], ['cases', `Cases (${linkedCases.length})`]] as [key, label]}
			<button
				onclick={() => activeSection = key as typeof activeSection}
				class="px-3 py-2 text-[10px] font-medium border-b-2 -mb-px transition-colors
					{activeSection === key ? 'text-primary border-primary' : 'text-muted-foreground border-transparent hover:text-foreground'}"
			>{label}</button>
		{/each}
	</div>

	<div class="px-4 py-3">
		{#if activeSection === 'info'}
			<!-- Property / metadata info -->
			{#if metadata.property}
				<div class="mb-3">
					<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-1.5">Property Data</p>
					<div class="space-y-1">
						{#each Object.entries(metadata.property) as [k, v]}
							<div class="flex justify-between text-xs">
								<span class="text-muted-foreground capitalize">{k.replace(/_/g, ' ')}</span>
								<span class="text-foreground">{v}</span>
							</div>
						{/each}
					</div>
				</div>
			{/if}

			{#if metadata.address}
				<div class="mb-3">
					<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-1">Address</p>
					<p class="text-xs text-foreground">{metadata.address}</p>
				</div>
			{/if}

			{#if metadata.enrichment}
				<div class="mb-3">
					<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-1.5">Enrichment</p>
					<div class="space-y-1">
						{#each Object.entries(metadata.enrichment) as [k, v]}
							<div class="flex justify-between text-xs">
								<span class="text-muted-foreground capitalize">{k.replace(/_/g, ' ')}</span>
								<span class="text-foreground">{typeof v === 'object' ? JSON.stringify(v) : v}</span>
							</div>
						{/each}
					</div>
				</div>
			{/if}

			{#if !metadata.property && !metadata.address && !metadata.enrichment}
				<p class="text-xs text-muted-foreground py-4 text-center">No additional information yet. Add data via attachments or auto-enrichment.</p>
			{/if}

		{:else if activeSection === 'media'}
			<!-- Attachments -->
			{#if images.length > 0}
				<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-1.5">Photos</p>
				<div class="grid grid-cols-3 gap-1.5 mb-3">
					{#each images as img}
						<div class="aspect-square rounded-lg bg-muted overflow-hidden">
							{#if img.url}
								<img src={img.url} alt={img.caption || img.name} class="w-full h-full object-cover" />
							{:else}
								<div class="w-full h-full flex items-center justify-center"><Image size={20} class="text-muted-foreground/30" /></div>
							{/if}
						</div>
					{/each}
				</div>
			{/if}

			{#if documents.length > 0}
				<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-1.5">Documents</p>
				<div class="space-y-1 mb-3">
					{#each documents as doc}
						<a href={doc.url} target="_blank" rel="noopener" class="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-muted text-xs text-foreground">
							<FileText size={14} class="text-muted-foreground shrink-0" />
							<span class="truncate">{doc.name || doc.url}</span>
							<ExternalLink size={10} class="text-muted-foreground shrink-0 ml-auto" />
						</a>
					{/each}
				</div>
			{/if}

			{#if links.length > 0}
				<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-1.5">Links</p>
				<div class="space-y-1 mb-3">
					{#each links as link}
						<a href={link.url} target="_blank" rel="noopener" class="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-muted text-xs text-foreground">
							<Link2 size={14} class="text-primary shrink-0" />
							<span class="truncate">{link.name || link.url}</span>
							<ExternalLink size={10} class="text-muted-foreground shrink-0 ml-auto" />
						</a>
					{/each}
				</div>
			{/if}

			<button onclick={() => showAttachModal = true}
				class="flex items-center gap-2 w-full px-3 py-2 rounded-lg border border-dashed border-border text-xs text-muted-foreground hover:text-foreground hover:border-primary/50 transition-colors">
				<Paperclip size={14} /> Add Attachment
			</button>

		{:else if activeSection === 'signals'}
			<!-- Nearby signals -->
			{#if loadingNearby}
				<p class="text-xs text-muted-foreground animate-pulse py-4 text-center">Searching nearby...</p>
			{:else if nearbySignals.length > 0}
				<div class="space-y-1.5">
					{#each nearbySignals as sig}
						<div class="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-muted">
							<Radio size={12} class="text-muted-foreground shrink-0" />
							<div class="flex-1 min-w-0">
								<p class="text-[11px] text-foreground truncate">{sig.signal_type?.split('.').pop()?.replace(/_/g, ' ')}</p>
								<p class="text-[9px] text-muted-foreground">{sig.device_id} · {Math.round(sig.distance_m)}m away</p>
							</div>
							<span class="text-[9px] text-muted-foreground">{timeAgo(sig.timestamp)}</span>
						</div>
					{/each}
				</div>
			{:else}
				<p class="text-xs text-muted-foreground py-4 text-center">No signals within 500m</p>
			{/if}

		{:else if activeSection === 'cases'}
			<!-- Linked cases -->
			{#if linkedCases.length > 0}
				<div class="space-y-1.5 mb-3">
					{#each linkedCases as c}
						<a href="/cases/{c.case_id}" class="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-muted">
							<Briefcase size={12} class="text-primary shrink-0" />
							<div class="flex-1 min-w-0">
								<p class="text-[11px] text-foreground truncate">{c.title}</p>
								<p class="text-[9px] text-muted-foreground">{c.status}</p>
							</div>
						</a>
					{/each}
				</div>
			{/if}
			<button onclick={openCaseLinkModal}
				class="flex items-center gap-2 w-full px-3 py-2 rounded-lg border border-dashed border-border text-xs text-muted-foreground hover:text-foreground hover:border-primary/50 transition-colors">
				<FolderPlus size={14} /> Link to Case
			</button>
		{/if}
	</div>

	<!-- Actions bar -->
	<div class="px-4 py-3 border-t border-border mt-auto">
		<a href="/chat?q=what do we know about {props.title || 'this location'}"
			class="flex items-center justify-center gap-2 w-full px-3 py-2 rounded-lg border border-border text-xs text-muted-foreground hover:text-foreground hover:bg-muted transition-colors">
			<MessageCircle size={14} /> Ask Enki about this location
		</a>
	</div>
{/snippet}

<!-- Attachment Modal -->
{#if showAttachModal}
	<div class="fixed inset-0 z-[2000] flex items-center justify-center p-4">
		<button class="absolute inset-0 bg-black/50" onclick={() => showAttachModal = false} aria-label="Close"></button>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="relative bg-card border border-border rounded-xl p-4 w-full max-w-sm shadow-xl" onclick={(e) => e.stopPropagation()}>
			<h3 class="text-sm font-medium text-foreground mb-3">Add Attachment</h3>
			<div class="space-y-3">
				<div class="flex gap-1">
					{#each ['link', 'image', 'document'] as t}
						<button onclick={() => attachForm.type = t}
							class="flex-1 px-2 py-1.5 rounded-lg text-xs transition-colors
								{attachForm.type === t ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}"
						>{t}</button>
					{/each}
				</div>
				<input type="text" bind:value={attachForm.name} placeholder="Name / title"
					class="w-full px-3 py-2 rounded-lg border border-border bg-card text-foreground text-sm" />
				<input type="text" bind:value={attachForm.url} placeholder="URL"
					class="w-full px-3 py-2 rounded-lg border border-border bg-card text-foreground text-sm" />
				<input type="text" bind:value={attachForm.caption} placeholder="Caption / notes (optional)"
					class="w-full px-3 py-2 rounded-lg border border-border bg-card text-foreground text-sm" />
				<select bind:value={attachForm.source} class="w-full px-3 py-2 rounded-lg border border-border bg-card text-foreground text-sm">
					<option value="manual">Manual</option>
					<option value="osint">OSINT</option>
					<option value="public_records">Public Records</option>
					<option value="field_collection">Field Collection</option>
				</select>
			</div>
			<div class="flex gap-2 mt-4">
				<button onclick={() => showAttachModal = false} class="flex-1 px-3 py-2 rounded-lg border border-border text-xs hover:bg-muted">Cancel</button>
				<button onclick={addAttachment} class="flex-1 px-3 py-2 rounded-lg bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90">Add</button>
			</div>
		</div>
	</div>
{/if}

<!-- Case Link Modal -->
{#if showCaseLinkModal}
	<div class="fixed inset-0 z-[2000] flex items-center justify-center p-4">
		<button class="absolute inset-0 bg-black/50" onclick={() => showCaseLinkModal = false} aria-label="Close"></button>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="relative bg-card border border-border rounded-xl p-4 w-full max-w-sm shadow-xl" onclick={(e) => e.stopPropagation()}>
			<h3 class="text-sm font-medium text-foreground mb-3">Link to Case</h3>
			{#if availableCases.length > 0}
				<div class="space-y-1 max-h-60 overflow-y-auto">
					{#each availableCases as c}
						<button onclick={() => linkToCase(c.case_id)}
							class="flex items-center gap-2 w-full px-3 py-2 rounded-lg text-left hover:bg-muted transition-colors">
							<Briefcase size={14} class="text-primary shrink-0" />
							<div class="flex-1 min-w-0">
								<p class="text-xs text-foreground truncate">{c.title}</p>
								<p class="text-[9px] text-muted-foreground">{c.status}</p>
							</div>
						</button>
					{/each}
				</div>
			{:else}
				<p class="text-xs text-muted-foreground py-4 text-center">No open cases</p>
			{/if}
			<button onclick={() => showCaseLinkModal = false} class="w-full mt-3 px-3 py-2 rounded-lg border border-border text-xs hover:bg-muted">Cancel</button>
		</div>
	</div>
{/if}

<style>
	.safe-bottom { padding-bottom: env(safe-area-inset-bottom, 0px); }
</style>
