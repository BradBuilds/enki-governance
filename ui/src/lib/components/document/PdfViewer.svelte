<script lang="ts">
	import { onMount, onDestroy, tick } from 'svelte';
	import Loader from 'lucide-svelte/icons/loader-2';

	type Props = {
		bytes: ArrayBuffer | null;
		initialPage?: number;
	};
	let { bytes, initialPage = 1 }: Props = $props();

	let pdfjsLib: any = null;
	let pdf: any = null;
	let pageCount = $state(0);
	let loading = $state(true);
	let error = $state('');
	let canvasEls: HTMLCanvasElement[] = $state([]);
	let renderedPages: Set<number> = new Set();

	// Devicepixel-aware scale so retina screens stay sharp without blowing
	// up canvas memory on regular ones.
	const RENDER_SCALE =
		typeof window !== 'undefined' ? Math.min(window.devicePixelRatio || 1, 2) * 1.5 : 1.5;

	onMount(async () => {
		try {
			pdfjsLib = await import('pdfjs-dist');
			const workerMod = await import('pdfjs-dist/build/pdf.worker.min.mjs?url');
			pdfjsLib.GlobalWorkerOptions.workerSrc = workerMod.default;
			await openPdf();
		} catch (e: any) {
			error = `Could not load PDF: ${e?.message ?? e}`;
			loading = false;
		}
	});

	onDestroy(() => {
		try { pdf?.destroy?.(); } catch {}
	});

	$effect(() => {
		// Re-open when the bytes prop changes (eg. user navigates to a
		// different PDF in the same component instance).
		if (bytes && pdfjsLib) openPdf();
	});

	async function openPdf() {
		if (!bytes || !pdfjsLib) return;
		loading = true;
		error = '';
		renderedPages.clear();
		try {
			pdf?.destroy?.();
			pdf = await pdfjsLib.getDocument({ data: new Uint8Array(bytes) }).promise;
			pageCount = pdf.numPages;
			canvasEls = new Array(pageCount);
			loading = false;
			await tick();
			renderAllPages();
		} catch (e: any) {
			error = `Could not parse PDF: ${e?.message ?? e}`;
			loading = false;
		}
	}

	async function renderAllPages() {
		if (!pdf) return;
		for (let n = 1; n <= pageCount; n++) {
			if (renderedPages.has(n)) continue;
			await renderPage(n);
		}
		if (initialPage > 1 && initialPage <= pageCount) {
			canvasEls[initialPage - 1]?.scrollIntoView({ behavior: 'smooth', block: 'start' });
		}
	}

	async function renderPage(n: number) {
		const canvas = canvasEls[n - 1];
		if (!canvas || !pdf) return;
		try {
			const page = await pdf.getPage(n);
			const viewport = page.getViewport({ scale: RENDER_SCALE });
			const ctx = canvas.getContext('2d');
			if (!ctx) return;
			canvas.width = viewport.width;
			canvas.height = viewport.height;
			canvas.style.width = `${viewport.width / RENDER_SCALE}px`;
			canvas.style.height = `${viewport.height / RENDER_SCALE}px`;
			await page.render({ canvasContext: ctx, viewport }).promise;
			renderedPages.add(n);
		} catch (e: any) {
			console.warn(`PDF page ${n} render failed:`, e);
		}
	}
</script>

<div class="flex flex-col h-full">
	{#if loading}
		<div class="flex-1 flex items-center justify-center text-muted-foreground gap-2">
			<Loader class="w-4 h-4 animate-spin" />
			<span class="text-sm">Loading PDF…</span>
		</div>
	{:else if error}
		<div class="flex-1 flex items-center justify-center">
			<p class="text-sm text-destructive">{error}</p>
		</div>
	{:else}
		<div class="flex-1 overflow-auto bg-muted/30 p-4 space-y-3">
			{#each Array(pageCount) as _, idx}
				<div class="flex flex-col items-center">
					<canvas
						bind:this={canvasEls[idx]}
						data-page={idx + 1}
						class="shadow-md bg-white rounded-sm"></canvas>
					<span class="mt-1 text-[10px] text-muted-foreground">
						Page {idx + 1} of {pageCount}
					</span>
				</div>
			{/each}
		</div>
	{/if}
</div>
