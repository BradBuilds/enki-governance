<script lang="ts">
	import AskEnkiButton from '$lib/components/shared/AskEnkiButton.svelte';
	import Badge from '$lib/components/shared/Badge.svelte';
	import Upload from 'lucide-svelte/icons/upload';
	import FileText from 'lucide-svelte/icons/file-text';
	import Check from 'lucide-svelte/icons/check';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
	import ChevronRight from 'lucide-svelte/icons/chevron-right';
	import ChevronLeft from 'lucide-svelte/icons/chevron-left';
	import Loader from 'lucide-svelte/icons/loader-circle';
	import Contact from 'lucide-svelte/icons/contact';
	import PackageIcon from 'lucide-svelte/icons/package';
	import Eye from 'lucide-svelte/icons/eye';

	let step = $state(1);
	let entityType = $state<'contacts' | 'assets' | 'observables'>('contacts');
	let toast = $state<string | null>(null);

	// File state
	let file = $state<File | null>(null);
	let rawText = $state('');
	let headers = $state<string[]>([]);
	let rows = $state<string[][]>([]);
	let parseError = $state<string | null>(null);

	// Mapping state
	let columnMap = $state<Record<string, string>>({});

	// Import state
	let importing = $state(false);
	let importResult = $state<any>(null);

	// Target fields per entity type
	const fieldOptions: Record<string, { value: string; label: string }[]> = {
		contacts: [
			{ value: '', label: '— Skip —' },
			{ value: 'primary_name', label: 'Name' },
			{ value: 'email', label: 'Email' },
			{ value: 'phone', label: 'Phone' },
			{ value: 'contact_type', label: 'Type (person/company)' },
			{ value: 'status', label: 'Status' },
			{ value: 'threat_level', label: 'Threat Level' },
			{ value: 'tags', label: 'Tags (comma-separated)' },
			{ value: 'notes', label: 'Notes' },
			{ value: 'role_title', label: 'Role / Title' },
			{ value: 'company_name', label: 'Company Name' },
			{ value: 'address', label: 'Address' },
		],
		assets: [
			{ value: '', label: '— Skip —' },
			{ value: 'display_name', label: 'Name' },
			{ value: 'asset_category', label: 'Category' },
			{ value: 'status', label: 'Status' },
			{ value: 'estimated_value', label: 'Estimated Value' },
			{ value: 'serial_number', label: 'Serial Number' },
			{ value: 'description', label: 'Description' },
			{ value: 'tags', label: 'Tags (comma-separated)' },
		],
		observables: [
			{ value: '', label: '— Skip —' },
			{ value: 'observable_type', label: 'Observable Type' },
			{ value: 'observable_value', label: 'Observable Value' },
			{ value: 'notes', label: 'Notes' },
		],
	};

	function showToast(msg: string) { toast = msg; setTimeout(() => toast = null, 3000); }

	// Parse CSV
	function parseCSV(text: string) {
		const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
		if (lines.length < 2) { parseError = 'File needs at least a header row and one data row'; return; }

		const delimiter = lines[0].includes('\t') ? '\t' : ',';
		headers = lines[0].split(delimiter).map(h => h.replace(/^"(.*)"$/, '$1').trim());
		rows = lines.slice(1).map(line => {
			// Basic CSV parsing (handles quoted fields)
			const cells: string[] = [];
			let current = '';
			let inQuotes = false;
			for (const char of line) {
				if (char === '"') { inQuotes = !inQuotes; }
				else if ((char === delimiter) && !inQuotes) { cells.push(current.trim()); current = ''; }
				else { current += char; }
			}
			cells.push(current.trim());
			return cells;
		});

		// Auto-map columns by name matching
		columnMap = {};
		const fields = fieldOptions[entityType] || [];
		for (let i = 0; i < headers.length; i++) {
			const h = headers[i].toLowerCase().replace(/[\s_-]+/g, '_');
			const match = fields.find(f => f.value && f.value.toLowerCase() === h);
			if (match) columnMap[headers[i]] = match.value;
			else if (h.includes('name') && entityType === 'contacts') columnMap[headers[i]] = 'primary_name';
			else if (h.includes('email')) columnMap[headers[i]] = 'email';
			else if (h.includes('phone')) columnMap[headers[i]] = 'phone';
			else columnMap[headers[i]] = '';
		}
		parseError = null;
	}

	// Handle file drop/select
	function handleFile(f: File) {
		file = f;
		const reader = new FileReader();
		reader.onload = (e) => {
			rawText = e.target?.result as string || '';
			parseCSV(rawText);
			step = 2;
		};
		reader.readAsText(f);
	}

	function onDrop(e: DragEvent) {
		e.preventDefault();
		const f = e.dataTransfer?.files[0];
		if (f) handleFile(f);
	}

	function onFileSelect(e: Event) {
		const f = (e.target as HTMLInputElement).files?.[0];
		if (f) handleFile(f);
	}

	// Build mapped records
	let mappedRecords = $derived.by(() => {
		return rows.map(row => {
			const record: Record<string, any> = {};
			headers.forEach((h, i) => {
				const field = columnMap[h];
				if (field && row[i]) {
					if (field === 'tags') record[field] = row[i].split(',').map(t => t.trim());
					else if (field === 'estimated_value') record[field] = parseFloat(row[i]) || 0;
					else record[field] = row[i];
				}
			});
			return record;
		}).filter(r => Object.keys(r).length > 0);
	});

	// Validation
	let validationErrors = $derived.by(() => {
		const errors: string[] = [];
		const mapped = Object.values(columnMap).filter(Boolean);
		if (entityType === 'contacts' && !mapped.includes('primary_name')) errors.push('Name column is required');
		if (entityType === 'assets' && !mapped.includes('display_name')) errors.push('Name column is required');
		if (entityType === 'observables' && !mapped.includes('observable_type')) errors.push('Observable type column is required');
		if (entityType === 'observables' && !mapped.includes('observable_value')) errors.push('Observable value column is required');
		if (mappedRecords.length === 0) errors.push('No valid records after mapping');
		return errors;
	});

	// Submit import
	async function submitImport() {
		if (validationErrors.length > 0) return;
		importing = true;
		try {
			const res = await fetch(`/api/imports/${entityType}`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json', 'X-Tenant-ID': 'default' },
				body: JSON.stringify({ records: mappedRecords }),
			});
			if (res.ok) {
				importResult = await res.json();
				step = 4;
				showToast(`Imported ${importResult.imported} ${entityType}`);
			} else {
				const err = await res.json();
				showToast(`Import failed: ${err.detail || 'Unknown error'}`);
			}
		} catch (e: any) {
			showToast(`Import failed: ${e.message}`);
		}
		importing = false;
	}

	function reset() {
		step = 1; file = null; rawText = ''; headers = []; rows = []; columnMap = {}; importResult = null; parseError = null;
	}
