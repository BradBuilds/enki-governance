<script lang="ts">
	/**
	 * EntityDossier — unified view of entity_identifiers + entity_documents +
	 * entity_images for a single contact / asset / node. Hits the /entities/:id
	 * endpoints in entity_profile.py.
	 *
	 * Props:
	 *   entityId:   the contact/asset/node id
	 *   entityType: 'contact' | 'asset' | 'node'
	 *
	 * Intended to drop into an existing tabbed detail page as the contents
	 * of a "Dossier" tab.
	 */

	import { onMount } from 'svelte';
	import IdCard from 'lucide-svelte/icons/id-card';
	import FileText from 'lucide-svelte/icons/file-text';
	import Image from 'lucide-svelte/icons/image';
	import Plus from 'lucide-svelte/icons/plus';
	import X from 'lucide-svelte/icons/x';
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import ShieldCheck from 'lucide-svelte/icons/shield-check';
	import AlertCircle from 'lucide-svelte/icons/alert-circle';
	import Star from 'lucide-svelte/icons/star';
	import Trash2 from 'lucide-svelte/icons/trash-2';

	let { entityId, entityType }: {
		entityId: string;
		entityType: 'contact' | 'asset' | 'node';
	} = $props();

	// ── Section state ────────────────────────────────────────────────
	type Section = 'identifiers' | 'documents' | 'images';
	let section = $state<Section>('identifiers');

	// ── Data ─────────────────────────────────────────────────────────
	let identifiers = $state<any[]>([]);
	let documents = $state<any[]>([]);
	let images = $state<any[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// ── Add forms ────────────────────────────────────────────────────
	let showAddId = $state(false);
	let newIdType = $state('passport');
	let newIdNumber = $state('');
	let newIdCountry = $state('');
	let newIdIssuer = $state('');
	let newIdIssueDate = $state('');
	let newIdExpiryDate = $state('');
	let newIdScanUrl = $state('');
	let newIdNotes = $state('');
	let saving = $state(false);

	let showAddDoc = $state(false);
	let newDocTitle = $state('');
	let newDocType = $state('report');
	let newDocCategory = $state('general');
	let newDocClassification = $state('unclassified');
	let newDocFileUrl = $state('');
	let newDocDescription = $state('');
	let newDocContent = $state('');

	let showAddImg = $state(false);
	let newImgType = $state('profile');
	let newImgUrl = $state('');
	let newImgCaption = $state('');
	let newImgPrimary = $state(false);

	// ── Derived counts ──────────────────────────────────────────────
	const identifierCount = $derived(identifiers.length);
	const documentCount = $derived(documents.length);
	const imageCount = $derived(images.length);

	// Identifier types vary by entity type
	const identifierTypes = $derived(
		entityType === 'asset'
			? [
				{ value: 'vin', label: 'VIN (vehicle)' },
				{ value: 'license_plate', label: 'License Plate' },
				{ value: 'mmsi', label: 'MMSI (vessel)' },
				{ value: 'imo', label: 'IMO Number (vessel)' },
				{ value: 'registration', label: 'Registration' },
				{ value: 'icao_hex', label: 'ICAO Hex (aircraft)' },
				{ value: 'tail_number', label: 'Tail Number (aircraft)' },
				{ value: 'serial', label: 'Serial Number' },
				{ value: 'other', label: 'Other' },
			  ]
			: [
				{ value: 'passport', label: 'Passport' },
				{ value: 'drivers_license', label: "Driver's License" },
				{ value: 'national_id', label: 'National ID' },
				{ value: 'ssn', label: 'SSN / Tax ID' },
				{ value: 'visa', label: 'Visa' },
				{ value: 'residence', label: 'Residence Permit' },
				{ value: 'ein', label: 'EIN (company)' },
				{ value: 'other', label: 'Other' },
			  ]
	);

	const imageTypes = $derived(
		entityType === 'asset'
			? [
				{ value: 'profile', label: 'Main photo' },
				{ value: 'exterior', label: 'Exterior' },
				{ value: 'interior', label: 'Interior' },
				{ value: 'vehicle_photo', label: 'Vehicle photo' },
				{ value: 'registration_scan', label: 'Registration scan' },
				{ value: 'other', label: 'Other' },
			  ]
			: [
				{ value: 'profile', label: 'Profile photo' },
				{ value: 'mugshot', label: 'Mugshot' },
				{ value: 'passport_photo', label: 'Passport photo' },
				{ value: 'surveillance', label: 'Surveillance' },
				{ value: 'social', label: 'Social media' },
				{ value: 'other', label: 'Other' },
			  ]
	);

	// ── API ──────────────────────────────────────────────────────────
	async function loadAll() {
		loading = true;
		error = null;
		try {
			const q = `?entity_type=${entityType}`;
			const [ids, docs, imgs] = await Promise.all([
				fetch(`/api/entities/${entityId}/identifiers${q}`).then((r) => r.ok ? r.json() : { identifiers: [] }),
				fetch(`/api/entities/${entityId}/documents${q}`).then((r) => r.ok ? r.json() : { documents: [] }),
				fetch(`/api/entities/${entityId}/images${q}`).then((r) => r.ok ? r.json() : { images: [] }),
			]);
			identifiers = ids.identifiers || [];
			documents = docs.documents || [];
			images = imgs.images || [];
		} catch (e: any) {
			error = e?.message || String(e);
		} finally {
			loading = false;
		}
	}

	async function saveIdentifier() {
		if (!newIdNumber.trim()) return;
		saving = true;
		try {
			const res = await fetch(`/api/entities/${entityId}/identifiers`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					entity_type: entityType,
					entity_id: entityId,
					id_type: newIdType,
					id_number: newIdNumber.trim(),
					id_country: newIdCountry.trim() || null,
					id_issuer: newIdIssuer.trim() || null,
					issue_date: newIdIssueDate || null,
					expiry_date: newIdExpiryDate || null,
					scan_url: newIdScanUrl.trim() || null,
					notes: newIdNotes.trim() || null,
				}),
			});
			if (!res.ok) throw new Error(await res.text());
			newIdNumber = '';
			newIdCountry = '';
			newIdIssuer = '';
			newIdIssueDate = '';
			newIdExpiryDate = '';
			newIdScanUrl = '';
			newIdNotes = '';
			showAddId = false;
			await loadAll();
		} catch (e: any) {
			error = e?.message || String(e);
		} finally {
			saving = false;
		}
	}

	async function deleteIdentifier(id: string) {
		if (!confirm('Remove this identifier?')) return;
		try {
			await fetch(`/api/entities/identifiers/${id}`, { method: 'DELETE' });
			await loadAll();
		} catch (e: any) {
			error = e?.message || String(e);
		}
	}

	async function saveDocument() {
		if (!newDocTitle.trim()) return;
		saving = true;
		try {
			const res = await fetch(`/api/entities/${entityId}/documents`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					entity_type: entityType,
					entity_id: entityId,
					title: newDocTitle.trim(),
					doc_type: newDocType,
					doc_category: newDocCategory,
					classification: newDocClassification,
					description: newDocDescription.trim() || null,
					content: newDocContent.trim() || null,
					file_url: newDocFileUrl.trim() || null,
				}),
			});
			if (!res.ok) throw new Error(await res.text());
			newDocTitle = '';
			newDocDescription = '';
			newDocContent = '';
			newDocFileUrl = '';
			showAddDoc = false;
			await loadAll();
		} catch (e: any) {
			error = e?.message || String(e);
		} finally {
			saving = false;
		}
	}

	async function saveImage() {
		if (!newImgUrl.trim()) return;
		saving = true;
		try {
			const res = await fetch(`/api/entities/${entityId}/images`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					entity_type: entityType,
					entity_id: entityId,
					image_type: newImgType,
					url: newImgUrl.trim(),
					caption: newImgCaption.trim() || null,
					is_primary: newImgPrimary,
				}),
			});
			if (!res.ok) throw new Error(await res.text());
			newImgUrl = '';
			newImgCaption = '';
			newImgPrimary = false;
			showAddImg = false;
			await loadAll();
		} catch (e: any) {
			error = e?.message || String(e);
		} finally {
			saving = false;
		}
	}

	function humanDate(iso?: string | null): string {
		if (!iso) return '—';
		try {
			return new Date(iso).toLocaleDateString();
		} catch {
			return iso;
		}
	}

	function isExpired(iso?: string | null): boolean {
		if (!iso) return false;
		const t = new Date(iso).getTime();
		return !isNaN(t) && t < Date.now();
	}

	const classifColor: Record<string, string> = {
		unclassified: 'bg-muted/40 text-muted-foreground border-border',
		restricted: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
		confidential: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
		secret: 'bg-red-500/10 text-red-400 border-red-500/20',
	};

	onMount(loadAll);
