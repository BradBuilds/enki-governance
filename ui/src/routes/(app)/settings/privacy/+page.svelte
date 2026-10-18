<script lang="ts">
	import { onMount } from 'svelte';
	import Shield from 'lucide-svelte/icons/shield';
	import Eye from 'lucide-svelte/icons/eye';
	import EyeOff from 'lucide-svelte/icons/eye-off';
	import Bell from 'lucide-svelte/icons/bell';
	import Plus from 'lucide-svelte/icons/plus';
	import X from 'lucide-svelte/icons/x';
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';

	let permissions = $state<any[]>([]);
	let accessLog = $state<any[]>([]);
	let loading = $state(true);
	let toast = $state<string | null>(null);

	// New permission form
	let showAdd = $state(false);
	let newIdentity = $state('');
	let newDataType = $state('location');
	let newAllowed = $state(true);
	let newAlert = $state(true);

	const dataTypes = ['location', 'contacts', 'financial', 'communications', 'photos', 'calendar', 'all'];

	async function loadAll() {
		loading = true;
		try {
			const [permRes, logRes] = await Promise.all([
				fetch('/api/permissions').then(r => r.ok ? r.json() : { permissions: [] }),
				fetch('/api/permissions/log').then(r => r.ok ? r.json() : { entries: [] }),
			]);
			permissions = permRes.permissions || [];
			accessLog = logRes.entries || [];
		} catch {}
		loading = false;
	}

	async function setPermission(identityId: string, dataType: string, allowed: boolean, alertOnAccess: boolean) {
		try {
			const res = await fetch('/api/permissions', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					identity_id: identityId,
					data_type: dataType,
					allowed,
					alert_on_access: alertOnAccess,
				}),
			});
			if (res.ok) {
				toast = 'Permission updated';
				await loadAll();
			}
		} catch {}
		setTimeout(() => toast = null, 2000);
	}

	async function addPermission() {
		if (!newIdentity.trim()) return;
		await setPermission(newIdentity.trim(), newDataType, newAllowed, newAlert);
		showAdd = false;
		newIdentity = '';
	}

	function timeAgo(ts: string): string {
		if (!ts) return '';
		const diff = Date.now() - new Date(ts).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}

	onMount(() => loadAll());
</script>

