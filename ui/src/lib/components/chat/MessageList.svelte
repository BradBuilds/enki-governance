<script lang="ts">
	// Shared message renderer — bubbles + action-result cards + typing indicator.
	// The single card-rendering surface used by the bubble, the /chat page, and
	// the library Chat tab so every entry point shows the same rich results.
	import type { ChatMessage } from '$lib/stores/chat.svelte';

	import Bot from 'lucide-svelte/icons/bot';
	import User from 'lucide-svelte/icons/user';
	import Cloud from 'lucide-svelte/icons/cloud';
	import Network from 'lucide-svelte/icons/network';

	import SignalCard from '$lib/components/cards/SignalCard.svelte';
	import AlertCard from '$lib/components/cards/AlertCard.svelte';
	import CaseCard from '$lib/components/cards/CaseCard.svelte';
	import StatusCard from '$lib/components/cards/StatusCard.svelte';
	import MapPreviewCard from '$lib/components/cards/MapPreviewCard.svelte';
	import AircraftCard from '$lib/components/cards/AircraftCard.svelte';
	import TimelineCard from '$lib/components/cards/TimelineCard.svelte';
	import BriefCard from '$lib/components/cards/BriefCard.svelte';
	import ExportCard from '$lib/components/cards/ExportCard.svelte';
	import { EntityCard, FactCard, EventCard, RefCard, LibraryCard } from '$lib/components/cards2';
	import ConnectorCard from '$lib/components/cards/ConnectorCard.svelte';
	import ObservableCard from '$lib/components/cards/ObservableCard.svelte';
	import ConfirmationCard from '$lib/components/cards/ConfirmationCard.svelte';
	import WorkerActivityCard from '$lib/components/cards/WorkerActivityCard.svelte';
	import TripCard from '$lib/components/cards/TripCard.svelte';
	import CorrelationCard from '$lib/components/cards/CorrelationCard.svelte';
	import PhotoGalleryCard from '$lib/components/cards/PhotoGalleryCard.svelte';

	interface Props {
		messages: ChatMessage[];
		sending?: boolean;
	}
	let { messages, sending = false }: Props = $props();
</script>

