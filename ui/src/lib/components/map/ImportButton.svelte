<script lang="ts">
	/**
	 * Import KML/GPX files as map annotations.
	 * File picker → upload → creates annotations → reloads map.
	 */
	import { api } from '$lib/api/mock';
	import Upload from 'lucide-svelte/icons/upload';

	interface Props {
		projectId?: string;
		onImported?: (count: number) => void;
	}

	let { projectId = '', onImported = () => {} }: Props = $props();

	let importing = $state(false);
	let fileInput: HTMLInputElement;

	async function handleFile(e: Event) {
		const input = e.target as HTMLInputElement;
		const file = input.files?.[0];
		if (!file) return;

		importing = true;

		try {
			const text = await file.text();
			const result = await api.importGeoFile({
				filename: file.name,
				content: text,
				project_id: projectId || undefined,
			});
			onImported(result?.features_imported || 0);
		} catch (err) {
			console.error('Import failed:', err);
		}

		importing = false;
		input.value = '';  // reset for re-upload
	}
</script>

<button
	onclick={() => fileInput.click()}
	disabled={importing}
	class="flex items-center gap-1.5 bg-card/90 backdrop-blur-sm border border-border rounded-lg px-3 py-2 shadow-lg text-[10px] text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
>
	<Upload class="w-3 h-3" />
	{importing ? 'Importing...' : 'Import'}
</button>

<input
	bind:this={fileInput}
	type="file"
	accept=".gpx,.kml,.kmz"
	onchange={handleFile}
	class="hidden"
/>
