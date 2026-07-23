<script lang="ts">
	/**
	 * Export current map view as PNG screenshot.
	 * Uses html2canvas-style approach: reads the map container's canvas/DOM.
	 * For Leaflet: uses leaflet-image or dom serialization.
	 * For MapLibre: uses map.getCanvas().toDataURL().
	 */
	import type { MapAdapter } from './engine/types';
	import Download from 'lucide-svelte/icons/download';

	interface Props {
		adapter: MapAdapter;
	}

	let { adapter }: Props = $props();

	let exporting = $state(false);

	async function screenshot() {
		if (!adapter) return;
		exporting = true;

		try {
			const raw = adapter.getRawMap();
			let dataUrl: string | null = null;

			if (adapter.engine === 'maplibre') {
				// MapLibre: canvas is directly accessible
				const canvas = raw.getCanvas();
				dataUrl = canvas.toDataURL('image/png');
			} else {
				// Leaflet: grab the container and use canvas rendering
				const container = raw.getContainer();
				// Find all canvas elements (tile layers render as img, but overlays use canvas)
				const mapCanvas = container.querySelector('canvas');

				if (mapCanvas) {
					dataUrl = mapCanvas.toDataURL('image/png');
				} else {
					// Fallback: use the container's DOM via a temporary canvas
					// This captures tiles + overlays
					const { default: html2canvas } = await import('html2canvas').catch(() => ({ default: null }));
					if (html2canvas) {
						const canvas = await html2canvas(container, {
							useCORS: true,
							allowTaint: true,
							backgroundColor: '#0a0a1e',
						});
						dataUrl = canvas.toDataURL('image/png');
					}
				}
			}

			if (dataUrl) {
				const link = document.createElement('a');
				link.download = `enki-map-${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.png`;
				link.href = dataUrl;
				link.click();
			}
		} catch (err) {
			console.error('Screenshot failed:', err);
		}

		exporting = false;
	}
</script>

<button
	onclick={screenshot}
	disabled={exporting}
	class="flex items-center gap-1.5 bg-card/90 backdrop-blur-sm border border-border rounded-lg px-3 py-2 shadow-lg text-[10px] text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
>
	<Download class="w-3 h-3" />
	{exporting ? 'Exporting...' : 'Screenshot'}
</button>
