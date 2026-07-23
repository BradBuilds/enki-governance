<script lang="ts">
	/**
	 * Upload a geotagged photo → extracts EXIF GPS → creates knowledge node on map.
	 */
	import { api, getHeaders } from '$lib/api/mock';
	import Camera from 'lucide-svelte/icons/camera';

	interface Props {
		projectId?: string;
		onUploaded?: (result: any) => void;
	}

	let { projectId = '', onUploaded = () => {} }: Props = $props();

	let uploading = $state(false);
	let error = $state('');
	let fileInput: HTMLInputElement;

	async function handleFile(e: Event) {
		const input = e.target as HTMLInputElement;
		const file = input.files?.[0];
		if (!file) return;

		if (!projectId) {
			error = 'Select a project first';
			return;
		}

		uploading = true;
		error = '';

		try {
			// Read as base64
			const arrayBuffer = await file.arrayBuffer();
			const base64 = btoa(
				new Uint8Array(arrayBuffer).reduce((data, byte) => data + String.fromCharCode(byte), '')
			);

			const result = await fetch('/api/knowledge/geo/photo', {
				method: 'POST',
				headers: getHeaders(),
				body: JSON.stringify({
					image_base64: base64,
					filename: file.name,
					project_id: projectId,
					epistemic_type: 'observation',
				}),
			}).then(r => r.json());

			if (result.error) {
				error = result.detail || result.error;
			} else {
				onUploaded(result);
			}
		} catch (err: any) {
			error = err?.message || 'Upload failed';
		}

		uploading = false;
		input.value = '';
	}
</script>

<div class="relative">
	<button
		onclick={() => fileInput.click()}
		disabled={uploading}
		class="flex items-center gap-1.5 bg-card/90 backdrop-blur-sm border border-border rounded-lg px-3 py-2 shadow-lg text-[10px] text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
	>
		<Camera class="w-3 h-3" />
		{uploading ? 'Placing...' : 'Photo'}
	</button>

	{#if error}
		<div class="absolute bottom-full mb-1 left-0 bg-enki-red/10 border border-enki-red/30 rounded px-2 py-1 text-[9px] text-enki-red whitespace-nowrap">
			{error}
		</div>
	{/if}
</div>

<input
	bind:this={fileInput}
	type="file"
	accept="image/jpeg,image/jpg,image/png,image/heic"
	onchange={handleFile}
	class="hidden"
/>
