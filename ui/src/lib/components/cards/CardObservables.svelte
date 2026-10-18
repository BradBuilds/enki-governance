<script lang="ts">
	import Search from 'lucide-svelte/icons/search';

	let { observables = [] }: { observables: any[] } = $props();

	const typeColors: Record<string, string> = {
		ip_address: 'bg-enki-cyan/10 text-enki-cyan border-enki-cyan/20',
		mac_address: 'bg-enki-orange/10 text-enki-orange border-enki-orange/20',
		email_address: 'bg-primary/10 text-primary border-primary/20',
		domain_name: 'bg-enki-purple/10 text-enki-purple border-enki-purple/20',
		license_plate: 'bg-enki-orange/10 text-enki-orange border-enki-orange/20',
		url: 'bg-enki-green/10 text-enki-green border-enki-green/20',
		file_hash_sha256: 'bg-muted text-muted-foreground border-border',
		phone_number: 'bg-enki-cyan/10 text-enki-cyan border-enki-cyan/20',
		coordinates: 'bg-enki-green/10 text-enki-green border-enki-green/20',
	};
</script>

{#if observables.length > 0}
	<div class="flex flex-wrap gap-1.5">
		{#each observables as obs}
			<a
				href="/search?type={obs.observable_type}&value={encodeURIComponent(obs.observable_value || obs.value || '')}"
				class="inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[10px] font-mono hover:opacity-80 transition-opacity {typeColors[obs.observable_type || obs.type] || 'bg-muted text-muted-foreground border-border'}"
			>
				<Search class="w-2.5 h-2.5" />
				<span class="opacity-60">{(obs.observable_type || obs.type || '').replace('_', ' ')}:</span>
				<span>{obs.observable_value || obs.value || ''}</span>
			</a>
		{/each}
	</div>
{/if}