</script>

<div class="space-y-4">
	<!-- Section switcher -->
	<div class="flex items-center gap-1 border-b border-border">
		{#each [
			{ id: 'identifiers' as Section, label: 'Identifiers', icon: IdCard, count: identifierCount },
			{ id: 'documents' as Section, label: 'Documents', icon: FileText, count: documentCount },
			{ id: 'images' as Section, label: 'Images', icon: Image, count: imageCount },
		] as t}
			<button onclick={() => section = t.id}
				class="flex items-center gap-1.5 text-xs px-3 py-2 border-b-2 -mb-px transition-colors
					{section === t.id ? 'text-primary border-primary' : 'text-muted-foreground border-transparent hover:text-foreground'}">
				<t.icon class="w-3.5 h-3.5" />
				{t.label}
				{#if t.count > 0}
					<span class="text-[10px] px-1.5 py-0.5 rounded-full bg-muted/50">{t.count}</span>
				{/if}
			</button>
		{/each}
	</div>

	{#if error}
		<div class="rounded-md border border-red-500/30 bg-red-500/5 px-3 py-2 text-xs text-red-400">
			{error}
		</div>
	{/if}

	<!-- ── Identifiers ───────────────────────────────────────────── -->
	{#if section === 'identifiers'}
		<div class="flex items-center justify-between">
			<h3 class="text-sm font-semibold text-foreground">Government & unique IDs</h3>
			<button onclick={() => showAddId = !showAddId}
				class="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors">
				{#if showAddId}<X class="w-3 h-3" />{:else}<Plus class="w-3 h-3" />{/if}
				{showAddId ? 'Cancel' : 'Add ID'}
			</button>
		</div>

		{#if showAddId}
			<div class="rounded-lg border border-primary/50 bg-card p-4 space-y-3">
				<div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
					<div>
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Type</label>
						<select bind:value={newIdType} class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
							{#each identifierTypes as opt}<option value={opt.value}>{opt.label}</option>{/each}
						</select>
					</div>
					<div>
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Number</label>
						<input type="text" bind:value={newIdNumber} placeholder="ID number…"
							class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring font-mono" />
					</div>
					<div>
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Country</label>
						<input type="text" bind:value={newIdCountry} placeholder="US, UK, ..."
							class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
					</div>
					<div>
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Issuer</label>
						<input type="text" bind:value={newIdIssuer} placeholder="e.g. California DMV"
							class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
					</div>
					<div>
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Issue date</label>
						<input type="date" bind:value={newIdIssueDate}
							class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
					</div>
					<div>
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Expiry date</label>
						<input type="date" bind:value={newIdExpiryDate}
							class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
					</div>
					<div class="sm:col-span-2">
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Scan URL</label>
						<input type="url" bind:value={newIdScanUrl} placeholder="https://..."
							class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
					</div>
					<div class="sm:col-span-2">
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Notes</label>
						<textarea bind:value={newIdNotes} rows="2" placeholder="Notes…"
							class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring resize-none"></textarea>
					</div>
				</div>
				<button onclick={saveIdentifier} disabled={!newIdNumber.trim() || saving}
					class="px-4 py-2 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors">
					{saving ? 'Saving...' : 'Save ID'}
				</button>
			</div>
		{/if}

		{#if loading}
			<div class="text-center py-6 text-xs text-muted-foreground">Loading…</div>
		{:else if identifiers.length === 0}
			<div class="text-center py-8 text-muted-foreground">
				<IdCard class="w-6 h-6 mx-auto mb-2 opacity-50" />
				<p class="text-sm">No identifiers on file</p>
				<p class="text-xs">Add passports, licenses, registrations, VINs, MMSI numbers…</p>
			</div>
		{:else}
			<div class="space-y-2">
				{#each identifiers as id}
					<div class="rounded-lg border border-border bg-card p-3 flex items-start gap-3">
						<IdCard class="w-4 h-4 text-muted-foreground shrink-0 mt-0.5" />
						<div class="flex-1 min-w-0">
							<div class="flex items-center gap-2 flex-wrap">
								<span class="text-xs font-semibold text-foreground uppercase">{id.id_type.replace('_', ' ')}</span>
								{#if id.verified}
									<span class="text-[9px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 flex items-center gap-0.5">
										<ShieldCheck class="w-2.5 h-2.5" /> verified
									</span>
								{/if}
								{#if id.status && id.status !== 'active'}
									<span class="text-[9px] px-1.5 py-0.5 rounded bg-muted/50 text-muted-foreground">{id.status}</span>
								{/if}
								{#if isExpired(id.expiry_date)}
									<span class="text-[9px] px-1.5 py-0.5 rounded bg-red-500/10 text-red-400 border border-red-500/20 flex items-center gap-0.5">
										<AlertCircle class="w-2.5 h-2.5" /> expired
									</span>
								{/if}
							</div>
							<div class="text-sm font-mono text-foreground mt-0.5 select-all">{id.id_number}</div>
							<div class="flex items-center gap-3 mt-1 text-[10px] text-muted-foreground flex-wrap">
								{#if id.id_country}<span>Country: {id.id_country}</span>{/if}
								{#if id.id_issuer}<span>· Issuer: {id.id_issuer}</span>{/if}
								{#if id.issue_date}<span>· Issued: {humanDate(id.issue_date)}</span>{/if}
								{#if id.expiry_date}<span>· Expires: {humanDate(id.expiry_date)}</span>{/if}
							</div>
							{#if id.notes}
								<p class="text-[11px] text-muted-foreground mt-1.5 italic">{id.notes}</p>
							{/if}
							{#if id.scan_url}
								<a href={id.scan_url} target="_blank" rel="noopener"
									class="inline-flex items-center gap-1 text-[11px] text-primary hover:underline mt-1.5">
									<ExternalLink class="w-3 h-3" /> View scan
								</a>
							{/if}
						</div>
						<button onclick={() => deleteIdentifier(id.identifier_id)}
							class="p-1 text-muted-foreground hover:text-destructive transition-colors"
							title="Remove">
							<Trash2 class="w-3.5 h-3.5" />
						</button>
					</div>
				{/each}
			</div>
		{/if}
	{/if}

	<!-- ── Documents ────────────────────────────────────────────── -->
	{#if section === 'documents'}
		<div class="flex items-center justify-between">
			<h3 class="text-sm font-semibold text-foreground">Filed documents</h3>
			<button onclick={() => showAddDoc = !showAddDoc}
				class="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors">
				{#if showAddDoc}<X class="w-3 h-3" />{:else}<Plus class="w-3 h-3" />{/if}
				{showAddDoc ? 'Cancel' : 'File Document'}
			</button>
		</div>

		{#if showAddDoc}
			<div class="rounded-lg border border-primary/50 bg-card p-4 space-y-3">
				<div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
					<div class="sm:col-span-2">
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Title</label>
						<input type="text" bind:value={newDocTitle} placeholder="Document title…"
							class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
					</div>
					<div>
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Type</label>
						<select bind:value={newDocType} class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
							<option value="report">Report</option>
							<option value="warrant">Warrant</option>
							<option value="court_order">Court order</option>
							<option value="intelligence">Intelligence product</option>
							<option value="correspondence">Correspondence</option>
							<option value="financial">Financial record</option>
							<option value="legal">Legal document</option>
							<option value="contract">Contract</option>
							<option value="briefing">Briefing</option>
							<option value="other">Other</option>
						</select>
					</div>
					<div>
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Category</label>
						<select bind:value={newDocCategory} class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
							<option value="general">General</option>
							<option value="legal">Legal</option>
							<option value="financial">Financial</option>
							<option value="operational">Operational</option>
							<option value="intelligence">Intelligence</option>
							<option value="admin">Admin</option>
						</select>
					</div>
					<div>
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Classification</label>
						<select bind:value={newDocClassification} class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
							<option value="unclassified">Unclassified</option>
							<option value="restricted">Restricted</option>
							<option value="confidential">Confidential</option>
							<option value="secret">Secret</option>
						</select>
					</div>
					<div>
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">File URL (optional)</label>
						<input type="url" bind:value={newDocFileUrl} placeholder="https://..."
							class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
					</div>
					<div class="sm:col-span-2">
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Description</label>
						<input type="text" bind:value={newDocDescription} placeholder="Brief description"
							class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
					</div>
					<div class="sm:col-span-2">
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Content (optional — for in-line docs)</label>
						<textarea bind:value={newDocContent} rows="4" placeholder="Document text (markdown supported)…"
							class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring resize-none font-mono text-xs"></textarea>
					</div>
				</div>
				<button onclick={saveDocument} disabled={!newDocTitle.trim() || saving}
					class="px-4 py-2 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors">
					{saving ? 'Saving...' : 'File Document'}
				</button>
			</div>
		{/if}

		{#if loading}
			<div class="text-center py-6 text-xs text-muted-foreground">Loading…</div>
		{:else if documents.length === 0}
			<div class="text-center py-8 text-muted-foreground">
				<FileText class="w-6 h-6 mx-auto mb-2 opacity-50" />
				<p class="text-sm">No documents filed</p>
				<p class="text-xs">Warrants, court orders, intelligence products, legal records…</p>
			</div>
		{:else}
			<div class="space-y-2">
				{#each documents as doc}
					<div class="rounded-lg border border-border bg-card p-3">
						<div class="flex items-start gap-3">
							<FileText class="w-4 h-4 text-muted-foreground shrink-0 mt-0.5" />
							<div class="flex-1 min-w-0">
								<div class="flex items-center gap-2 flex-wrap">
									<h4 class="text-sm font-medium text-foreground">{doc.title}</h4>
									<span class="text-[9px] px-1.5 py-0.5 rounded bg-muted/50 text-muted-foreground uppercase">{(doc.doc_type || '').replace('_', ' ')}</span>
									{#if doc.classification && doc.classification !== 'unclassified'}
										<span class="text-[9px] px-1.5 py-0.5 rounded border uppercase {classifColor[doc.classification] || classifColor.unclassified}">
											{doc.classification}
										</span>
									{/if}
									{#if doc.reviewed}
										<span class="text-[9px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">reviewed</span>
									{/if}
								</div>
								{#if doc.description}
									<p class="text-xs text-muted-foreground mt-0.5">{doc.description}</p>
								{/if}
								<div class="flex items-center gap-3 mt-1 text-[10px] text-muted-foreground flex-wrap">
									{#if doc.issued_by}<span>Issued by: {doc.issued_by}</span>{/if}
									{#if doc.issued_date}<span>· {humanDate(doc.issued_date)}</span>{/if}
									{#if doc.expiry_date}<span>· Expires: {humanDate(doc.expiry_date)}</span>{/if}
								</div>
								{#if doc.file_url}
									<a href={doc.file_url} target="_blank" rel="noopener"
										class="inline-flex items-center gap-1 text-[11px] text-primary hover:underline mt-1.5">
										<ExternalLink class="w-3 h-3" /> View file
										{#if doc.file_size}
											<span class="text-muted-foreground">({(doc.file_size / 1024).toFixed(0)} KB)</span>
										{/if}
									</a>
								{/if}
							</div>
						</div>
					</div>
				{/each}
			</div>
		{/if}
	{/if}

	<!-- ── Images ───────────────────────────────────────────────── -->
	{#if section === 'images'}
		<div class="flex items-center justify-between">
			<h3 class="text-sm font-semibold text-foreground">Photos & scans</h3>
			<button onclick={() => showAddImg = !showAddImg}
				class="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors">
				{#if showAddImg}<X class="w-3 h-3" />{:else}<Plus class="w-3 h-3" />{/if}
				{showAddImg ? 'Cancel' : 'Add Image'}
			</button>
		</div>

		{#if showAddImg}
			<div class="rounded-lg border border-primary/50 bg-card p-4 space-y-3">
				<div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
					<div>
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Type</label>
						<select bind:value={newImgType} class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
							{#each imageTypes as opt}<option value={opt.value}>{opt.label}</option>{/each}
						</select>
					</div>
					<div class="flex items-end">
						<label class="flex items-center gap-2 text-xs text-foreground cursor-pointer">
							<input type="checkbox" bind:checked={newImgPrimary}
								class="rounded border-border bg-muted text-primary focus:ring-1 focus:ring-ring" />
							Set as primary photo
						</label>
					</div>
					<div class="sm:col-span-2">
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Image URL</label>
						<input type="url" bind:value={newImgUrl} placeholder="https://..."
							class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
					</div>
					<div class="sm:col-span-2">
						<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Caption</label>
						<input type="text" bind:value={newImgCaption} placeholder="Describe the image"
							class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
					</div>
				</div>
				<button onclick={saveImage} disabled={!newImgUrl.trim() || saving}
					class="px-4 py-2 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors">
					{saving ? 'Saving...' : 'Save Image'}
				</button>
			</div>
		{/if}

		{#if loading}
			<div class="text-center py-6 text-xs text-muted-foreground">Loading…</div>
		{:else if images.length === 0}
			<div class="text-center py-8 text-muted-foreground">
				<Image class="w-6 h-6 mx-auto mb-2 opacity-50" />
				<p class="text-sm">No images uploaded</p>
				<p class="text-xs">Profile photos, surveillance images, passport scans…</p>
			</div>
		{:else}
			<div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
				{#each images as img}
					<div class="rounded-lg border border-border bg-card overflow-hidden group relative">
						<div class="aspect-square bg-muted/30 flex items-center justify-center overflow-hidden">
							<img src={img.thumbnail_url || img.url} alt={img.caption || img.image_type}
								class="w-full h-full object-cover"
								onerror={(e) => { (e.target as HTMLImageElement).style.opacity = '0.3'; }} />
						</div>
						{#if img.is_primary}
							<span class="absolute top-1.5 left-1.5 bg-amber-500 text-white rounded-full p-1" title="Primary photo">
								<Star class="w-3 h-3" fill="currentColor" />
							</span>
						{/if}
						<div class="p-2">
							<div class="flex items-center gap-1 mb-0.5">
								<span class="text-[9px] uppercase px-1.5 py-0.5 rounded bg-muted/50 text-muted-foreground">{img.image_type.replace('_', ' ')}</span>
								{#if img.verified}<ShieldCheck class="w-2.5 h-2.5 text-emerald-400" />{/if}
							</div>
							{#if img.caption}
								<p class="text-[10px] text-foreground/80 line-clamp-2">{img.caption}</p>
							{/if}
							<a href={img.url} target="_blank" rel="noopener"
								class="inline-flex items-center gap-1 text-[10px] text-primary hover:underline mt-1">
								<ExternalLink class="w-2.5 h-2.5" /> full size
							</a>
						</div>
					</div>
				{/each}
			</div>
		{/if}
	{/if}
</div>