{#if toast}
	<div class="fixed top-4 right-4 z-50 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm shadow-lg">{toast}</div>
{/if}

<div class="space-y-4 max-w-3xl">
	<div class="flex items-center justify-between">
		<div>
			<h1 class="text-xl font-semibold text-foreground flex items-center gap-2">
				<Shield class="w-5 h-5 text-primary" /> Privacy & Access
			</h1>
			<p class="text-xs text-muted-foreground mt-0.5">Control who can see your personal data</p>
		</div>
		<div class="flex gap-2">
			<button onclick={loadAll} class="p-1.5 rounded text-muted-foreground hover:text-foreground">
				<RefreshCw class="w-4 h-4" />
			</button>
			<button onclick={() => showAdd = true} class="flex items-center gap-1 px-3 py-1.5 text-xs rounded-lg bg-primary text-primary-foreground">
				<Plus class="w-3.5 h-3.5" /> Add Rule
			</button>
		</div>
	</div>

	{#if loading}
		<div class="text-center py-8 text-muted-foreground">Loading...</div>
	{:else}
		<!-- Add permission form -->
		{#if showAdd}
			<div class="rounded-lg border border-primary/30 bg-primary/5 p-4">
				<div class="flex items-center justify-between mb-3">
					<h3 class="text-sm font-semibold text-foreground">New Permission Rule</h3>
					<button onclick={() => showAdd = false} class="text-muted-foreground hover:text-foreground"><X class="w-4 h-4" /></button>
				</div>
				<div class="grid grid-cols-2 gap-3">
					<div>
						<label class="text-[10px] text-muted-foreground uppercase tracking-wider block mb-1">User ID</label>
						<input type="text" bind:value={newIdentity} placeholder="sarah, analyst_1, etc."
							class="w-full px-2 py-1.5 text-xs rounded border border-border bg-background text-foreground" />
					</div>
					<div>
						<label class="text-[10px] text-muted-foreground uppercase tracking-wider block mb-1">Data Type</label>
						<select bind:value={newDataType} class="w-full px-2 py-1.5 text-xs rounded border border-border bg-background text-foreground">
							{#each dataTypes as dt}
								<option value={dt}>{dt}</option>
							{/each}
						</select>
					</div>
				</div>
				<div class="flex items-center gap-4 mt-3">
					<label class="flex items-center gap-2 text-xs text-foreground">
						<input type="checkbox" bind:checked={newAllowed} class="rounded" />
						Allow access
					</label>
					<label class="flex items-center gap-2 text-xs text-foreground">
						<input type="checkbox" bind:checked={newAlert} class="rounded" />
						Alert me on access
					</label>
					<button onclick={addPermission} class="ml-auto px-3 py-1.5 text-xs rounded bg-primary text-primary-foreground">Save</button>
				</div>
			</div>
		{/if}

		<!-- Current permissions -->
		<div>
			<h2 class="text-sm font-semibold text-muted-foreground mb-2">Who Can See My Data</h2>
			{#if permissions.length === 0}
				<div class="rounded-lg border border-border bg-card p-4 text-center text-xs text-muted-foreground">
					<Shield class="w-6 h-6 mx-auto mb-2 opacity-30" />
					No permission rules set. All cross-identity access is denied by default.
				</div>
			{:else}
				<div class="space-y-1.5">
					{#each permissions as perm}
						<div class="rounded-lg border border-border bg-card p-3 flex items-center justify-between">
							<div class="flex items-center gap-3">
								{#if perm.allowed}
									<Eye class="w-4 h-4 text-emerald-400" />
								{:else}
									<EyeOff class="w-4 h-4 text-red-400" />
								{/if}
								<div>
									<span class="text-xs font-semibold text-foreground">{perm.identity_id}</span>
									<span class="text-xs text-muted-foreground ml-2">
										{perm.allowed ? 'can view' : 'blocked from'} <span class="font-medium">{perm.data_type}</span>
									</span>
								</div>
								{#if perm.alert_on_access}
									<span title="Alert on access"><Bell class="w-3 h-3 text-amber-400" /></span>
								{/if}
							</div>
							<div class="flex items-center gap-2">
								<button
									onclick={() => setPermission(perm.identity_id, perm.data_type, !perm.allowed, perm.alert_on_access)}
									class="text-[10px] px-2 py-1 rounded {perm.allowed ? 'bg-red-500/10 text-red-400' : 'bg-emerald-500/10 text-emerald-400'}"
								>
									{perm.allowed ? 'Revoke' : 'Grant'}
								</button>
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</div>

		<!-- Access log -->
		<div>
			<h2 class="text-sm font-semibold text-muted-foreground mb-2">Recent Access Log</h2>
			{#if accessLog.length === 0}
				<div class="rounded-lg border border-border bg-card p-4 text-center text-xs text-muted-foreground">
					No access events recorded yet.
				</div>
			{:else}
				<div class="space-y-0.5">
					{#each accessLog.slice(0, 20) as entry}
						<div class="flex items-center justify-between text-xs py-1.5 px-3 rounded hover:bg-muted/20
							{entry.allowed ? '' : 'bg-red-500/5'}">
							<div class="flex items-center gap-2">
								{#if entry.allowed}
									<Eye class="w-3 h-3 text-muted-foreground" />
								{:else}
									<EyeOff class="w-3 h-3 text-red-400" />
								{/if}
								<span class="font-medium text-foreground">{entry.querier_id}</span>
								<span class="text-muted-foreground">{entry.allowed ? 'accessed' : 'was denied'}</span>
								<span class="font-medium text-foreground">{entry.data_type}</span>
								<span class="text-muted-foreground/50">via {entry.context}</span>
							</div>
							<span class="text-[9px] text-muted-foreground/50">{timeAgo(entry.timestamp)}</span>
						</div>
					{/each}
				</div>
			{/if}
		</div>
	{/if}
</div>
