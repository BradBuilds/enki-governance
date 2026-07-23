<script lang="ts">
	/**
	 * GlobeView — real 3D rotating globe using Globe.gl (three.js).
	 * Shows satellite imagery texture with signal/node points plotted on the sphere.
	 * Emits adapter-compatible events for integration with the map page.
	 */
	import { onMount, onDestroy } from 'svelte';

	interface Props {
		height?: string;
		onReady?: (globe: any) => void;
		signals?: any[];
		knowledgeNodes?: any[];
		onSignalClick?: (signal: any) => void;
		onNodeClick?: (node: any) => void;
	}

	let {
		height = '100%',
		onReady = () => {},
		signals = [],
		knowledgeNodes = [],
		onSignalClick = () => {},
		onNodeClick = () => {},
	}: Props = $props();

	let container: HTMLElement;
	let globe: any = null;

	const domainColors: Record<string, string> = {
		geospatial: '#3b82f6', security: '#ef4444', communications: '#a855f7',
		personal: '#22c55e', financial: '#f59e0b', environmental: '#06b6d4',
		infrastructure: '#f97316', vehicle: '#6366f1', intelligence: '#ec4899',
	};

	const epistemicColors: Record<string, string> = {
		fact: '#22c55e', observation: '#3b82f6', claim: '#f59e0b',
		question: '#ef4444', recommendation: '#a855f7', opinion: '#ec4899',
		definition: '#06b6d4',
	};

	function buildPointsData() {
		const points: any[] = [];

		for (const sig of signals) {
			const props = sig.properties || sig;
			const lat = props.latitude;
			const lon = props.longitude;
			if (lat == null || lon == null) continue;
			const domain = (props.signal_type ?? '').split('.')[1] || '';
			points.push({
				lat, lng: lon,
				color: domainColors[domain] || '#6b7280',
				size: 0.4,
				label: props.signal_type?.split('.').pop()?.replace(/_/g, ' ') || 'Signal',
				type: 'signal',
				data: sig,
			});
		}

		for (const node of knowledgeNodes) {
			const props = node.properties || node;
			const lat = props.latitude;
			const lon = props.longitude;
			if (lat == null || lon == null) continue;
			const epType = props.epistemic_type ?? 'observation';
			points.push({
				lat, lng: lon,
				color: epistemicColors[epType] || '#6b7280',
				size: 0.6,
				label: props.title ?? 'Location',
				type: 'node',
				data: node,
			});
		}

		return points;
	}

	onMount(async () => {
		const Globe = (await import('globe.gl')).default;

		globe = Globe()
			.globeImageUrl('//unpkg.com/three-globe/example/img/earth-blue-marble.jpg')
			.bumpImageUrl('//unpkg.com/three-globe/example/img/earth-topology.png')
			.backgroundImageUrl('//unpkg.com/three-globe/example/img/night-sky.png')
			.showAtmosphere(true)
			.atmosphereColor('#3a7bd5')
			.atmosphereAltitude(0.2)
			.pointsData(buildPointsData())
			.pointLat('lat')
			.pointLng('lng')
			.pointColor('color')
			.pointAltitude(0.01)
			.pointRadius('size')
			.pointLabel((d: any) => `<div style="background:rgba(0,0,0,0.8);color:white;padding:4px 8px;border-radius:4px;font-size:11px;">${d.label}</div>`)
			.onPointClick((point: any) => {
				if (point.type === 'signal') onSignalClick(point.data);
				else if (point.type === 'node') onNodeClick(point.data);
			})
			.width(container.clientWidth)
			.height(container.clientHeight)
			(container);

		// Auto-rotate
		globe.controls().autoRotate = true;
		globe.controls().autoRotateSpeed = 0.3;
		globe.controls().enableDamping = true;

		// Handle resize
		const observer = new ResizeObserver(() => {
			if (globe && container) {
				globe.width(container.clientWidth);
				globe.height(container.clientHeight);
			}
		});
		observer.observe(container);

		onReady(globe);
	});

	// Update points when data changes
	$effect(() => {
		if (globe && (signals || knowledgeNodes)) {
			globe.pointsData(buildPointsData());
		}
	});

	onDestroy(() => {
		if (globe) {
			globe._destructor?.();
			globe = null;
		}
	});
</script>

<div
	bind:this={container}
	style="height: {height}; width: 100%;"
	class="rounded-lg"
	style:background-color="#0b0b19"
></div>
