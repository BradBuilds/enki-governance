<script lang="ts">
	import PdfViewer from './PdfViewer.svelte';
	import IntegrityBadge from './IntegrityBadge.svelte';
	import AlertCircle from 'lucide-svelte/icons/alert-circle';
	import Loader from 'lucide-svelte/icons/loader-2';
	import Download from 'lucide-svelte/icons/download';
	import FileText from 'lucide-svelte/icons/file-text';
	import Quote from 'lucide-svelte/icons/quote';
	import X from 'lucide-svelte/icons/x';

	type Props = {
		documentId: number;
		sourceUrl?: string | null;     // original ingest URL (display + fallback)
		title?: string | null;
		initialPage?: number;
		// When set, the viewer fetches /documents/statement/{id}/source-span
		// and renders a sticky 'source for' banner pointing to the right span.
		statementId?: number | null;
	};
	let {
		documentId, sourceUrl = null, title = null, initialPage = 1,
		statementId = null,
	}: Props = $props();

	let bytes: ArrayBuffer | null = $state(null);
	let mime = $state<string | null>(null);
	let expectedHash = $state<string | null>(null);
	let verifiedHash = $state<string | null>(null);
	let signedAt = $state<string | null>(null);
	let signer = $state<string | null>(null);
	let byteCount = $state<number | null>(null);
	let filename = $state<string | null>(null);
	let error = $state('');
	let loading = $state(true);

	// Source-span banner state (set when statementId is provided)
	let sourceSpan = $state<null | {
		statement_id: number;
		raw_text: string | null;
		page_order: number | null;
		char_start: number | null;
		char_end: number | null;
	}>(null);
	let dismissedBanner = $state(false);
	const targetPage = $derived(sourceSpan?.page_order ?? initialPage);

	$effect(() => {
		if (documentId) load();
	});

	$effect(() => {
		if (statementId) loadSourceSpan(statementId);
		else sourceSpan = null;
	});

	async function loadSourceSpan(sid: number) {
		try {
			const r = await fetch(`/api/documents/statement/${sid}/source-span`);
			if (r.ok) {
				sourceSpan = await r.json();
				dismissedBanner = false;
			} else {
				sourceSpan = null;
			}
		} catch {
			sourceSpan = null;
		}
	}

	async function load() {
		loading = true;
		error = '';
		bytes = null;
		try {
			const res = await fetch(`/api/documents/${documentId}/source`);
			if (!res.ok) {
				error = `Could not load source (${res.status})`;
				loading = false;
				return;
			}
			mime = res.headers.get('content-type')?.split(';')[0] ?? null;
			expectedHash = res.headers.get('x-enki-content-hash');
			signedAt = res.headers.get('x-enki-signed-at');
			signer = res.headers.get('x-enki-signer');
			// Pull download-name out of Content-Disposition if present
			const cd = res.headers.get('content-disposition') ?? '';
			const m = cd.match(/filename="([^"]+)"/);
			filename = m ? m[1] : null;

			const buf = await res.arrayBuffer();
			bytes = buf;
			byteCount = buf.byteLength;

			// Recompute sha256 client-side to prove integrity. The expected
			// hash came from the server's X-ENKI-Content-Hash header; if a
			// MITM or storage corruption mutated bytes, this won't match.
			verifiedHash = `sha256:${await sha256Hex(buf)}`;
			loading = false;
		} catch (e: any) {
			error = `Network error: ${e?.message ?? e}`;
			loading = false;
		}
	}

	async function sha256Hex(buf: ArrayBuffer): Promise<string> {
		const h = await crypto.subtle.digest('SHA-256', buf);
		return Array.from(new Uint8Array(h))
			.map((b) => b.toString(16).padStart(2, '0'))
			.join('');
	}

	function downloadOriginal() {
		if (!bytes) return;
		const blob = new Blob([bytes], { type: mime || 'application/octet-stream' });
		const a = document.createElement('a');
		a.href = URL.createObjectURL(blob);
		a.download = filename || `document-${documentId}`;
		document.body.appendChild(a);
		a.click();
		a.remove();
		setTimeout(() => URL.revokeObjectURL(a.href), 5000);
	}
</script>

