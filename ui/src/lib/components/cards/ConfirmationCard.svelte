<script lang="ts">
	import Check from 'lucide-svelte/icons/check';
	import Info from 'lucide-svelte/icons/info';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';

	interface Props {
		title: string;
		description?: string;
		link?: { href: string; label: string };
		variant?: 'success' | 'info' | 'warning';
	}

	let { title, description, link, variant = 'success' }: Props = $props();

	const styles = {
		success: { border: 'border-emerald-500/20', bg: 'bg-emerald-500/5', icon: Check, iconColor: 'text-emerald-400' },
		info: { border: 'border-blue-500/20', bg: 'bg-blue-500/5', icon: Info, iconColor: 'text-blue-400' },
		warning: { border: 'border-amber-500/20', bg: 'bg-amber-500/5', icon: AlertTriangle, iconColor: 'text-amber-400' },
	};

	const style = $derived(styles[variant]);
</script>

<div class="rounded-lg border {style.border} {style.bg} p-3">
	<div class="flex items-start gap-2">
		<svelte:component this={style.icon} size={16} class="{style.iconColor} mt-0.5 shrink-0" />
		<div class="flex-1 min-w-0">
			<p class="text-sm text-foreground">{title}</p>
			{#if description}
				<p class="text-xs text-muted-foreground mt-0.5">{description}</p>
			{/if}
			{#if link}
				<a href={link.href} class="inline-block text-xs text-primary mt-1 hover:underline">{link.label}</a>
			{/if}
		</div>
	</div>
</div>
