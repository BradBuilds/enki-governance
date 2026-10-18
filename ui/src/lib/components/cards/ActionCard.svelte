<script lang="ts">
	import HelpCircle from 'lucide-svelte/icons/help-circle';

	interface Props {
		title: string;
		description?: string;
		confirmLabel?: string;
		cancelLabel?: string;
		onConfirm: () => void;
		onCancel?: () => void;
	}

	let {
		title,
		description,
		confirmLabel = 'Confirm',
		cancelLabel = 'Cancel',
		onConfirm,
		onCancel
	}: Props = $props();
</script>

<div class="rounded-lg border border-primary/30 bg-primary/5 p-4">
	<div class="flex items-start gap-3">
		<div class="w-8 h-8 rounded-full bg-primary/10 flex items-center justify-center shrink-0 mt-0.5">
			<HelpCircle size={16} class="text-primary" />
		</div>
		<div class="flex-1 min-w-0">
			<p class="text-sm font-medium text-foreground">{title}</p>
			{#if description}
				<p class="text-xs text-muted-foreground mt-1">{description}</p>
			{/if}
			<div class="flex items-center gap-2 mt-3">
				<button
					onclick={onConfirm}
					class="px-3 py-1.5 rounded-lg bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 transition-colors"
				>
					{confirmLabel}
				</button>
				{#if onCancel}
					<button
						onclick={onCancel}
						class="px-3 py-1.5 rounded-lg border border-border text-xs text-muted-foreground hover:bg-muted transition-colors"
					>
						{cancelLabel}
					</button>
				{/if}
			</div>
		</div>
	</div>
</div>
