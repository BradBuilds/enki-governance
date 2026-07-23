<script lang="ts">
	import { onMount } from 'svelte';
	import { api } from '$lib/api/mock';
	import Badge from '$lib/components/shared/Badge.svelte';
	import LoadingSkeleton from '$lib/components/shared/LoadingSkeleton.svelte';
	import Building from 'lucide-svelte/icons/building';
	import User from 'lucide-svelte/icons/user';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import FileText from 'lucide-svelte/icons/file-text';
	import DollarSign from 'lucide-svelte/icons/dollar-sign';
	import Scale from 'lucide-svelte/icons/scale';
	import Plane from 'lucide-svelte/icons/plane';
	import Globe from 'lucide-svelte/icons/globe';
	import Shield from 'lucide-svelte/icons/shield';
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import Plus from 'lucide-svelte/icons/plus';

	interface Props {
		entityType: 'company' | 'person' | 'address';
		entityValue: string;
		caseId?: string;
		onClose?: () => void;
	}

	let { entityType, entityValue, caseId, onClose }: Props = $props();

	let loading = $state(true);
	let result = $state<any>(null);
	let error = $state<string | null>(null);
	let activeTab = $state('overview');

	onMount(async () => {
		try {
			const data = await api.resolveEntity(entityType, entityValue, caseId);
			result = data?.result;
		} catch (e: any) {
			error = e?.message || 'Resolution failed';
		}
		loading = false;
	});

	function formatCurrency(val: any): string {
		if (!val) return '—';
		const n = Number(val);
		if (n >= 1e9) return `$${(n / 1e9).toFixed(1)}B`;
		if (n >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
		if (n >= 1e3) return `$${(n / 1e3).toFixed(0)}K`;
		return `$${n.toLocaleString()}`;
	}

	const typeIcon = $derived(entityType === 'company' ? Building : entityType === 'person' ? User : MapPin);

	const tabs = $derived.by(() => {
		if (!result) return [];
		const t = [{ id: 'overview', label: 'Overview' }];
		if (result.sec?.name || result.financials?.revenue) t.push({ id: 'sec', label: 'SEC Filings' });
		if (result.contracts?.award_count > 0) t.push({ id: 'contracts', label: 'Contracts' });
		if (result.legal?.length > 0 || result.legal?.length > 0) t.push({ id: 'legal', label: `Legal (${(result.legal || []).length})` });
		if (result.aircraft?.length > 0 || result.assets?.length > 0) t.push({ id: 'assets', label: 'Assets' });
		if (result.finra?.length > 0) t.push({ id: 'finra', label: 'FINRA' });
		if (result.companies?.length > 0) t.push({ id: 'entities', label: `Entities (${result.companies.length})` });
		return t;
	});
</script>

<div class="rounded-xl border border-border bg-card overflow-hidden">
	<!-- Header -->
	<div class="px-4 py-3 border-b border-border bg-muted/30">
		<div class="flex items-center gap-3">
			<div class="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center shrink-0">
				<svelte:component this={typeIcon} size={20} class="text-primary" />
			</div>
			<div class="flex-1 min-w-0">
				<h3 class="text-sm font-medium text-foreground truncate">{entityValue}</h3>
				<p class="text-xs text-muted-foreground capitalize">{entityType} Investigation</p>
			</div>
			{#if caseId}
				<Badge text="Case linked" variant="success" size="sm" />
			{/if}
		</div>
	</div>

	{#if loading}
		<div class="p-4">
			<LoadingSkeleton type="card" />
			<p class="text-xs text-muted-foreground text-center mt-2 animate-pulse">Querying SEC, GLEIF, CourtListener, FINRA, USAspending...</p>
		</div>
	{:else if error}
		<div class="p-4 text-sm text-destructive">{error}</div>
	{:else if result}
		<!-- Tabs -->
		{#if tabs.length > 1}
			<div class="flex border-b border-border overflow-x-auto">
				{#each tabs as tab}
					<button
						onclick={() => activeTab = tab.id}
						class="px-3 py-2 text-xs font-medium whitespace-nowrap border-b-2 -mb-px transition-colors
							{activeTab === tab.id ? 'text-primary border-primary' : 'text-muted-foreground border-transparent hover:text-foreground'}"
					>{tab.label}</button>
				{/each}
			</div>
		{/if}

		<div class="p-4 space-y-3 max-h-[500px] overflow-y-auto">
			{#if activeTab === 'overview'}
				<!-- SEC Summary -->
				{#if result.sec?.name}
					<div class="flex items-start gap-2">
						<FileText size={14} class="text-muted-foreground mt-0.5 shrink-0" />
						<div class="flex-1">
							<p class="text-xs font-medium text-foreground">{result.sec.name}</p>
							<p class="text-[10px] text-muted-foreground">
								{result.sec.sic_description || ''} · {result.sec.state || ''} · CIK {result.sec.cik}
								{#if result.sec.tickers?.length} · {result.sec.tickers.join(', ')}{/if}
							</p>
						</div>
					</div>
				{/if}

				<!-- Financials -->
				{#if result.financials?.revenue}
					<div class="flex items-start gap-2">
						<DollarSign size={14} class="text-emerald-400 mt-0.5 shrink-0" />
						<div class="grid grid-cols-2 gap-2 flex-1 text-xs">
							<div><span class="text-muted-foreground">Revenue:</span> <span class="text-foreground font-medium">{formatCurrency(result.financials.revenue)}</span></div>
							{#if result.financials.total_assets}<div><span class="text-muted-foreground">Assets:</span> <span class="text-foreground">{formatCurrency(result.financials.total_assets)}</span></div>{/if}
							{#if result.financials.net_income}<div><span class="text-muted-foreground">Net Income:</span> <span class="text-foreground">{formatCurrency(result.financials.net_income)}</span></div>{/if}
							{#if result.financials.equity}<div><span class="text-muted-foreground">Equity:</span> <span class="text-foreground">{formatCurrency(result.financials.equity)}</span></div>{/if}
						</div>
					</div>
				{/if}

				<!-- GLEIF -->
				{#if result.gleif?.lei}
					<div class="flex items-start gap-2">
						<Globe size={14} class="text-blue-400 mt-0.5 shrink-0" />
						<div class="text-xs">
							<p class="text-foreground">LEI: <span class="font-mono text-[10px]">{result.gleif.lei}</span></p>
							<p class="text-muted-foreground">{result.gleif.jurisdiction} · {result.gleif.status} · {result.gleif.country}</p>
						</div>
					</div>
				{/if}

				<!-- Contracts summary -->
				{#if result.contracts?.award_count > 0}
					<div class="flex items-start gap-2">
						<Shield size={14} class="text-yellow-400 mt-0.5 shrink-0" />
						<div class="text-xs">
							<p class="text-foreground">{result.contracts.award_count} federal awards · {formatCurrency(result.contracts.total_amount)}</p>
						</div>
					</div>
				{/if}

				<!-- Legal summary -->
				{#if result.legal?.length > 0}
					<div class="flex items-start gap-2">
						<Scale size={14} class="text-red-400 mt-0.5 shrink-0" />
						<p class="text-xs text-foreground">{result.legal.length} federal court case{result.legal.length > 1 ? 's' : ''}</p>
					</div>
				{/if}

				<!-- Nonprofit -->
				{#if result.nonprofit?.ein}
					<div class="flex items-start gap-2">
						<Building size={14} class="text-purple-400 mt-0.5 shrink-0" />
						<div class="text-xs">
							<p class="text-foreground">Tax-exempt · EIN: {result.nonprofit.ein}</p>
							{#if result.nonprofit.income_amount}<p class="text-muted-foreground">Income: {formatCurrency(result.nonprofit.income_amount)}</p>{/if}
						</div>
					</div>
				{/if}

				<!-- FINRA (person) -->
				{#if result.finra?.length > 0}
					{#each result.finra as broker}
						<div class="flex items-start gap-2">
							<Shield size={14} class="text-yellow-400 mt-0.5 shrink-0" />
							<div class="text-xs">
								<p class="text-foreground">{broker.name} · CRD #{broker.crd}</p>
								<p class="text-muted-foreground">{broker.firm} · {broker.disclosures} disclosures · {broker.licenses} exams</p>
							</div>
						</div>
					{/each}
				{/if}

			{:else if activeTab === 'sec'}
				{#if result.sec}
					<div class="space-y-2 text-xs">
						<div class="flex justify-between"><span class="text-muted-foreground">Entity</span><span class="text-foreground">{result.sec.name}</span></div>
						<div class="flex justify-between"><span class="text-muted-foreground">CIK</span><span class="text-foreground font-mono">{result.sec.cik}</span></div>
						<div class="flex justify-between"><span class="text-muted-foreground">SIC</span><span class="text-foreground">{result.sec.sic_description} ({result.sec.sic})</span></div>
						<div class="flex justify-between"><span class="text-muted-foreground">State</span><span class="text-foreground">{result.sec.state}</span></div>
						<div class="flex justify-between"><span class="text-muted-foreground">Fiscal Year End</span><span class="text-foreground">{result.sec.fiscal_year_end}</span></div>
						<div class="flex justify-between"><span class="text-muted-foreground">Total Filings</span><span class="text-foreground">{result.sec.filing_count}</span></div>
						{#if result.sec.tickers?.length}
							<div class="flex justify-between"><span class="text-muted-foreground">Tickers</span><span class="text-foreground">{result.sec.tickers.join(', ')}</span></div>
						{/if}
						<a href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={result.sec.cik}" target="_blank" rel="noopener"
							class="flex items-center gap-1 text-primary hover:underline mt-2">
							<ExternalLink size={12} /> View on SEC EDGAR
						</a>
					</div>
				{/if}

			{:else if activeTab === 'contracts'}
				{#if result.contracts?.awards}
					<p class="text-xs text-muted-foreground mb-2">{result.contracts.award_count} awards · Total: {formatCurrency(result.contracts.total_amount)}</p>
					{#each result.contracts.awards as award}
						<div class="p-2 rounded-lg border border-border/50 mb-2">
							<div class="flex justify-between text-xs mb-1">
								<span class="text-foreground font-medium">{formatCurrency(award.amount)}</span>
								<span class="text-muted-foreground">{award.start_date}</span>
							</div>
							<p class="text-[10px] text-muted-foreground">{award.agency} · {award.type}</p>
							<p class="text-[10px] text-muted-foreground font-mono">{award.id}</p>
						</div>
					{/each}
				{/if}

			{:else if activeTab === 'legal'}
				{#each (result.legal || []) as case_item}
					<div class="p-2 rounded-lg border border-border/50 mb-2">
						<p class="text-xs text-foreground font-medium">{case_item.case_name}</p>
						<p class="text-[10px] text-muted-foreground">{case_item.court} · Filed: {case_item.date_filed}</p>
						{#if case_item.docket_number}<p class="text-[10px] font-mono text-muted-foreground">{case_item.docket_number}</p>{/if}
					</div>
				{/each}

			{:else if activeTab === 'assets'}
				{#each (result.aircraft || result.assets || []) as asset}
					<div class="flex items-center gap-2 p-2 rounded-lg border border-border/50 mb-2">
						<Plane size={14} class="text-blue-400 shrink-0" />
						<div class="text-xs">
							<p class="text-foreground font-medium">{asset.n_number || asset.name}</p>
							<p class="text-muted-foreground">{asset.model || ''} · {asset.registrant || ''}</p>
						</div>
					</div>
				{/each}

			{:else if activeTab === 'finra'}
				{#each (result.finra || []) as broker}
					<div class="p-2 rounded-lg border border-border/50 mb-2">
						<p class="text-xs text-foreground font-medium">{broker.name}</p>
						<p class="text-[10px] text-muted-foreground">CRD #{broker.crd} · {broker.firm}</p>
						<p class="text-[10px] text-muted-foreground">{broker.disclosures} disclosures · {broker.licenses} exams passed</p>
					</div>
				{/each}

			{:else if activeTab === 'entities'}
				{#each (result.companies || []) as company}
					<div class="p-3 rounded-lg border border-border mb-2">
						<div class="flex items-center gap-2 mb-2">
							<Building size={14} class="text-primary shrink-0" />
							<span class="text-sm font-medium text-foreground">{company.name}</span>
						</div>
						{#if company.financials?.revenue}
							<p class="text-xs text-muted-foreground">Revenue: {formatCurrency(company.financials.revenue)}</p>
						{/if}
						{#if company.government_contracts?.total_amount}
							<p class="text-xs text-muted-foreground">Gov contracts: {formatCurrency(company.government_contracts.total_amount)}</p>
						{/if}
						{#if company.gleif?.lei}
							<p class="text-[10px] text-muted-foreground font-mono">LEI: {company.gleif.lei}</p>
						{/if}
						{#if company.legal_cases?.length}
							<p class="text-xs text-red-400">{company.legal_cases.length} court cases</p>
						{/if}
					</div>
				{/each}
			{/if}
		</div>
	{/if}
</div>
