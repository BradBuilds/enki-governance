<script lang="ts">
	import type { PageData } from './$types';
	import Search from 'lucide-svelte/icons/search';

	let { data }: { data: PageData } = $props();
	// Only rendered when the load() didn't redirect (i.e. no row had this address).
</script>

<div class="max-w-2xl mx-auto p-6">
	<div class="rounded-xl border border-border bg-card px-6 py-8 text-center">
		<Search size={24} class="mx-auto mb-3 text-muted-foreground" />
		<h1 class="text-lg font-semibold text-foreground mb-1">No row at this address</h1>
		<p class="text-sm text-muted-foreground mb-4">
			Nothing in the substrate is bound to
			<code class="font-mono text-foreground/90 px-1.5 py-0.5 rounded bg-muted">{data.address}</code>
			yet. Addresses are assigned at admission time; if the entity exists by name but
			hasn't been admitted under the current pack version, it won't appear here.
		</p>
		<div class="flex gap-2 justify-center text-xs">
			<a href="/library" class="px-3 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90">
				Browse libraries
			</a>
			<a href="/hub" class="px-3 py-1.5 rounded-md border border-border hover:bg-muted">
				Back to hub
			</a>
		</div>
	</div>
</div>
