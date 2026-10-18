<script lang="ts">
	import { goto } from '$app/navigation';
	import { chat } from '$lib/stores/chat.svelte';
	import { screen } from '$lib/stores/screen.svelte';
	import { accessLog } from '$lib/stores/accessLog.svelte';
	import { space } from '$lib/stores/space.svelte';

	interface Props {
		name: string;
		entityType?: string;
		nodeId?: string;
		variant?: 'inline' | 'pill';
	}

	let { name, entityType = 'entity', nodeId, variant = 'pill' }: Props = $props();

	let showMenu = $state(false);
	let menuX = $state(0);
	let menuY = $state(0);

	const typeColors: Record<string, string> = {
		person: 'bg-violet-500/10 text-violet-400 border-violet-500/20',
		organization: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
		country: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
		location: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
		place: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
		vehicle: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20',
		vessel: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20',
		aircraft: 'bg-sky-500/10 text-sky-400 border-sky-500/20',
		entity: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
	};

	const colorClass = $derived(typeColors[entityType] || typeColors.entity);

	function handleClick() {
		accessLog.log({
			action: 'open_entity',
			resource_type: entityType,
			resource_id: nodeId || name,
			context: 'entity_chip',
			space: space.activeSpace,
		});
		if (nodeId) {
			goto(`/nodes/${encodeURIComponent(nodeId)}`);
		} else {
			chat.open();
			chat.sendMessage(`Tell me about ${name}`);
		}
	}

	function handleContextMenu(e: MouseEvent) {
		e.preventDefault();
		menuX = e.clientX;
		menuY = e.clientY;
		showMenu = true;
	}

	let pressTimer: ReturnType<typeof setTimeout> | null = null;

	function handleTouchStart(e: TouchEvent) {
		pressTimer = setTimeout(() => {
			const touch = e.touches[0];
			menuX = touch.clientX;
			menuY = touch.clientY;
			showMenu = true;
		}, 500);
	}

	function handleTouchEnd() {
		if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
	}

	function askEnki() {
		showMenu = false;
		chat.open();
		chat.sendMessage(`Tell me about ${name}`);
	}

	function openOnMap() {
		showMenu = false;
		goto(`/map?search=${encodeURIComponent(name)}`);
	}

	function openInResearch() {
		showMenu = false;
		if (nodeId) goto(`/nodes/${encodeURIComponent(nodeId)}`);
		else goto(`/search?type=name&value=${encodeURIComponent(name)}`);
	}

	function investigate() {
		showMenu = false;
		chat.open();
		chat.sendMessage(`Investigate ${name}`);
	}
</script>

<button
	onclick={handleClick}
	oncontextmenu={handleContextMenu}
	ontouchstart={handleTouchStart}
	ontouchend={handleTouchEnd}
	ontouchcancel={handleTouchEnd}
	class="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full border cursor-pointer transition-colors hover:brightness-110 {colorClass}"
>
	{name}
</button>

{#if showMenu}
	<button class="fixed inset-0 z-[90]" onclick={() => showMenu = false} aria-label="Close menu"></button>
	{#if screen.isMobile}
		<!-- Bottom sheet on mobile -->
		<div class="fixed bottom-0 left-0 right-0 z-[100] bg-card border-t border-border rounded-t-xl shadow-xl p-4 safe-bottom animate-in slide-in-from-bottom">
			<div class="w-8 h-1 bg-muted-foreground/30 rounded-full mx-auto mb-3"></div>
			<p class="text-sm font-medium text-foreground mb-3">{name}</p>
			<div class="space-y-1">
				<button onclick={openInResearch} class="w-full text-left px-3 py-2.5 text-sm rounded-lg hover:bg-muted transition-colors text-foreground">Open in Research</button>
				<button onclick={openOnMap} class="w-full text-left px-3 py-2.5 text-sm rounded-lg hover:bg-muted transition-colors text-foreground">View on Map</button>
				<button onclick={investigate} class="w-full text-left px-3 py-2.5 text-sm rounded-lg hover:bg-muted transition-colors text-foreground">Investigate (Public Records)</button>
				<button onclick={askEnki} class="w-full text-left px-3 py-2.5 text-sm rounded-lg hover:bg-muted transition-colors text-primary">Ask Enki</button>
			</div>
		</div>
	{:else}
		<!-- Dropdown on desktop -->
		<div
			class="fixed z-[100] w-44 rounded-lg border border-border bg-card shadow-lg py-1"
			style="left: {menuX}px; top: {menuY}px;"
		>
			<button onclick={openInResearch} class="w-full text-left px-3 py-2 text-xs hover:bg-muted transition-colors text-foreground">Open in Research</button>
			<button onclick={openOnMap} class="w-full text-left px-3 py-2 text-xs hover:bg-muted transition-colors text-foreground">View on Map</button>
			<button onclick={investigate} class="w-full text-left px-3 py-2 text-xs hover:bg-muted transition-colors text-foreground">Investigate</button>
			<button onclick={askEnki} class="w-full text-left px-3 py-2 text-xs hover:bg-muted transition-colors text-primary">Ask Enki</button>
		</div>
	{/if}
{/if}

<style>
	.safe-bottom {
		padding-bottom: env(safe-area-inset-bottom, 0px);
	}
</style>
