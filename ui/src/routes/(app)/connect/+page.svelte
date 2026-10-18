<script lang="ts">
	/**
	 * AI Connect Wizard — guided device/source onboarding.
	 *
	 * Split-pane layout:
	 *   Left:  Conversational chat with the wizard AI
	 *   Right: Live preview (analysis card, code block, card preview, status)
	 *
	 * The AI guides users through connecting ANY device or data source
	 * to Enki through natural conversation.
	 */
	import { onMount } from 'svelte';
	import { api } from '$lib/api/mock';
	import SignalCard from '$lib/components/cards/SignalCard.svelte';

	import Wand2 from 'lucide-svelte/icons/wand-2';
	import Send from 'lucide-svelte/icons/send';
	import Copy from 'lucide-svelte/icons/copy';
	import Check from 'lucide-svelte/icons/check';
	import Wifi from 'lucide-svelte/icons/wifi';
	import Camera from 'lucide-svelte/icons/camera';
	import Thermometer from 'lucide-svelte/icons/thermometer';
	import Radio from 'lucide-svelte/icons/radio';
	import Globe from 'lucide-svelte/icons/globe';
	import Zap from 'lucide-svelte/icons/zap';
	import ShieldCheck from 'lucide-svelte/icons/shield-check';
	import ArrowRight from 'lucide-svelte/icons/arrow-right';

	interface Message {
		role: 'user' | 'assistant';
		content: string;
		action_results?: any[];
	}

	let messages = $state<Message[]>([]);
	let input = $state('');
	let sending = $state(false);
	let chatEl: HTMLDivElement;
	let copied = $state(false);

	// Preview state — derived from latest action results
	let latestAnalysis = $state<any>(null);
	let latestCode = $state<any>(null);
	let latestCardPreview = $state<any>(null);
	let latestRules = $state<any>(null);
	let latestConnector = $state<any>(null);

	const quickStarts = [
		{ label: 'Temperature sensor', prompt: 'I have a Raspberry Pi with a DHT22 temperature and humidity sensor', icon: Thermometer },
		{ label: 'Security camera', prompt: 'I have a Hikvision camera that sends motion alerts via HTTP', icon: Camera },
		{ label: 'Weather data', prompt: 'I want to pull weather data for my location every hour', icon: Globe },
		{ label: 'WiFi scanner', prompt: 'I have a script that scans nearby WiFi networks', icon: Wifi },
		{ label: 'SDR radio', prompt: 'I have an RTL-SDR receiving ADS-B aircraft data', icon: Radio },
		{ label: 'Custom API', prompt: 'I want to pull data from a REST API every 5 minutes', icon: Zap },
	];

	async function sendMessage(text?: string) {
		const msg = text || input.trim();
		if (!msg || sending) return;
		input = '';
		sending = true;

		messages = [...messages, { role: 'user', content: msg }];
		scrollToBottom();

		try {
			// Build conversation history for context
			const history = messages.slice(0, -1).map(m => ({
				role: m.role,
				content: m.content,
			}));

			const result: any = await api.connectWizard(msg, history);

			const assistantMsg: Message = {
				role: 'assistant',
				content: result.response || 'I encountered an issue. Please try again.',
				action_results: result.action_results || [],
			};
			messages = [...messages, assistantMsg];

			// Update preview panes from action results
			for (const ar of assistantMsg.action_results || []) {
				if (!ar.success) continue;
				switch (ar.action) {
					case 'analyze_source':
						latestAnalysis = ar.data;
						break;
					case 'generate_code':
						latestCode = ar.data;
						break;
					case 'preview_card':
						latestCardPreview = ar.data;
						break;
					case 'suggest_rules':
						latestRules = ar.data;
						break;
					case 'create_connector':
						latestConnector = ar.data;
						break;
				}
			}
		} catch (e: any) {
			messages = [...messages, {
				role: 'assistant',
				content: 'Connection error. Make sure your AI provider is configured in Settings.',
			}];
		}

		sending = false;
		scrollToBottom();
	}

	function scrollToBottom() {
		requestAnimationFrame(() => {
			if (chatEl) chatEl.scrollTop = chatEl.scrollHeight;
		});
	}

	async function copyCode() {
		if (!latestCode?.code) return;
		await navigator.clipboard.writeText(latestCode.code);
		copied = true;
		setTimeout(() => copied = false, 2000);
	}

	async function createRule(rule: any) {
		try {
			await api.chat(`Create a detection rule named "${rule.name}" of type ${rule.rule_type} with severity ${rule.severity}`);
		} catch {}
	}

	// Language display names
	const langNames: Record<string, string> = {
		bash: 'Bash', python: 'Python', javascript: 'Node.js',
		json: 'JSON', text: 'Config',
	};
</script>

<svelte:head>
	<title>Connect Device - Enki</title>
</svelte:head>

