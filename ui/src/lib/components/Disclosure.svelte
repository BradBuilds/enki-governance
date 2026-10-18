<script lang="ts">
	import { browser } from '$app/environment';
	import ChevronDown from 'lucide-svelte/icons/chevron-down';
	import type { Snippet } from 'svelte';

	interface Props {
		/** Stable id for persistence (combined with scope into a localStorage key) */
		id: string;
		/** Per-entity/page scope so different dossiers can have independent open-states */
		scope?: string;
		title: string;
		count?: number | string | null;
		/** Optional small tag rendered next to the title (e.g. "8 companies, 4 properties") */
		subtitle?: string;
		/** Default state when no preference is stored */
		defaultOpen?: boolean;
		children: Snippet;
	}

	let {
		id, scope = 'default', title, count = null, subtitle = '',
		defaultOpen = false, children,
	}: Props = $props();

	const storageKey = $derived(`disclosure:${scope}:${id}`);

	let open = $state<boolean>(defaultOpen);

	// Load persisted state once on mount
	$effect(() => {
		if (!browser) return;
		const stored = localStorage.getItem(storageKey);
		if (stored === '1') open = true;
		else if (stored === '0') open = false;
	});

	function toggle() {
		open = !open;
		if (browser) {
			localStorage.setItem(storageKey, open ? '1' : '0');
		}
	}
</script>

<section class="rounded-xl border border-border bg-card overflow-hidden">
	<button onclick={toggle}
		aria-expanded={open}
		class="w-full flex items-center justify-between gap-3 px-4 py-3
			hover:bg-muted/30 transition-colors text-left">
		<div class="flex items-baseline gap-2.5 min-w-0">
			<span class="text-sm font-semibold text-foreground">{title}</span>
			{#if count !== null && count !== undefined && count !== ''}
				<span class="text-xs text-muted-foreground tabular-nums">{count}</span>
			{/if}
			{#if subtitle}
				<span class="text-[11px] text-muted-foreground/70 truncate">{subtitle}</span>
			{/if}
		</div>
		<ChevronDown size={16}
			class="text-muted-foreground/70 shrink-0 transition-transform duration-150 {open ? 'rotate-180' : ''}" />
	</button>
	{#if open}
		<div class="px-4 pb-4 pt-1 border-t border-border/40">
			{@render children()}
		</div>
	{/if}
</section>
