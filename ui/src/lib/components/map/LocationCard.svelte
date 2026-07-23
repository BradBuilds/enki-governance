<script lang="ts">
	/**
	 * Rich expandable location card — the core exploration component.
	 * Shows when a knowledge node marker is clicked on the map.
	 * Displays images, description, concepts, connected nodes, and actions.
	 */
	import { onMount } from 'svelte';
	import { api } from '$lib/api/mock';
	import X from 'lucide-svelte/icons/x';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import ImageIcon from 'lucide-svelte/icons/image';
	import Link from 'lucide-svelte/icons/link';
	import MessageCircle from 'lucide-svelte/icons/message-circle';
	import Pencil from 'lucide-svelte/icons/pencil';
	import ChevronDown from 'lucide-svelte/icons/chevron-down';
	import ChevronUp from 'lucide-svelte/icons/chevron-up';
	import ExternalLink from 'lucide-svelte/icons/external-link';

	interface Props {
		node: any;
		onClose: () => void;
		onNodeNavigate?: (node: any) => void;
	}

	let { node, onClose, onNodeNavigate = () => {} }: Props = $props();

	let expanded = $state(true);
	let connectedNodes = $state<any[]>([]);
	let lightboxImage = $state<string | null>(null);
	let showAddImage = $state(false);
	let newImageUrl = $state('');
	let newImageCaption = $state('');

	const props = $derived(node?.properties || node || {});
	const title = $derived(props.title || 'Unnamed Location');
	const epistemic = $derived(props.epistemic_type || 'observation');
	const description = $derived(props.description || '');
	const concepts = $derived(props.concepts || []);
	const images = $derived(props.images || []);
	const nodeId = $derived(props.node_id || '');
	const projectId = $derived(props.project_id || '');

	const epistemicColors: Record<string, string> = {
		fact: 'bg-enki-green/10 text-enki-green border-enki-green/30',
		observation: 'bg-primary/10 text-primary border-primary/30',
		claim: 'bg-severity-medium/10 text-severity-medium border-severity-medium/30',
		recommendation: 'bg-enki-purple/10 text-enki-purple border-enki-purple/30',
		opinion: 'bg-enki-cyan/10 text-enki-cyan border-enki-cyan/30',
		question: 'bg-enki-red/10 text-enki-red border-enki-red/30',
		definition: 'bg-enki-cyan/10 text-enki-cyan border-enki-cyan/30',
	};

	function timeAgo(ts: string): string {
		if (!ts) return '';
		const diff = Date.now() - new Date(ts).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 1) return 'just now';
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		const days = Math.floor(hrs / 24);
		return `${days}d ago`;
	}

	async function loadConnected() {
		if (!nodeId) return;
		try {
			const data = await api.signal(nodeId); // fallback — ideally /knowledge/nodes/{id}/graph
			// For now, connected nodes come from mock or API
		} catch { /* ignore */ }
	}

	async function addImage() {
		if (!newImageUrl || !nodeId) return;
		try {
			await api.addNodeImage(nodeId, { url: newImageUrl, caption: newImageCaption });
			// Optimistic update
			images.push({ url: newImageUrl, caption: newImageCaption });
			newImageUrl = '';
			newImageCaption = '';
			showAddImage = false;
		} catch { /* ignore */ }
	}
</script>

