<script lang="ts">
	import { page } from '$app/stores';
</script>

<div class="flex items-center justify-center min-h-screen bg-background text-foreground">
	<div class="text-center max-w-md mx-auto px-6">
		{#if $page.status === 404}
			<div class="text-6xl font-bold text-muted-foreground mb-4">404</div>
			<h1 class="text-xl font-semibold mb-2">Page not found</h1>
			<p class="text-muted-foreground mb-8">
				The page you're looking for doesn't exist or has been moved.
			</p>
		{:else if $page.status >= 500}
			<div class="text-6xl font-bold text-destructive mb-4">{$page.status}</div>
			<h1 class="text-xl font-semibold mb-2">Server error</h1>
			<p class="text-muted-foreground mb-8">
				Something went wrong on our end. The API may be unreachable.
			</p>
		{:else}
			<div class="text-6xl font-bold text-muted-foreground mb-4">{$page.status}</div>
			<h1 class="text-xl font-semibold mb-2">Something went wrong</h1>
			<p class="text-muted-foreground mb-8">
				{$page.error?.message || 'An unexpected error occurred.'}
			</p>
		{/if}

		<div class="flex gap-4 justify-center">
			<a
				href="/hub"
				class="inline-flex items-center px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors"
			>
				Return to Hub
			</a>
			<button
				onclick={() => window.location.reload()}
				class="inline-flex items-center px-4 py-2 rounded-md bg-secondary text-secondary-foreground text-sm font-medium hover:bg-secondary/80 transition-colors"
			>
				Retry
			</button>
		</div>
	</div>
</div>