<div class="flex flex-col h-full min-h-[600px]">
	<!-- Header: title + download + integrity -->
	<div class="flex-shrink-0 space-y-2 mb-3">
		<div class="flex items-center justify-between gap-3">
			<h2 class="text-base font-semibold text-foreground truncate flex items-center gap-2">
				<FileText class="w-4 h-4 text-muted-foreground shrink-0" />
				<span class="truncate">{title || filename || `Document ${documentId}`}</span>
			</h2>
			<button onclick={downloadOriginal} disabled={!bytes}
				class="flex items-center gap-1.5 px-2.5 py-1 rounded-md border border-border text-xs hover:bg-muted disabled:opacity-50">
				<Download class="w-3.5 h-3.5" />
				Download
			</button>
		</div>
		<IntegrityBadge
			sha256={expectedHash?.replace(/^sha256:/, '') ?? null}
			verifiedSha256={verifiedHash?.replace(/^sha256:/, '') ?? null}
			{signedAt} {signer} {mime} bytes={byteCount} {sourceUrl} />

		<!-- Source-span banner — shown when the viewer was opened via a
		     statement deep-link (e.g. clicked from a dossier fact). -->
		{#if sourceSpan && !dismissedBanner}
			<div class="flex items-start gap-2 px-3 py-2 rounded-md border border-primary/30 bg-primary/5 text-xs">
				<Quote class="w-4 h-4 text-primary shrink-0 mt-0.5" />
				<div class="flex-1 min-w-0">
					<div class="font-medium text-foreground mb-0.5">
						Source span for statement #{sourceSpan.statement_id}
						{#if sourceSpan.page_order}
							<span class="text-muted-foreground font-normal">
								· page {sourceSpan.page_order}
								{#if sourceSpan.char_start != null && sourceSpan.char_end != null}
									· chars {sourceSpan.char_start}–{sourceSpan.char_end}
								{/if}
							</span>
						{/if}
					</div>
					{#if sourceSpan.raw_text}
						<div class="text-muted-foreground italic line-clamp-3 break-words">
							"{sourceSpan.raw_text.slice(0, 280)}{sourceSpan.raw_text.length > 280 ? '…' : ''}"
						</div>
					{/if}
				</div>
				<button onclick={() => (dismissedBanner = true)}
					class="text-muted-foreground hover:text-foreground" aria-label="Dismiss">
					<X class="w-4 h-4" />
				</button>
			</div>
		{/if}
	</div>

	<!-- Body: per-mime renderer -->
	<div class="flex-1 min-h-0 border border-border rounded-md overflow-hidden bg-background">
		{#if loading}
			<div class="h-full flex items-center justify-center gap-2 text-muted-foreground">
				<Loader class="w-4 h-4 animate-spin" />
				<span class="text-sm">Loading…</span>
			</div>
		{:else if error}
			<div class="h-full flex flex-col items-center justify-center gap-2 p-6">
				<AlertCircle class="w-5 h-5 text-destructive" />
				<p class="text-sm text-destructive">{error}</p>
				{#if sourceUrl}
					<a href={sourceUrl} target="_blank" rel="noopener" class="text-xs text-primary hover:underline">
						View at original URL ↗
					</a>
				{/if}
			</div>
		{:else if mime === 'application/pdf' && bytes}
			<PdfViewer {bytes} initialPage={targetPage} />
		{:else if mime?.startsWith('text/') && bytes}
			<!-- Plain text / markdown / HTML — for now, show as text. HTML/MD
			     renderers come in a later commit. -->
			{#await new Response(bytes).text() then text}
				<pre class="p-4 text-xs font-mono whitespace-pre-wrap text-foreground overflow-auto h-full">{text}</pre>
			{/await}
		{:else if mime?.startsWith('image/') && bytes}
			<div class="h-full flex items-center justify-center bg-muted/30 overflow-auto p-4">
				{#await new Response(bytes).blob() then blob}
					<img src={URL.createObjectURL(blob)} alt={filename ?? ''} class="max-w-full max-h-full" />
				{/await}
			</div>
		{:else}
			<div class="h-full flex flex-col items-center justify-center gap-2 p-6 text-center">
				<FileText class="w-8 h-8 text-muted-foreground" />
				<p class="text-sm text-muted-foreground">
					No in-browser viewer for <code class="font-mono">{mime ?? 'unknown'}</code> yet.
				</p>
				<button onclick={downloadOriginal}
					class="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-primary text-primary-foreground text-xs hover:bg-primary/90">
					<Download class="w-3.5 h-3.5" /> Download to view
				</button>
			</div>
		{/if}
	</div>
</div>
