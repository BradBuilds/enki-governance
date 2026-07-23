<script lang="ts">
	import { notifications } from '$lib/stores/notifications.svelte';
	import Bell from 'lucide-svelte/icons/bell';

	let dropdownOpen = $state(false);
</script>

<div class="relative">
	<button
		onclick={() => dropdownOpen = !dropdownOpen}
		class="relative p-2 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
		title="Notifications"
	>
		<Bell size={18} />
		{#if notifications.unread > 0}
			<span class="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] rounded-full bg-destructive text-destructive-foreground text-[10px] flex items-center justify-center leading-none px-1 border-2 border-card">
				{notifications.unread > 99 ? '99+' : notifications.unread}
			</span>
		{/if}
	</button>

	{#if dropdownOpen}
		<!-- Backdrop -->
		<button class="fixed inset-0 z-40" onclick={() => dropdownOpen = false} aria-label="Close notifications"></button>
		<!-- Dropdown -->
		<div class="absolute right-0 top-full mt-1 w-72 rounded-lg border border-border bg-card shadow-xl z-50">
			<div class="flex items-center justify-between px-3 py-2 border-b border-border">
				<span class="text-sm font-medium text-foreground">Notifications</span>
				{#if notifications.unread > 0}
					<span class="text-[10px] text-muted-foreground">{notifications.unread} unread</span>
				{/if}
			</div>
			<div class="max-h-80 overflow-y-auto">
				{#if notifications.alertsNew > 0}
					<a href="/governance" onclick={() => dropdownOpen = false} class="flex items-center gap-3 px-3 py-2.5 hover:bg-muted transition-colors">
						<span class="w-2 h-2 rounded-full bg-[var(--severity-high)] shrink-0"></span>
						<div class="flex-1 min-w-0">
							<p class="text-sm text-foreground">{notifications.alertsNew} new alert{notifications.alertsNew > 1 ? 's' : ''}</p>
							<p class="text-[10px] text-muted-foreground">Requires attention</p>
						</div>
					</a>
				{/if}
				{#if notifications.connectorsError > 0}
					<a href="/connectors" onclick={() => dropdownOpen = false} class="flex items-center gap-3 px-3 py-2.5 hover:bg-muted transition-colors">
						<span class="w-2 h-2 rounded-full bg-[var(--severity-critical)] shrink-0"></span>
						<div class="flex-1 min-w-0">
							<p class="text-sm text-foreground">{notifications.connectorsError} connector error{notifications.connectorsError > 1 ? 's' : ''}</p>
							<p class="text-[10px] text-muted-foreground">Data sources need attention</p>
						</div>
					</a>
				{/if}
				{#if notifications.processingPending > 0}
					<a href="/processing" onclick={() => dropdownOpen = false} class="flex items-center gap-3 px-3 py-2.5 hover:bg-muted transition-colors">
						<span class="w-2 h-2 rounded-full bg-[var(--severity-medium)] shrink-0"></span>
						<div class="flex-1 min-w-0">
							<p class="text-sm text-foreground">{notifications.processingPending} items pending review</p>
							<p class="text-[10px] text-muted-foreground">LDU-P queue</p>
						</div>
					</a>
				{/if}
				{#if notifications.unread === 0}
					<div class="px-3 py-6 text-center text-sm text-muted-foreground">
						All caught up
					</div>
				{/if}
			</div>
			<a href="/governance" onclick={() => dropdownOpen = false} class="block px-3 py-2 text-xs text-primary hover:bg-muted text-center border-t border-border transition-colors">
				View all alerts
			</a>
		</div>
	{/if}
</div>
