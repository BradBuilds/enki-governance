<script lang="ts">
	interface Props {
		text: string;
		variant?: 'critical' | 'high' | 'medium' | 'low' | 'info' | 'success' | 'neutral';
		size?: 'sm' | 'md';
	}

	let { text, variant = 'neutral', size = 'sm' }: Props = $props();

	const colors: Record<string, string> = {
		critical: 'bg-[var(--severity-critical)]/15 text-[var(--severity-critical)] border-[var(--severity-critical)]/30',
		high: 'bg-[var(--severity-high)]/15 text-[var(--severity-high)] border-[var(--severity-high)]/30',
		medium: 'bg-[var(--severity-medium)]/15 text-[var(--severity-medium)] border-[var(--severity-medium)]/30',
		low: 'bg-[var(--severity-low)]/15 text-[var(--severity-low)] border-[var(--severity-low)]/30',
		info: 'bg-[var(--severity-info)]/15 text-[var(--severity-info)] border-[var(--severity-info)]/30',
		success: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
		neutral: 'bg-muted text-muted-foreground border-border',
	};

	const sizes: Record<string, string> = {
		sm: 'text-xs px-1.5 py-0.5',
		md: 'text-sm px-2 py-0.5',
	};
</script>

<span class="inline-flex items-center rounded-full border font-medium leading-none {colors[variant]} {sizes[size]}">
	{text}
</span>
