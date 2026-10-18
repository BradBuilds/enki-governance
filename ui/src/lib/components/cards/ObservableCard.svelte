<script lang="ts">
	import Radio from 'lucide-svelte/icons/radio';

	interface Props {
		observables: any[];
	}

	let { observables }: Props = $props();
	const shown = $derived(observables.slice(0, 10));

	const typeIcons: Record<string, string> = {
		ip_address: 'IP',
		email_address: '@',
		domain_name: 'DNS',
		phone_number: 'TEL',
		license_plate: 'PLT',
		file_hash_sha256: '#',
		url: 'URL',
	};
</script>

<div class="rounded-lg border bg-card p-3">
	<div class="flex items-center gap-2 mb-2">
		<Radio size={14} class="text-muted-foreground" />
		<span class="text-xs font-medium text-muted-foreground">{observables.length} observables</span>
	</div>
	<div class="space-y-1">
		{#each shown as obs}
			<a
				href="/search?type={obs.observable_type || obs.type}&value={encodeURIComponent(obs.value || obs.observable_value || '')}"
				class="flex items-center gap-2 text-xs py-1.5 px-2 rounded bg-muted/30 hover:bg-muted/50 transition-colors"
			>
				<span class="w-7 text-center font-mono text-[10px] text-muted-foreground shrink-0">
					{typeIcons[obs.observable_type || obs.type] || '?'}
				</span>
				<span class="text-foreground truncate font-mono">{obs.value || obs.observable_value || '—'}</span>
			</a>
		{/each}
	</div>
</div>