</script>

{#if toast}
	<div class="fixed top-4 right-4 z-50 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm shadow-lg">{toast}</div>
{/if}

<div class="max-w-3xl mx-auto space-y-6">
	<div class="flex items-center justify-between">
		<div>
			<h1 class="text-xl font-semibold text-foreground flex items-center gap-2">
				<Upload class="w-5 h-5" /> Import Data
			</h1>
			<p class="text-sm text-muted-foreground mt-0.5">Bulk import contacts, assets, or observables from CSV</p>
		</div>
		<AskEnkiButton prompt="Help me import data from a CSV file" variant="compact" />
	</div>

	<!-- Progress -->
	<div class="flex items-center justify-center gap-2">
		{#each [{ n: 1, l: 'Upload' }, { n: 2, l: 'Map' }, { n: 3, l: 'Review' }, { n: 4, l: 'Done' }] as s}
			<span class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium
				{step === s.n ? 'bg-primary text-primary-foreground' : step > s.n ? 'bg-emerald-500/15 text-emerald-400' : 'bg-muted text-muted-foreground'}">
				{#if step > s.n}<Check class="w-3.5 h-3.5" />{/if}
				{s.l}
			</span>
			{#if s.n < 4}<ChevronRight class="w-4 h-4 text-muted-foreground/30" />{/if}
		{/each}
	</div>

	<!-- Step 1: Upload -->
	{#if step === 1}
		<div class="space-y-4">
			<!-- Entity type selector -->
			<div class="grid grid-cols-3 gap-3">
				{#each [
					{ id: 'contacts' as const, icon: Contact, label: 'Contacts', desc: 'People, companies, entities' },
					{ id: 'assets' as const, icon: PackageIcon, label: 'Assets', desc: 'Vehicles, property, equipment' },
					{ id: 'observables' as const, icon: Eye, label: 'Observables', desc: 'IPs, emails, plates, hashes' },
				] as opt}
					<button onclick={() => entityType = opt.id}
						class="p-4 rounded-lg border text-center transition-colors
							{entityType === opt.id ? 'border-primary bg-primary/5' : 'border-border hover:bg-muted/50'}">
						<svelte:component this={opt.icon} class="w-5 h-5 mx-auto mb-1 {entityType === opt.id ? 'text-primary' : 'text-muted-foreground'}" />
						<div class="text-sm font-medium {entityType === opt.id ? 'text-foreground' : 'text-muted-foreground'}">{opt.label}</div>
						<div class="text-[10px] text-muted-foreground mt-0.5">{opt.desc}</div>
					</button>
				{/each}
			</div>

			<!-- Drop zone -->
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div class="border-2 border-dashed border-border rounded-xl p-12 text-center hover:border-primary/50 transition-colors cursor-pointer"
				ondragover={(e) => e.preventDefault()}
				ondrop={onDrop}
				onclick={() => document.getElementById('csv-input')?.click()}
			>
				<Upload class="w-10 h-10 mx-auto mb-3 text-muted-foreground/40" />
				<p class="text-sm text-foreground font-medium">Drop a CSV file here or click to browse</p>
				<p class="text-xs text-muted-foreground mt-1">Supports .csv and .tsv files</p>
				<input id="csv-input" type="file" accept=".csv,.tsv,.txt" class="hidden" onchange={onFileSelect} />
			</div>
		</div>

	<!-- Step 2: Column Mapping -->
	{:else if step === 2}
		<div class="space-y-4">
			<div class="flex items-center justify-between">
				<div>
					<h2 class="text-sm font-semibold text-foreground">Map Columns</h2>
					<p class="text-xs text-muted-foreground">{file?.name} · {rows.length} rows · {headers.length} columns</p>
				</div>
				<Badge text={entityType} variant="info" size="sm" />
			</div>

			{#if parseError}
				<div class="rounded-lg bg-destructive/10 border border-destructive/20 px-4 py-3 text-xs text-destructive">{parseError}</div>
			{:else}
				<div class="rounded-lg border border-border bg-card divide-y divide-border">
					{#each headers as header, i}
						<div class="flex items-center gap-3 px-4 py-2.5">
							<code class="text-xs font-mono text-foreground w-36 truncate shrink-0">{header}</code>
							<span class="text-[10px] text-muted-foreground w-32 truncate shrink-0">{rows[0]?.[i] || '—'}</span>
							<ChevronRight class="w-3 h-3 text-muted-foreground shrink-0" />
							<select bind:value={columnMap[header]}
								class="flex-1 px-2 py-1 rounded-md border border-border bg-muted text-foreground text-xs">
								{#each fieldOptions[entityType] as opt}
									<option value={opt.value}>{opt.label}</option>
								{/each}
							</select>
						</div>
					{/each}
				</div>

				<!-- Preview -->
				<div class="rounded-lg border border-border bg-card p-4">
					<h3 class="text-xs font-medium text-muted-foreground mb-2">Preview (first 3 records)</h3>
					<div class="space-y-2">
						{#each mappedRecords.slice(0, 3) as record, i}
							<div class="text-[10px] font-mono text-muted-foreground bg-muted rounded p-2 overflow-x-auto">
								{JSON.stringify(record)}
							</div>
						{/each}
					</div>
				</div>
			{/if}
		</div>

	<!-- Step 3: Review & Confirm -->
	{:else if step === 3}
		<div class="space-y-4">
			<h2 class="text-sm font-semibold text-foreground">Review Import</h2>

			<div class="rounded-lg border border-border bg-card divide-y divide-border">
				<div class="px-4 py-3 flex justify-between">
					<span class="text-sm text-muted-foreground">Entity Type</span>
					<span class="text-sm font-medium text-foreground capitalize">{entityType}</span>
				</div>
				<div class="px-4 py-3 flex justify-between">
					<span class="text-sm text-muted-foreground">Records to Import</span>
					<span class="text-sm font-medium text-foreground">{mappedRecords.length}</span>
				</div>
				<div class="px-4 py-3 flex justify-between">
					<span class="text-sm text-muted-foreground">Mapped Fields</span>
					<span class="text-sm font-medium text-foreground">{Object.values(columnMap).filter(Boolean).length} of {headers.length}</span>
				</div>
			</div>

			{#if validationErrors.length > 0}
				<div class="rounded-lg bg-destructive/10 border border-destructive/20 px-4 py-3 space-y-1">
					{#each validationErrors as err}
						<div class="flex items-center gap-2 text-xs text-destructive">
							<AlertTriangle class="w-3 h-3 shrink-0" /> {err}
						</div>
					{/each}
				</div>
			{:else}
				<button onclick={submitImport} disabled={importing}
					class="w-full py-3 rounded-lg bg-primary text-primary-foreground font-medium text-sm hover:bg-primary/90 disabled:opacity-50 transition-colors flex items-center justify-center gap-2">
					{#if importing}
						<Loader class="w-4 h-4 animate-spin" /> Importing...
					{:else}
						<Upload class="w-4 h-4" /> Import {mappedRecords.length} {entityType}
					{/if}
				</button>
			{/if}
		</div>

	<!-- Step 4: Done -->
	{:else if step === 4}
		<div class="text-center py-12">
			<div class="w-16 h-16 rounded-full bg-emerald-500/15 flex items-center justify-center mx-auto mb-4">
				<Check class="w-8 h-8 text-emerald-400" />
			</div>
			<h2 class="text-xl font-bold text-foreground mb-2">Import Complete</h2>
			{#if importResult}
				<p class="text-sm text-muted-foreground mb-1">{importResult.imported} records imported successfully</p>
				{#if importResult.errors?.length > 0}
					<p class="text-xs text-amber-400 mb-4">{importResult.errors.length} rows skipped due to errors</p>
				{/if}
			{/if}
			<div class="flex items-center justify-center gap-3 mt-6">
				<a href="/{entityType}" class="px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors">
					View {entityType}
				</a>
				<button onclick={reset} class="px-4 py-2 rounded-lg border border-border text-sm text-foreground hover:bg-muted transition-colors">
					Import Another
				</button>
			</div>
		</div>
	{/if}

	<!-- Navigation -->
	{#if step > 1 && step < 4}
		<div class="flex justify-between">
			<button onclick={() => step--}
				class="flex items-center gap-1.5 px-4 py-2 rounded-lg border border-border text-sm text-foreground hover:bg-muted transition-colors">
				<ChevronLeft class="w-4 h-4" /> Back
			</button>
			{#if step < 3}
				<button onclick={() => step++} disabled={parseError != null || mappedRecords.length === 0}
					class="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 disabled:opacity-50 transition-colors">
					Next <ChevronRight class="w-4 h-4" />
				</button>
			{/if}
		</div>
	{/if}
</div>
