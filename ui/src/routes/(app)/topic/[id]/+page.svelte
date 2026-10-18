<script lang="ts">
	import Network from 'lucide-svelte/icons/network';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();
	const topic = $derived(data.topic);
	const reclassified = $derived(data.reclassified);

	const entityMembers = $derived(
		topic?.members?.filter((m: any) => m.member_kind === 'entity') ?? []
	);
	const otherMembers = $derived(
		topic?.members?.filter((m: any) => m.member_kind !== 'entity') ?? []
	);
</script>

<div class="max-w-4xl mx-auto p-4 space-y-3">
	{#if reclassified}
		<div class="rounded-xl border border-amber-500/40 bg-amber-500/5 p-5 space-y-2">
			<div class="flex items-center gap-2">
				<AlertTriangle class="w-4 h-4 text-amber-400" />
				<h2 class="text-sm font-semibold">This topic was reclassified</h2>
			</div>
			{#if reclassified.pending && reclassified.target_category}
				<p class="text-xs text-muted-foreground">
					Queued for migration to <code>{reclassified.target_category}.{reclassified.target_subtype ?? '?'}</code>.
					Once the migrator runs, the new dossier will be linked here.
				</p>
			{:else if reclassified.sweep_action === 'dropped_garbage'}
				<p class="text-xs text-muted-foreground">
					Flagged as noise during decontamination ({reclassified.sweep_reason ?? 'no reason'}). No dossier exists.
				</p>
			{:else if reclassified.sweep_action === 'unmapped_residual'}
				<p class="text-xs text-muted-foreground">
					Decontamination couldn't classify this topic ({reclassified.sweep_reason ?? 'no mapping'}). Tombstoned, not yet routed.
				</p>
			{:else}
				<p class="text-xs text-muted-foreground">
					This topic was reclassified but the redirect target is missing.
				</p>
			{/if}
		</div>
	{:else if topic}
		<header class="rounded-xl border border-border bg-card px-5 py-4">
			<div class="flex items-baseline gap-2.5 mb-1 flex-wrap">
				<Network size={18} class="text-primary shrink-0" />
				<h1 class="text-lg font-semibold text-foreground flex-1 min-w-0 break-words">
					{topic.canonical_name}
				</h1>
				{#if topic.base60_address}
					<span class="text-[11px] font-mono px-2 py-0.5 rounded
						bg-primary/10 text-primary/90 border border-primary/20"
						title="Base-60 substrate address">
						{topic.base60_address}
					</span>
				{/if}
				{#if topic.canonical_type}
					<span class="text-[10px] uppercase tracking-wider text-muted-foreground/80 bg-muted px-1.5 py-0.5 rounded">
						{topic.canonical_type}
					</span>
				{/if}
				{#if topic.federation && topic.federation.n_nodes > 1}
					<span class="text-[10px] tabular-nums px-1.5 py-0.5 rounded
						bg-primary/10 text-primary border border-primary/20"
						title={`+${topic.federation.n_nodes - 1} other nodes recognize this topic; network member count ${topic.federation.network_member_count}`}>
						🌐 +{topic.federation.n_nodes - 1} {topic.federation.n_nodes === 2 ? 'node' : 'nodes'}
					</span>
				{/if}
			</div>
			<div class="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground/80 mt-2">
				<span>{topic.member_count ?? 0} members</span>
				{#if topic.topic_kind}<span>· kind: <span class="text-foreground/90">{topic.topic_kind}</span></span>{/if}
				{#if topic.created_by}<span>· created by <span class="text-foreground/90">{topic.created_by}</span></span>{/if}
				{#if topic.publish_membership_policy}
					<span>· redaction: <span class="font-mono text-foreground/90">{topic.publish_membership_policy}</span></span>
				{/if}
				{#if topic.updated_at}
					<span>· updated {topic.updated_at.slice(0, 10)}</span>
				{/if}
			</div>
		</header>

		{#if entityMembers.length > 0}
			<section class="rounded-xl border border-border bg-card px-5 py-4">
				<h2 class="text-sm font-semibold text-foreground mb-2">
					Entity members <span class="text-muted-foreground/60 font-normal">({entityMembers.length})</span>
				</h2>
				<ul class="space-y-1 text-xs">
					{#each entityMembers.slice(0, 100) as m}
						<li class="flex items-baseline gap-2">
							<span class="text-[10px] uppercase tracking-wider text-muted-foreground/60 w-20 shrink-0">
								{m.canonical_type || 'entity'}
							</span>
							<a href={`/entity/${m.member_id}`}
								class="text-foreground hover:text-primary hover:underline truncate flex-1">
								{m.canonical_name || `entity ${m.member_id}`}
							</a>
							{#if m.score && m.score > 1}
								<span class="text-[10px] text-emerald-400/80 tabular-nums shrink-0"
									title="Membership score">
									{m.score.toFixed(1)}
								</span>
							{/if}
							{#if m.via_rule}
								<span class="text-[10px] text-muted-foreground/60 font-mono shrink-0"
									title="Membership rule">
									{m.via_rule}
								</span>
							{/if}
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		{#if otherMembers.length > 0}
			<section class="rounded-xl border border-border bg-card px-5 py-4">
				<h2 class="text-sm font-semibold text-foreground mb-2">
					Other members <span class="text-muted-foreground/60 font-normal">({otherMembers.length})</span>
				</h2>
				<ul class="space-y-1 text-xs">
					{#each otherMembers.slice(0, 50) as m}
						<li class="flex items-baseline gap-2">
							<span class="text-[10px] uppercase tracking-wider text-muted-foreground/60 w-20 shrink-0">
								{m.member_kind}
							</span>
							<span class="text-foreground/80 truncate flex-1">
								{m.canonical_name || `${m.member_kind} ${m.member_id}`}
							</span>
							{#if m.score && m.score > 1}
								<span class="text-[10px] text-emerald-400/80 tabular-nums shrink-0">
									{m.score.toFixed(1)}
								</span>
							{/if}
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		<div class="text-xs text-muted-foreground/70 pt-2">
			Topic ID <span class="font-mono">{topic.topic_id}</span>
		</div>
	{/if}
</div>