<div class="h-full flex flex-col lg:flex-row gap-0 overflow-hidden">
	<!-- LEFT: Chat pane -->
	<div class="flex-1 flex flex-col min-h-0 lg:border-r border-border">
		<!-- Header -->
		<div class="px-4 py-3 border-b border-border flex items-center gap-3">
			<div class="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center">
				<Wand2 class="w-4 h-4 text-primary" />
			</div>
			<div>
				<h1 class="text-sm font-semibold text-foreground">Connect a Device</h1>
				<p class="text-[10px] text-muted-foreground">Tell me what you want to connect and I'll generate the integration</p>
			</div>
		</div>

		<!-- Messages -->
		<div bind:this={chatEl} class="flex-1 overflow-y-auto px-4 py-4 space-y-4">
			{#if messages.length === 0}
				<!-- Welcome + quick starts -->
				<div class="text-center py-8">
					<div class="w-12 h-12 rounded-2xl bg-primary/10 flex items-center justify-center mx-auto mb-3">
						<Wand2 class="w-6 h-6 text-primary" />
					</div>
					<h2 class="text-lg font-semibold text-foreground mb-1">What are you connecting?</h2>
					<p class="text-xs text-muted-foreground mb-6 max-w-sm mx-auto">
						Describe your device, sensor, camera, API, or data source. I'll generate the exact code to get data flowing.
					</p>
					<div class="grid grid-cols-2 sm:grid-cols-3 gap-2 max-w-lg mx-auto">
						{#each quickStarts as qs}
							<button
								onclick={() => sendMessage(qs.prompt)}
								class="flex flex-col items-center gap-1.5 p-3 rounded-xl border border-border hover:border-primary/30 hover:bg-primary/5 transition-colors text-center"
							>
								<svelte:component this={qs.icon} class="w-5 h-5 text-primary" />
								<span class="text-[11px] text-muted-foreground">{qs.label}</span>
							</button>
						{/each}
					</div>
				</div>
			{:else}
				{#each messages as msg}
					<div class="flex gap-3 {msg.role === 'user' ? 'justify-end' : ''}">
						{#if msg.role === 'assistant'}
							<div class="w-7 h-7 rounded-lg bg-primary/10 flex items-center justify-center shrink-0 mt-0.5">
								<Wand2 class="w-3.5 h-3.5 text-primary" />
							</div>
						{/if}
						<div class="max-w-[80%] {msg.role === 'user'
							? 'bg-primary text-primary-foreground rounded-2xl rounded-br-md px-4 py-2'
							: 'bg-muted rounded-2xl rounded-bl-md px-4 py-2'}">
							<p class="text-sm whitespace-pre-wrap">{msg.content}</p>
							{#if msg.action_results?.length}
								<div class="mt-2 flex flex-wrap gap-1">
									{#each msg.action_results as ar}
										<span class="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full {ar.success ? 'bg-enki-green/10 text-enki-green' : 'bg-destructive/10 text-destructive'}">
											{#if ar.success}<ShieldCheck class="w-2.5 h-2.5" />{/if}
											{ar.action}
										</span>
									{/each}
								</div>
							{/if}
						</div>
					</div>
				{/each}
				{#if sending}
					<div class="flex gap-3">
						<div class="w-7 h-7 rounded-lg bg-primary/10 flex items-center justify-center shrink-0">
							<Wand2 class="w-3.5 h-3.5 text-primary animate-pulse" />
						</div>
						<div class="bg-muted rounded-2xl rounded-bl-md px-4 py-3">
							<div class="flex gap-1">
								<div class="w-1.5 h-1.5 rounded-full bg-muted-foreground/40 animate-bounce" style="animation-delay: 0ms"></div>
								<div class="w-1.5 h-1.5 rounded-full bg-muted-foreground/40 animate-bounce" style="animation-delay: 150ms"></div>
								<div class="w-1.5 h-1.5 rounded-full bg-muted-foreground/40 animate-bounce" style="animation-delay: 300ms"></div>
							</div>
						</div>
					</div>
				{/if}
			{/if}
		</div>

		<!-- Input -->
		<div class="px-4 py-3 border-t border-border">
			<form onsubmit={(e) => { e.preventDefault(); sendMessage(); }} class="flex items-center gap-2">
				<input
					bind:value={input}
					type="text"
					placeholder="Describe your device or data source..."
					class="flex-1 bg-muted rounded-xl px-4 py-2.5 text-sm text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary/30"
					disabled={sending}
				/>
				<button
					type="submit"
					disabled={!input.trim() || sending}
					class="w-9 h-9 rounded-xl flex items-center justify-center transition-colors {input.trim() ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}"
				>
					<Send class="w-4 h-4" />
				</button>
			</form>
		</div>
	</div>

	<!-- RIGHT: Preview pane -->
	<div class="flex-1 flex flex-col min-h-0 overflow-y-auto bg-muted/30">
		<div class="px-4 py-3 border-b border-border">
			<h2 class="text-xs font-medium text-muted-foreground uppercase tracking-wide">Live Preview</h2>
		</div>

		<div class="p-4 space-y-4 flex-1">
			{#if !latestAnalysis && !latestCode && !latestCardPreview && !latestRules && !latestConnector}
				<!-- Empty state -->
				<div class="text-center py-16 text-muted-foreground/50">
					<Zap class="w-8 h-8 mx-auto mb-2 opacity-30" />
					<p class="text-xs">Describe your device and the preview will appear here</p>
				</div>
			{/if}

			<!-- Analysis card -->
			{#if latestAnalysis}
				<div class="rounded-xl border border-border bg-card p-4 space-y-3">
					<h3 class="text-xs font-medium text-muted-foreground uppercase tracking-wide">Source Analysis</h3>
					<div class="grid grid-cols-2 gap-3">
						<div>
							<span class="text-[10px] text-muted-foreground/60">Device Type</span>
							<p class="text-sm font-medium text-foreground">{latestAnalysis.device_type}</p>
						</div>
						<div>
							<span class="text-[10px] text-muted-foreground/60">Data Format</span>
							<p class="text-sm font-medium text-foreground">{latestAnalysis.data_format}</p>
						</div>
						<div>
							<span class="text-[10px] text-muted-foreground/60">Connection Method</span>
							<p class="text-sm font-medium text-primary">{latestAnalysis.connection_method}</p>
						</div>
						{#if latestAnalysis.predicted_signal_type}
							<div>
								<span class="text-[10px] text-muted-foreground/60">Signal Type</span>
								<p class="text-sm font-mono text-foreground">{latestAnalysis.predicted_signal_type}</p>
							</div>
						{/if}
						{#if latestAnalysis.recommended_recipe}
							<div class="col-span-2">
								<span class="text-[10px] text-muted-foreground/60">Recommended Recipe</span>
								<p class="text-sm font-medium text-enki-green">{latestAnalysis.recommended_recipe}</p>
							</div>
						{/if}
					</div>
				</div>
			{/if}

			<!-- Generated code -->
			{#if latestCode && !latestCode.error}
				<div class="rounded-xl border border-border bg-card overflow-hidden">
					<div class="flex items-center justify-between px-4 py-2 border-b border-border">
						<div class="flex items-center gap-2">
							<span class="text-xs font-medium text-foreground">{latestCode.filename}</span>
							<span class="text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary">{langNames[latestCode.language] || latestCode.language}</span>
						</div>
						<button
							onclick={copyCode}
							class="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground transition-colors"
						>
							{#if copied}
								<Check class="w-3 h-3 text-enki-green" />
								<span class="text-enki-green">Copied</span>
							{:else}
								<Copy class="w-3 h-3" />
								<span>Copy</span>
							{/if}
						</button>
					</div>
					<pre class="p-4 overflow-x-auto text-[11px] font-mono leading-relaxed text-foreground/90 max-h-80">{latestCode.code}</pre>
					<div class="px-4 py-2 border-t border-border bg-muted/30">
						<p class="text-[10px] text-muted-foreground">{latestCode.description}</p>
					</div>
				</div>
			{/if}

			<!-- Card preview -->
			{#if latestCardPreview?.card_recipe}
				<div class="rounded-xl border border-border bg-card p-4 space-y-2">
					<h3 class="text-xs font-medium text-muted-foreground uppercase tracking-wide">Card Preview</h3>
					<p class="text-[10px] text-muted-foreground mb-2">This is how your data will appear in the Feed</p>
					<SignalCard
						signal={{
							signal_type: latestCardPreview.signal_type,
							timestamp: new Date().toISOString(),
							severity: 'informational',
							metadata: { card_recipe: latestCardPreview.card_recipe },
							payload: latestCardPreview.card_recipe._sample || {},
							observables: [],
						}}
					/>
				</div>
			{/if}

			<!-- Suggested rules -->
			{#if latestRules?.suggestions?.length}
				<div class="rounded-xl border border-border bg-card p-4 space-y-3">
					<h3 class="text-xs font-medium text-muted-foreground uppercase tracking-wide">Suggested Detection Rules</h3>
					{#each latestRules.suggestions as rule}
						<div class="flex items-center justify-between p-3 rounded-lg bg-muted/50 border border-border">
							<div>
								<p class="text-sm font-medium text-foreground">{rule.name}</p>
								<p class="text-[10px] text-muted-foreground">{rule.description || `${rule.rule_type} rule · ${rule.severity}`}</p>
							</div>
							<button
								onclick={() => createRule(rule)}
								class="flex items-center gap-1 text-[10px] px-3 py-1.5 rounded-lg bg-primary/10 text-primary hover:bg-primary/20 transition-colors"
							>
								Enable <ArrowRight class="w-3 h-3" />
							</button>
						</div>
					{/each}
				</div>
			{/if}

			<!-- Connector created -->
			{#if latestConnector && !latestConnector.error}
				<div class="rounded-xl border border-enki-green/30 bg-enki-green/5 p-4 space-y-2">
					<div class="flex items-center gap-2">
						<ShieldCheck class="w-5 h-5 text-enki-green" />
						<h3 class="text-sm font-medium text-enki-green">Connector Created</h3>
					</div>
					<p class="text-xs text-muted-foreground">
						Your connector is active and will begin polling. View it on the
						<a href="/connectors" class="text-primary hover:underline">Connectors page</a>.
					</p>
				</div>
			{/if}
		</div>
	</div>
</div>
