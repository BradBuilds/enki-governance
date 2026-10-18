<script lang="ts">
	/**
	 * Full-screen search overlay for mobile.
	 * Triggered from the search icon in the TopBar.
	 * Same power as the desktop search bar — search signals, pages, and AI queries.
	 */
	import Search from 'lucide-svelte/icons/search';
	import X from 'lucide-svelte/icons/x';
	import ArrowLeft from 'lucide-svelte/icons/arrow-left';
	import Radio from 'lucide-svelte/icons/radio';
	import Bell from 'lucide-svelte/icons/bell';
	import Briefcase from 'lucide-svelte/icons/briefcase';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import { goto } from '$app/navigation';

	interface Props {
		onClose: () => void;
	}

	let { onClose }: Props = $props();
	let query = $state('');
	let inputEl: HTMLInputElement;

	// Quick nav shortcuts
	const shortcuts = [
		{ label: 'Signals', href: '/signals', icon: Radio },
		{ label: 'Alerts', href: '/alerts', icon: Bell },
		{ label: 'Cases', href: '/cases', icon: Briefcase },
		{ label: 'Map', href: '/map', icon: MapPin },
	];

	// Recent searches (could be persisted to localStorage later)
	let recentSearches = $state<string[]>([
		'high severity alerts',
		'GPS signals last hour',
		'open cases',
	]);

	function handleSubmit() {
		if (!query.trim()) return;
		// Navigate to search page with query
		onClose();
		goto(`/search?q=${encodeURIComponent(query.trim())}`);
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') {
			e.preventDefault();
			handleSubmit();
		}
		if (e.key === 'Escape') {
			onClose();
		}
	}

	function searchRecent(term: string) {
		onClose();
		goto(`/search?q=${encodeURIComponent(term)}`);
	}

	import { onMount } from 'svelte';
	onMount(() => {
		inputEl?.focus();
	});
</script>

<!-- Full-screen overlay -->
<div class="fixed inset-0 z-50 bg-background flex flex-col">
	<!-- Header with search input -->
	<div class="flex items-center gap-2 px-3 py-2 border-b border-border">
		<button onclick={onClose} class="p-2 rounded-md text-muted-foreground hover:text-foreground">
			<ArrowLeft class="w-5 h-5" />
		</button>
		<div class="flex-1 flex items-center gap-2 bg-muted rounded-lg px-3 py-2">
			<Search class="w-4 h-4 text-muted-foreground shrink-0" />
			<input
				bind:this={inputEl}
				type="text"
				bind:value={query}
				onkeydown={handleKeydown}
				placeholder="Search signals, alerts, cases..."
				class="flex-1 bg-transparent text-sm text-foreground placeholder:text-muted-foreground outline-none"
			/>
			{#if query}
				<button onclick={() => query = ''} class="p-0.5 rounded hover:bg-accent/50">
					<X class="w-3.5 h-3.5 text-muted-foreground" />
				</button>
			{/if}
		</div>
		{#if query.trim()}
			<button onclick={handleSubmit} class="text-sm font-medium text-primary">
				Go
			</button>
		{/if}
	</div>

	<!-- Content -->
	<div class="flex-1 overflow-y-auto px-4 py-4 space-y-6">
		<!-- Quick navigation -->
		<div>
			<span class="text-[10px] uppercase tracking-wide text-muted-foreground/60 block mb-2">Quick Nav</span>
			<div class="grid grid-cols-4 gap-2">
				{#each shortcuts as s}
					<a
						href={s.href}
						onclick={onClose}
						class="flex flex-col items-center gap-1.5 py-3 rounded-xl bg-card border border-border hover:border-primary/50 transition-colors"
					>
						<s.icon class="w-5 h-5 text-muted-foreground" />
						<span class="text-[10px] font-medium text-foreground">{s.label}</span>
					</a>
				{/each}
			</div>
		</div>

		<!-- Recent searches -->
		{#if recentSearches.length > 0 && !query}
			<div>
				<span class="text-[10px] uppercase tracking-wide text-muted-foreground/60 block mb-2">Recent</span>
				<div class="space-y-1">
					{#each recentSearches as term}
						<button
							onclick={() => searchRecent(term)}
							class="flex items-center gap-3 w-full px-3 py-2.5 rounded-lg text-sm text-foreground hover:bg-muted transition-colors text-left"
						>
							<Search class="w-4 h-4 text-muted-foreground shrink-0" />
							{term}
						</button>
					{/each}
				</div>
			</div>
		{/if}
	</div>
</div>
