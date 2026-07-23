<script lang="ts">
	// Generic type-aware profile renderer. The backend (cards_api) emits
	// `entity.structured_profile` as a list of {key, label, value, kind}
	// pre-filtered by libraries/displayvocab/ — so this component never
	// hardcodes any field names. Adding a new entity type or a new
	// field is a pack change, not a code change.
	let { fields, source = null }:
		{ fields: Array<{ key: string; label: string; value: any; kind: string }>;
		  source?: string | null } = $props();

	function formatValue(value: any, kind: string): string {
		if (value == null) return '—';
		if (Array.isArray(value)) {
			return value.map((v) => formatValue(v, kind)).filter(Boolean).join(', ');
		}
		if (typeof value === 'object') {
			if (value.label) return String(value.label);
			if (value.id) return String(value.id);
			return JSON.stringify(value);
		}
		const s = String(value);
		switch (kind) {
			case 'number': {
				const n = Number(s);
				return Number.isFinite(n) ? n.toLocaleString() : s;
			}
			case 'money': {
				const n = Number(s);
				return Number.isFinite(n)
					? n.toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
					: s;
			}
			case 'date': {
				// Accept YYYY-MM-DD or full ISO; render as YYYY-MM-DD
				const m = s.match(/^(\d{4})(-\d{2}-\d{2})?/);
				return m ? m[0] : s;
			}
			case 'year': {
				const m = s.match(/^\d{4}/);
				return m ? m[0] : s;
			}
			case 'datetime': {
				try {
					return new Date(s).toLocaleString();
				} catch {
					return s;
				}
			}
			default:
				return s;
		}
	}
</script>

{#if fields && fields.length > 0}
	<section class="rounded-xl border border-border bg-card px-5 py-4 mt-3">
		<header class="flex items-baseline justify-between mb-3">
			<h2 class="text-sm font-semibold text-foreground">Profile</h2>
			{#if source}
				<span class="text-[10px] uppercase tracking-wider text-muted-foreground/60">
					from {source}
				</span>
			{/if}
		</header>
		<dl class="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2 text-xs">
			{#each fields as f}
				<div class="flex items-baseline gap-3 py-0.5">
					<dt class="text-[10px] uppercase tracking-wider text-muted-foreground/70 w-32 shrink-0">
						{f.label}
					</dt>
					<dd class="text-foreground/90 flex-1 min-w-0">
						{#if f.kind === 'url'}
							<a href={String(f.value)} target="_blank" rel="noopener noreferrer"
								class="text-primary hover:underline truncate inline-block max-w-full">
								{formatValue(f.value, f.kind)}
							</a>
						{:else if f.kind === 'coords' && Array.isArray(f.value) && f.value.length === 2}
							<a href={`https://www.google.com/maps/search/?api=1&query=${f.value[0]},${f.value[1]}`}
								target="_blank" rel="noopener noreferrer"
								class="text-primary hover:underline font-mono">
								{Number(f.value[0]).toFixed(4)}, {Number(f.value[1]).toFixed(4)}
							</a>
						{:else}
							<span class="break-words">{formatValue(f.value, f.kind)}</span>
						{/if}
					</dd>
				</div>
			{/each}
		</dl>
	</section>
{/if}
