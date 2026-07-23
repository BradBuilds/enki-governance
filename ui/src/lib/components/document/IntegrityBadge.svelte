<script lang="ts">
	import ShieldCheck from 'lucide-svelte/icons/shield-check';
	import ShieldAlert from 'lucide-svelte/icons/shield-alert';
	import Copy from 'lucide-svelte/icons/copy';
	import Check from 'lucide-svelte/icons/check';

	type Props = {
		sha256: string | null;          // expected hash (from response header)
		verifiedSha256?: string | null; // recomputed hash (from received bytes)
		signedAt?: string | null;       // ISO timestamp
		signer?: string | null;         // hex pubkey
		sourceUrl?: string | null;      // original retrieval URL
		mime?: string | null;
		bytes?: number | null;
	};
	let {
		sha256, verifiedSha256 = null,
		signedAt = null, signer = null,
		sourceUrl = null, mime = null, bytes = null,
	}: Props = $props();

	// Match status: green if expected hash matches recomputed; amber if we
	// don't have a recomputed one yet; red if mismatch.
	const verified = $derived(
		sha256 && verifiedSha256 && sha256 === verifiedSha256
	);
	const mismatched = $derived(
		sha256 && verifiedSha256 && sha256 !== verifiedSha256
	);

	const shortHash = $derived(
		sha256 ? `${sha256.slice(0, 12)}…${sha256.slice(-4)}` : ''
	);
	const shortSigner = $derived(
		signer ? `${signer.slice(0, 8)}…${signer.slice(-4)}` : ''
	);
	const sizeLabel = $derived(formatBytes(bytes));

	function formatBytes(n: number | null | undefined): string {
		if (!n && n !== 0) return '';
		if (n < 1024) return `${n} B`;
		if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
		return `${(n / 1024 / 1024).toFixed(1)} MB`;
	}

	let copied = $state(false);
	async function copyHash() {
		if (!sha256) return;
		try {
			await navigator.clipboard.writeText(sha256);
			copied = true;
			setTimeout(() => (copied = false), 1500);
		} catch {}
	}
</script>

<div class="flex flex-wrap items-center gap-3 px-3 py-2 rounded-md border border-border bg-card text-xs">
	<!-- Verification status -->
	<div class="flex items-center gap-1.5">
		{#if mismatched}
			<ShieldAlert class="w-4 h-4 text-destructive" />
			<span class="text-destructive font-medium">Hash mismatch</span>
		{:else if verified}
			<ShieldCheck class="w-4 h-4 text-emerald-400" />
			<span class="text-emerald-400 font-medium">Verified</span>
		{:else}
			<ShieldCheck class="w-4 h-4 text-muted-foreground" />
			<span class="text-muted-foreground">Signed</span>
		{/if}
	</div>

	<!-- Hash -->
	{#if sha256}
		<button type="button" onclick={copyHash}
			class="flex items-center gap-1 font-mono text-muted-foreground hover:text-foreground transition-colors"
			title="Click to copy full hash">
			<span>sha256:{shortHash}</span>
			{#if copied}
				<Check class="w-3 h-3 text-emerald-400" />
			{:else}
				<Copy class="w-3 h-3 opacity-60" />
			{/if}
		</button>
	{/if}

	<!-- Signer -->
	{#if signer}
		<div class="flex items-center gap-1 font-mono text-muted-foreground" title="Ed25519 signer pubkey">
			<span class="text-[10px] uppercase tracking-wider">Signer</span>
			<span>{shortSigner}</span>
		</div>
	{/if}

	<!-- Signed at -->
	{#if signedAt}
		<div class="text-muted-foreground" title="Ingested + signed timestamp">
			{new Date(signedAt).toLocaleString()}
		</div>
	{/if}

	<!-- Mime + size -->
	<div class="ml-auto flex items-center gap-3 text-muted-foreground">
		{#if mime}
			<span class="font-mono">{mime}</span>
		{/if}
		{#if sizeLabel}
			<span>{sizeLabel}</span>
		{/if}
	</div>

	<!-- Source link -->
	{#if sourceUrl}
		<a href={sourceUrl} target="_blank" rel="noopener"
			class="text-primary hover:underline">
			Original URL ↗
		</a>
	{/if}
</div>