<div class="w-96 border-l border-border bg-card h-full overflow-y-auto flex flex-col">
	<!-- Header -->
	<div class="flex items-start justify-between px-4 py-3 border-b border-border shrink-0">
		<div class="flex-1 min-w-0">
			<div class="flex items-center gap-2 mb-1">
				<div class="w-5 h-5 rounded bg-primary/10 flex items-center justify-center shrink-0"
					 style="transform: rotate(45deg)">
					<span class="text-[8px] text-primary font-bold" style="transform: rotate(-45deg)">&#x25C6;</span>
				</div>
				<h3 class="text-sm font-medium text-foreground truncate">{title}</h3>
			</div>
			<div class="flex items-center gap-2">
				<span class="text-[10px] px-1.5 py-0.5 rounded border {epistemicColors[epistemic] || 'bg-muted text-muted-foreground border-border'}">
					{epistemic}
				</span>
				{#if props.source_authority && props.source_authority !== 'unverified'}
					<span class="text-[10px] text-muted-foreground">{props.source_authority}</span>
				{/if}
				{#if props.created_at}
					<span class="text-[10px] text-muted-foreground">{timeAgo(props.created_at)}</span>
				{/if}
			</div>
		</div>
		<button onclick={onClose} class="p-1 rounded hover:bg-muted transition-colors shrink-0 ml-2">
			<X class="w-4 h-4 text-muted-foreground" />
		</button>
	</div>

	<!-- Image Gallery -->
	{#if images.length > 0 || showAddImage}
		<div class="px-4 py-3 border-b border-border">
			{#if images.length > 0}
				<div class="flex gap-2 overflow-x-auto pb-1">
					{#each images as img}
						<button
							onclick={() => lightboxImage = img.url}
							class="shrink-0 rounded-lg overflow-hidden border border-border hover:border-primary/50 transition-colors"
						>
							<img src={img.url} alt={img.caption || ''} class="h-20 w-28 object-cover" loading="lazy" />
						</button>
					{/each}
					<button
						onclick={() => showAddImage = true}
						class="shrink-0 w-28 h-20 rounded-lg border border-dashed border-border flex items-center justify-center hover:border-primary/50 transition-colors"
					>
						<ImageIcon class="w-4 h-4 text-muted-foreground" />
					</button>
				</div>
				{#if images[0]?.caption}
					<p class="text-[10px] text-muted-foreground mt-1">{images[0].caption}</p>
				{/if}
			{/if}

			{#if showAddImage}
				<div class="mt-2 space-y-2">
					<input bind:value={newImageUrl} placeholder="Image URL..."
						class="w-full text-xs bg-muted border border-border rounded px-2 py-1.5 text-foreground placeholder:text-muted-foreground" />
					<input bind:value={newImageCaption} placeholder="Caption (optional)..."
						class="w-full text-xs bg-muted border border-border rounded px-2 py-1.5 text-foreground placeholder:text-muted-foreground" />
					<div class="flex gap-2">
						<button onclick={addImage} class="text-[10px] px-2 py-1 rounded bg-primary/10 text-primary hover:bg-primary/20">Save</button>
						<button onclick={() => showAddImage = false} class="text-[10px] px-2 py-1 rounded text-muted-foreground hover:bg-muted">Cancel</button>
					</div>
				</div>
			{/if}
		</div>
	{/if}

	<!-- Description -->
	{#if description}
		<div class="px-4 py-3 border-b border-border">
			<p class="text-xs text-foreground/90 leading-relaxed whitespace-pre-wrap">{description}</p>
		</div>
	{/if}

	<!-- Concepts -->
	{#if concepts.length > 0}
		<div class="px-4 py-3 border-b border-border">
			<span class="text-[10px] uppercase tracking-wide text-muted-foreground block mb-1.5">Concepts</span>
			<div class="flex flex-wrap gap-1">
				{#each concepts as concept}
					<span class="text-[10px] px-2 py-0.5 rounded-full bg-primary/10 text-primary border border-primary/20">
						{concept}
					</span>
				{/each}
			</div>
		</div>
	{/if}

	<!-- Connected Nodes -->
	{#if connectedNodes.length > 0}
		<div class="px-4 py-3 border-b border-border">
			<span class="text-[10px] uppercase tracking-wide text-muted-foreground block mb-1.5">Connected</span>
			<div class="space-y-1">
				{#each connectedNodes as cn}
					<button
						onclick={() => onNodeNavigate(cn)}
						class="w-full flex items-center gap-2 px-2 py-1.5 rounded hover:bg-muted transition-colors text-left"
					>
						<div class="w-3 h-3 rounded-sm bg-primary/20" style="transform: rotate(45deg)"></div>
						<span class="text-xs text-foreground truncate flex-1">{cn.title}</span>
						<span class="text-[10px] text-muted-foreground">{cn.epistemic_type}</span>
					</button>
				{/each}
			</div>
		</div>
	{/if}

	<!-- Location + Metadata -->
	<div class="px-4 py-3 border-b border-border">
		{#if props.latitude != null && props.longitude != null}
			<div class="flex items-center gap-1.5 text-[10px] text-muted-foreground mb-1">
				<MapPin class="w-3 h-3" />
				<span class="font-mono">{Number(props.latitude).toFixed(6)}, {Number(props.longitude).toFixed(6)}</span>
			</div>
		{/if}
		{#if projectId}
			<div class="flex items-center gap-1.5 text-[10px] text-muted-foreground">
				<Link class="w-3 h-3" />
				<span>Project: {projectId}</span>
			</div>
		{/if}
	</div>

	<!-- Actions -->
	<div class="px-4 py-3 flex items-center gap-3 shrink-0">
		<button class="flex items-center gap-1.5 text-[10px] text-muted-foreground hover:text-primary transition-colors">
			<Pencil class="w-3 h-3" /> Edit
		</button>
		<button
			onclick={() => showAddImage = true}
			class="flex items-center gap-1.5 text-[10px] text-muted-foreground hover:text-primary transition-colors"
		>
			<ImageIcon class="w-3 h-3" /> Add Image
		</button>
		<button class="flex items-center gap-1.5 text-[10px] text-muted-foreground hover:text-primary transition-colors">
			<MessageCircle class="w-3 h-3" /> Ask Enki
		</button>
	</div>
</div>

<!-- Lightbox -->
{#if lightboxImage}
	<button
		onclick={() => lightboxImage = null}
		class="fixed inset-0 z-[9999] bg-black/90 flex items-center justify-center cursor-zoom-out"
	>
		<img src={lightboxImage} alt="" class="max-h-[90vh] max-w-[90vw] object-contain rounded-lg" />
	</button>
{/if}