{#each messages as msg}
	<div class="flex gap-2.5 {msg.role === 'user' ? 'flex-row-reverse' : ''}">
		<div class="w-7 h-7 rounded-full flex items-center justify-center shrink-0 {msg.role === 'user' ? 'bg-primary/10' : 'bg-muted'}">
			{#if msg.role === 'user'}
				<User size={14} class="text-primary" />
			{:else}
				<Bot size={14} class="text-muted-foreground" />
			{/if}
		</div>
		<div class="flex-1 min-w-0 {msg.role === 'user' ? 'text-right' : ''}">
			<div class="inline-block text-sm text-foreground {msg.role === 'user' ? 'bg-primary/10 rounded-2xl rounded-tr-sm px-3 py-2' : ''} max-w-full text-left whitespace-pre-wrap break-words">
				{msg.content}
			</div>
			{#if msg.model_used}
				{@const msgCloud = /\b(gpt|claude|gemini|grok|mistral|deepseek|command|sonar)\b/i.test(msg.model_used)}
				<div class="text-[9px] mt-0.5 flex items-center gap-1 {msgCloud ? 'text-amber-400/70' : 'text-muted-foreground/40'}">
					{#if msgCloud}<Cloud size={9} /> external:{/if}{msg.model_used}
				</div>
			{/if}
			<!-- Nodes produced — simple links (advanced pin/dim lives in the graph view) -->
			{#if msg.nodes_produced?.length}
				<div class="mt-1.5 flex flex-wrap gap-1 {msg.role === 'user' ? 'justify-end' : ''}">
					{#each msg.nodes_produced as nodeId}
						<a
							href="/nodes/{encodeURIComponent(nodeId)}"
							class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border border-border bg-muted text-muted-foreground text-[10px] hover:brightness-110 transition-all"
							title={nodeId}
						>
							<Network size={8} class="shrink-0" />
							<span class="truncate max-w-[120px]">{nodeId.slice(0, 14)}</span>
						</a>
					{/each}
				</div>
			{/if}
			<!-- Action results -->
			{#if msg.action_results?.length}
				<div class="mt-2 space-y-2 text-left">
					{#each msg.action_results as action}
						{@const act = action.action}
						{@const d = action.data}
						{#if act === 'ldu_queue' || act === 'list_discrepancies'}
							{#if (d?.discrepancies || []).length}
								<div class="space-y-1">
									<div class="text-[10px] font-medium text-muted-foreground uppercase tracking-wide">{d.count} unresolved — ask "who is X?" to get a proposal</div>
									{#each d.discrepancies.slice(0, 12) as disc}
										<div class="rounded-lg border border-border bg-card p-2 text-xs">
											<div class="flex items-center justify-between gap-2">
												<span class="font-medium text-foreground">{disc.surface_form}</span>
												<span class="text-[10px] text-amber-400/80">{disc.reason}</span>
											</div>
											{#if (disc.candidates || []).length}
												<div class="text-[10px] text-muted-foreground mt-0.5 truncate">
													maybe: {disc.candidates.slice(0, 3).map((c: any) => `${c.name} (${c.score})`).join(' · ')}
												</div>
											{/if}
										</div>
									{/each}
								</div>
							{:else}
								<div class="text-xs text-muted-foreground">Nothing unresolved in the LDU.</div>
							{/if}
						{:else if act === 'propose_resolution'}
							<div class="rounded-lg border border-primary/30 bg-primary/5 p-3 text-xs space-y-1">
								<div class="font-medium text-foreground">{d?.surface}</div>
								{#if d?.proposal}
									<div class="text-foreground/90">→ proposed: <span class="font-medium">{d.proposal.name}</span></div>
									<div class="text-muted-foreground">{d?.reasoning}</div>
									<div class="flex items-center gap-2 pt-0.5">
										<span class="text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded {d?.source === 'llm' ? 'bg-amber-500/15 text-amber-300' : 'bg-emerald-500/15 text-emerald-400'}">{d?.source === 'llm' ? `proposed by ${d?.model || 'model'}` : 'deterministic'}</span>
										<span class="text-[10px] text-muted-foreground">confirm: "assign {d?.surface} to {d.proposal.name}"</span>
									</div>
								{:else}
									<div class="text-muted-foreground">{d?.reasoning || 'No confident proposal — left parked.'}</div>
								{/if}
							</div>
						{:else if act === 'resolve_discrepancy'}
							<ConfirmationCard
								title={d?.message || (d?.resolved ? 'Resolved' : 'Not resolved')}
								description={d?.assigned_to ? `Scoped, validated alias recorded.` : undefined}
								variant={d?.resolved ? 'success' : 'warning'}
							/>
						{:else if act === 'answer_about'}
							<div class="space-y-2">
								<div class="text-sm text-foreground/90 leading-relaxed whitespace-pre-wrap break-words">{d?.answer || ''}</div>
								{#if (d?.sources || []).length}
									<div class="pt-1">
										<div class="text-[10px] font-medium text-muted-foreground uppercase tracking-wide mb-1">Sources</div>
										<ol class="space-y-0.5">
											{#each d.sources.slice(0, 12) as s}
												<li class="text-[11px] flex items-start gap-1.5">
													<span class="text-muted-foreground shrink-0">[{s.marker}]</span>
													{#if s.document_id}
														<a href="/document/{s.document_id}" class="text-primary hover:underline break-words">{s.title || s.source_url || `document ${s.document_id}`}</a>
													{:else}
														<span class="text-foreground/80 break-words">{s.title || s.source_url || 'source'}</span>
													{/if}
												</li>
											{/each}
										</ol>
									</div>
								{/if}
								{#if d?.entity_id}
									<a href="/entity/{d.entity_id}" class="inline-block text-[10px] text-muted-foreground/70 hover:text-primary">open {d.canonical_name || 'entity'} profile →</a>
								{/if}
							</div>
						{:else if act === 'search_signals' || act === 'get_signal'}
							{#each (d?.signals || (d ? [d] : [])).slice(0, 5) as sig}
								<SignalCard signal={sig} variant="standard" />
							{/each}
							{#if (d?.count || d?.signals?.length || 0) > 5}
								<a href="/signals" class="text-[10px] text-primary hover:underline">View all {d.count || d.signals.length} signals</a>
							{/if}
						{:else if act === 'list_alerts' || act === 'get_alert'}
							{#each (d?.alerts || (d ? [d] : [])).slice(0, 5) as a}
								<AlertCard alert={a} variant="standard" />
							{/each}
						{:else if act === 'list_cases' || act === 'get_case'}
							{#each (d?.cases || (d ? [d] : [])).slice(0, 5) as c}
								<CaseCard caseData={c} variant="standard" />
							{/each}
						{:else if act === 'get_dashboard'}
							<StatusCard
								title="System Overview"
								status="healthy"
								metrics={[
									{ label: 'Signals', value: d?.signals?.total ?? 0 },
									{ label: 'Alerts', value: d?.alerts?.total ?? 0 },
									{ label: 'Cases', value: d?.cases?.total ?? 0 },
								]}
							/>
						{:else if act === 'navigate_to' || act === 'search_nearby' || act === 'create_geofence' || act === 'highlight_area' || act === 'analyze_device' || act === 'enrich_location' || act === 'nearby_signals'}
							<MapPreviewCard action={act} data={d || {}} />
						{:else if act === 'live_aircraft' && d?.aircraft}
							<AircraftCard aircraft={d.aircraft} count={d.count || d.aircraft.length} />
						{:else if act === 'get_timeline' && d?.events}
							<TimelineCard events={d.events} />
						{:else if act === 'generate_brief'}
							<BriefCard summary={d?.summary || ''} sections={d?.sections || []} />
						{:else if act === 'export_signals' || act === 'export_cases' || act === 'export_contacts'}
							<ExportCard downloadUrl={d?.download_url || '#'} type={d?.type} format={d?.format} />
						{:else if act === 'connectors' || act === 'data_sources'}
							<ConnectorCard connectors={d?.connectors || d?.sources || []} />
						{:else if act === 'show_photos' && d?.photos}
							<PhotoGalleryCard photos={d.photos} count={d.count || d.photos.length} />
						{:else if act === 'cross_correlate' && d?.groups}
							<CorrelationCard groups={d.groups} total={d.total || 0} center={d.center} radius_m={d.radius_m} time_range={d.time_range} />
						{:else if act === 'show_trips' && d?.trips}
							<TripCard trips={d.trips} count={d.count || d.trips.length} />
						{:else if act === 'worker_activity'}
							<WorkerActivityCard events={d?.events || []} limit={d?.count || 10} />
						{:else if act === 'investigate_entity'}
							<ConfirmationCard
								title="Investigation queued: {d?.name || 'entity'}"
								description={d?.message || `Queued for ${d?.entity_type || 'entity'} investigation`}
								variant="info"
							/>
						{:else if act === 'deep_research'}
							<ConfirmationCard
								title="Deep research started: {d?.topic || 'topic'}"
								description={d?.message || 'Running in the background — building a cited report.'}
								variant="info"
								link={d?.run_id ? { href: `/research/${d.run_id}`, label: 'Open run' } : undefined}
							/>
							{#if d?.director_model}
								<span class="mt-1 inline-flex items-center gap-1 text-[9px] font-medium px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-300 border border-amber-500/30">
									<Cloud size={10} /> External · {d.director_model}
								</span>
							{/if}
						{:else if act === 'research_status' || act === 'list_research'}
							{#if (d?.runs || []).length}
								<div class="space-y-1">
									{#each d.runs.slice(0, 6) as run}
										<a href="/research/{run.run_id}" class="block rounded-lg border border-border bg-card p-2 text-xs hover:bg-muted transition-colors">
											<div class="flex items-center justify-between gap-2">
												<span class="font-medium text-foreground truncate">{run.goal}</span>
												<span class="text-[10px] px-1.5 py-0.5 rounded-full shrink-0 {run.status === 'done' ? 'bg-emerald-500/15 text-emerald-400' : run.status === 'failed' ? 'bg-red-500/15 text-red-400' : 'bg-amber-500/15 text-amber-400'}">{run.status}</span>
											</div>
											<div class="text-[10px] text-muted-foreground mt-0.5">
												iter {run.iteration}/{run.max_iterations} · {run.sources_grabbed} source{run.sources_grabbed === 1 ? '' : 's'} · {run.entities_count} entities · {run.facts_count} facts
											</div>
										</a>
									{/each}
								</div>
							{:else}
								<div class="text-xs text-muted-foreground">No research runs yet.</div>
							{/if}
						{:else if act === 'search_observables' && d?.observables}
							<ObservableCard observables={d.observables} />
						{:else if act === 'search_nodes' || act === 'get_node'}
							<div class="space-y-1">
								{#each (d?.nodes || (d ? [d] : [])).slice(0, 5) as node}
									<a href="/nodes/{encodeURIComponent(node.node_id || '')}" class="block rounded-lg border border-border bg-card p-2 text-xs hover:bg-muted transition-colors">
										<span class="font-medium text-foreground">{node.title || node.node_id}</span>
										{#if node.epistemic_type}<span class="text-muted-foreground ml-1">({node.epistemic_type})</span>{/if}
										{#if node.node_type}<span class="text-muted-foreground ml-1">· {node.node_type}</span>{/if}
									</a>
								{/each}
							</div>
						{:else if act === 'search_contacts' && d?.contacts}
							<div class="space-y-1">
								{#each (d.contacts || []).slice(0, 5) as contact}
									<a href="/contacts/{contact.contact_id}" class="block rounded-lg border border-border bg-card p-2 text-xs hover:bg-muted transition-colors">
										<span class="font-medium text-foreground">{contact.name}</span>
										<span class="text-muted-foreground ml-1">({contact.contact_type})</span>
									</a>
								{/each}
							</div>
						{:else if act === 'search_assets' && d?.assets}
							<div class="space-y-1">
								{#each (d.assets || []).slice(0, 5) as asset}
									<a href="/assets/{asset.asset_id}" class="block rounded-lg border border-border bg-card p-2 text-xs hover:bg-muted transition-colors">
										<span class="font-medium text-foreground">{asset.name}</span>
										<span class="text-muted-foreground ml-1">({asset.category})</span>
										{#if asset.value}<span class="text-muted-foreground ml-1">${asset.value.toLocaleString()}</span>{/if}
									</a>
								{/each}
							</div>
						{:else if act === 'create_case' && d?.case_id}
							<ConfirmationCard title="Case created" link={{ href: `/cases/${d.case_id}`, label: 'Open case' }} />
						{:else if act === 'escalate_alert' && d?.case_id}
							<ConfirmationCard title="Alert escalated to case" variant="warning" link={{ href: `/cases/${d.case_id}`, label: 'Open case' }} />
						{:else if act === 'create_rule'}
							<ConfirmationCard title="Rule created: {d?.name || 'Detection rule'}" link={{ href: '/rules', label: 'View rules' }} />
						{:else if act === 'navigate_to_page'}
							<ConfirmationCard title="Navigated to {d?.label || d?.page || 'page'}" variant="info" />
						{:else if act === 'link_node_case' || act === 'create_connector' || act === 'add_observable'}
							<ConfirmationCard title="{act.replace(/_/g, ' ')}" description={d?.message || 'Action completed'} />
						{:else if act === 'list_rules' || act === 'suggest_rules'}
							<div class="space-y-1">
								{#each (d?.rules || []).slice(0, 5) as rule}
									<div class="rounded-lg border border-border bg-card p-2 text-xs">
										<span class="font-medium text-foreground">{rule.name || rule.rule_name || 'Rule'}</span>
									</div>
								{/each}
							</div>
						{:else if act === 'investigation_status' || act === 'list_investigations'}
							{#if (d?.investigations || []).length}
								<div class="space-y-1">
									{#each d.investigations.slice(0, 6) as inv}
										<div class="rounded-lg border border-border bg-card p-2 text-xs">
											<div class="flex items-center justify-between gap-2">
												<span class="font-medium text-foreground truncate">{inv.name}</span>
												<span class="text-[10px] px-1.5 py-0.5 rounded-full shrink-0 {inv.status === 'complete' ? 'bg-emerald-500/15 text-emerald-400' : inv.status === 'failed' ? 'bg-red-500/15 text-red-400' : 'bg-amber-500/15 text-amber-400'}">{inv.status}</span>
											</div>
											<div class="text-[10px] text-muted-foreground mt-0.5">
												{inv.sources_done} source{inv.sources_done === 1 ? '' : 's'} done · {inv.findings_count} finding{inv.findings_count === 1 ? '' : 's'}{#if inv.entities_discovered} · {inv.entities_discovered} discovered{/if}
											</div>
										</div>
									{/each}
								</div>
							{:else}
								<div class="text-xs text-muted-foreground">No investigations yet.</div>
							{/if}
						{:else if act === 'setup_help' && d?.setup}
							<div class="rounded-lg border border-primary/30 bg-primary/5 p-3 text-xs space-y-2">
								<div class="font-medium text-foreground">{d.setup.feature}</div>
								<div class="text-muted-foreground">{d.setup.needs}</div>
								<ol class="list-decimal ml-4 space-y-0.5 text-muted-foreground">
									{#each d.setup.steps as step}<li>{step}</li>{/each}
								</ol>
								<div class="flex gap-2 pt-1">
									<a href={d.setup.register_url} target="_blank" rel="noopener" class="px-2 py-1 rounded bg-muted text-foreground hover:bg-accent">Register ↗</a>
									<a href={d.setup.settings_link} class="px-2 py-1 rounded bg-primary text-primary-foreground hover:bg-primary/90">Open Settings →</a>
								</div>
							</div>
						{:else if (act === 'entity_lookup' || act === 'library_search' || act === 'fact_search' || act === 'event_search' || act === 'reference_search' || act === 'address_resolve' || act === 'recent_facts') && d?.cards?.length}
							<div class="space-y-2">
								{#each d.cards.slice(0, 6) as card}
									{#if card.type === 'EntityCard'}<EntityCard entity_id={card.data.entity_id} entity={card.data.entity} size="compact" />
									{:else if card.type === 'FactCard'}<FactCard assessment_id={card.data.assessment_id} assessment={card.data.assessment} size="compact" />
									{:else if card.type === 'EventCard'}<EventCard event_id={card.data.event_id} event={card.data.event} size="compact" />
									{:else if card.type === 'RefCard'}<RefCard reference_id={card.data.reference_id} reference={card.data.reference} size="compact" />
									{:else if card.type === 'LibraryCard'}<LibraryCard library_id={card.data.library_id} library={card.data.library} size="compact" />
									{/if}
								{/each}
							</div>
						{:else if act === 'docs_search'}
							{#if (d?.passages || []).length}
								<div class="text-[10px] text-muted-foreground/70">from product docs: {d.passages.slice(0, 3).map((p: any) => p.title).join(' · ')}</div>
							{/if}
						{:else if d}
							<div class="rounded-lg border border-border bg-muted/30 p-2 text-xs">
								<span class="text-muted-foreground font-medium">{act}</span>
								<pre class="mt-1 text-[10px] text-muted-foreground overflow-x-auto max-h-20">{JSON.stringify(d, null, 2).slice(0, 300)}</pre>
							</div>
						{/if}
					{/each}
				</div>
			{/if}
		</div>
	</div>
{/each}

{#if sending}
	<div class="flex gap-2.5">
		<div class="w-7 h-7 rounded-full bg-muted flex items-center justify-center shrink-0">
			<Bot size={14} class="text-muted-foreground" />
		</div>
		<div class="flex items-center gap-1 py-2">
			<span class="w-2 h-2 rounded-full bg-muted-foreground/50 animate-bounce" style="animation-delay: 0ms"></span>
			<span class="w-2 h-2 rounded-full bg-muted-foreground/50 animate-bounce" style="animation-delay: 150ms"></span>
			<span class="w-2 h-2 rounded-full bg-muted-foreground/50 animate-bounce" style="animation-delay: 300ms"></span>
		</div>
	</div>
{/if}
