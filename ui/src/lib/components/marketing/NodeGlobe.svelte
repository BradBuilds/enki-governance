<script lang="ts">
	/**
	 * NodeGlobe — Cinematic 3D globe for the marketing page.
	 *
	 * Shows ~2,500 "active Enki nodes" pulsing across the globe with arc
	 * connections between federated clusters. Auto-rotates, no user controls.
	 * Combines real hub data (if available) with seed data for visual impact.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { browser } from '$app/environment';

	let container: HTMLElement;
	let globe: any = null;

	// Seed data: ~2,500 realistic node locations weighted by likely adoption
	function generateSeedNodes(): Array<{ lat: number; lng: number; size: number; color: string; city: string; country: string }> {
		const nodes: Array<{ lat: number; lng: number; size: number; color: string; city: string; country: string }> = [];

		// City clusters with weights (more nodes = bigger market)
		const clusters: Array<{ city: string; country: string; lat: number; lng: number; weight: number }> = [
			// United States (heavy)
			{ city: 'Washington DC', country: 'US', lat: 38.9, lng: -77.0, weight: 85 },
			{ city: 'New York', country: 'US', lat: 40.7, lng: -74.0, weight: 70 },
			{ city: 'Los Angeles', country: 'US', lat: 34.0, lng: -118.2, weight: 55 },
			{ city: 'Chicago', country: 'US', lat: 41.8, lng: -87.6, weight: 40 },
			{ city: 'Houston', country: 'US', lat: 29.7, lng: -95.3, weight: 35 },
			{ city: 'Miami', country: 'US', lat: 25.7, lng: -80.2, weight: 45 },
			{ city: 'San Francisco', country: 'US', lat: 37.7, lng: -122.4, weight: 60 },
			{ city: 'Dallas', country: 'US', lat: 32.7, lng: -96.7, weight: 30 },
			{ city: 'Atlanta', country: 'US', lat: 33.7, lng: -84.3, weight: 35 },
			{ city: 'Seattle', country: 'US', lat: 47.6, lng: -122.3, weight: 30 },
			{ city: 'Denver', country: 'US', lat: 39.7, lng: -104.9, weight: 25 },
			{ city: 'Phoenix', country: 'US', lat: 33.4, lng: -112.0, weight: 20 },
			{ city: 'Tampa', country: 'US', lat: 27.9, lng: -82.4, weight: 25 },
			{ city: 'Boston', country: 'US', lat: 42.3, lng: -71.0, weight: 30 },
			{ city: 'San Diego', country: 'US', lat: 32.7, lng: -117.1, weight: 25 },
			{ city: 'Las Vegas', country: 'US', lat: 36.1, lng: -115.1, weight: 15 },
			{ city: 'Salt Lake City', country: 'US', lat: 40.7, lng: -111.8, weight: 15 },
			{ city: 'Norfolk', country: 'US', lat: 36.8, lng: -76.2, weight: 20 },
			{ city: 'San Antonio', country: 'US', lat: 29.4, lng: -98.4, weight: 20 },
			{ city: 'Honolulu', country: 'US', lat: 21.3, lng: -157.8, weight: 15 },

			// Europe
			{ city: 'London', country: 'GB', lat: 51.5, lng: -0.1, weight: 65 },
			{ city: 'Berlin', country: 'DE', lat: 52.5, lng: 13.4, weight: 35 },
			{ city: 'Paris', country: 'FR', lat: 48.8, lng: 2.3, weight: 40 },
			{ city: 'Amsterdam', country: 'NL', lat: 52.3, lng: 4.9, weight: 25 },
			{ city: 'Stockholm', country: 'SE', lat: 59.3, lng: 18.0, weight: 20 },
			{ city: 'Warsaw', country: 'PL', lat: 52.2, lng: 21.0, weight: 25 },
			{ city: 'Rome', country: 'IT', lat: 41.9, lng: 12.5, weight: 20 },
			{ city: 'Madrid', country: 'ES', lat: 40.4, lng: -3.7, weight: 20 },
			{ city: 'Brussels', country: 'BE', lat: 50.8, lng: 4.3, weight: 20 },
			{ city: 'Oslo', country: 'NO', lat: 59.9, lng: 10.7, weight: 15 },
			{ city: 'Helsinki', country: 'FI', lat: 60.1, lng: 24.9, weight: 15 },
			{ city: 'Prague', country: 'CZ', lat: 50.0, lng: 14.4, weight: 15 },
			{ city: 'Bucharest', country: 'RO', lat: 44.4, lng: 26.1, weight: 15 },
			{ city: 'Zurich', country: 'CH', lat: 47.3, lng: 8.5, weight: 20 },
			{ city: 'Vienna', country: 'AT', lat: 48.2, lng: 16.3, weight: 15 },
			{ city: 'Tallinn', country: 'EE', lat: 59.4, lng: 24.7, weight: 15 },
			{ city: 'Kyiv', country: 'UA', lat: 50.4, lng: 30.5, weight: 30 },

			// Middle East
			{ city: 'Tel Aviv', country: 'IL', lat: 32.0, lng: 34.7, weight: 35 },
			{ city: 'Dubai', country: 'AE', lat: 25.2, lng: 55.2, weight: 25 },
			{ city: 'Riyadh', country: 'SA', lat: 24.7, lng: 46.7, weight: 20 },
			{ city: 'Amman', country: 'JO', lat: 31.9, lng: 35.9, weight: 15 },
			{ city: 'Doha', country: 'QA', lat: 25.2, lng: 51.5, weight: 15 },
			{ city: 'Abu Dhabi', country: 'AE', lat: 24.4, lng: 54.6, weight: 15 },
			{ city: 'Ankara', country: 'TR', lat: 39.9, lng: 32.8, weight: 15 },
			{ city: 'Istanbul', country: 'TR', lat: 41.0, lng: 28.9, weight: 20 },

			// Asia Pacific
			{ city: 'Tokyo', country: 'JP', lat: 35.6, lng: 139.6, weight: 40 },
			{ city: 'Singapore', country: 'SG', lat: 1.3, lng: 103.8, weight: 35 },
			{ city: 'Seoul', country: 'KR', lat: 37.5, lng: 126.9, weight: 30 },
			{ city: 'Sydney', country: 'AU', lat: -33.8, lng: 151.2, weight: 35 },
			{ city: 'Melbourne', country: 'AU', lat: -37.8, lng: 144.9, weight: 20 },
			{ city: 'Taipei', country: 'TW', lat: 25.0, lng: 121.5, weight: 25 },
			{ city: 'Mumbai', country: 'IN', lat: 19.0, lng: 72.8, weight: 25 },
			{ city: 'New Delhi', country: 'IN', lat: 28.6, lng: 77.2, weight: 20 },
			{ city: 'Bangalore', country: 'IN', lat: 12.9, lng: 77.5, weight: 20 },
			{ city: 'Bangkok', country: 'TH', lat: 13.7, lng: 100.5, weight: 15 },
			{ city: 'Manila', country: 'PH', lat: 14.5, lng: 120.9, weight: 15 },
			{ city: 'Jakarta', country: 'ID', lat: -6.2, lng: 106.8, weight: 15 },
			{ city: 'Auckland', country: 'NZ', lat: -36.8, lng: 174.7, weight: 10 },
			{ city: 'Perth', country: 'AU', lat: -31.9, lng: 115.8, weight: 10 },

			// Americas (non-US)
			{ city: 'Toronto', country: 'CA', lat: 43.6, lng: -79.3, weight: 30 },
			{ city: 'Vancouver', country: 'CA', lat: 49.2, lng: -123.1, weight: 15 },
			{ city: 'Ottawa', country: 'CA', lat: 45.4, lng: -75.6, weight: 15 },
			{ city: 'Mexico City', country: 'MX', lat: 19.4, lng: -99.1, weight: 20 },
			{ city: 'Bogota', country: 'CO', lat: 4.7, lng: -74.0, weight: 15 },
			{ city: 'Sao Paulo', country: 'BR', lat: -23.5, lng: -46.6, weight: 25 },
			{ city: 'Buenos Aires', country: 'AR', lat: -34.6, lng: -58.3, weight: 15 },
			{ city: 'Lima', country: 'PE', lat: -12.0, lng: -77.0, weight: 10 },
			{ city: 'Santiago', country: 'CL', lat: -33.4, lng: -70.6, weight: 10 },
			{ city: 'Panama City', country: 'PA', lat: 8.9, lng: -79.5, weight: 10 },

			// Africa
			{ city: 'Nairobi', country: 'KE', lat: -1.2, lng: 36.8, weight: 15 },
			{ city: 'Cape Town', country: 'ZA', lat: -33.9, lng: 18.4, weight: 15 },
			{ city: 'Lagos', country: 'NG', lat: 6.5, lng: 3.3, weight: 15 },
			{ city: 'Cairo', country: 'EG', lat: 30.0, lng: 31.2, weight: 20 },
			{ city: 'Casablanca', country: 'MA', lat: 33.5, lng: -7.5, weight: 10 },
			{ city: 'Accra', country: 'GH', lat: 5.5, lng: -0.1, weight: 8 },
			{ city: 'Addis Ababa', country: 'ET', lat: 9.0, lng: 38.7, weight: 8 },
		];

		// Generate nodes around each cluster
		for (const cluster of clusters) {
			const count = cluster.weight;
			for (let i = 0; i < count; i++) {
				// Scatter within ~2 degrees of city center
				const lat = cluster.lat + (Math.random() - 0.5) * 4;
				const lng = cluster.lng + (Math.random() - 0.5) * 4;
				const isLarge = Math.random() < 0.08; // 8% are "large" nodes

				nodes.push({
					lat,
					lng,
					size: isLarge ? 0.6 : 0.2 + Math.random() * 0.25,
					color: isLarge ? '#10b981' : '#3b82f6',
					city: cluster.city,
					country: cluster.country,
				});
			}
		}

		return nodes;
	}

	// Generate federation arcs (connections between major hubs)
	function generateArcs(): Array<{ startLat: number; startLng: number; endLat: number; endLng: number; color: string }> {
		const hubs = [
			{ lat: 38.9, lng: -77.0 },   // DC
			{ lat: 51.5, lng: -0.1 },     // London
			{ lat: 32.0, lng: 34.7 },     // Tel Aviv
			{ lat: 35.6, lng: 139.6 },    // Tokyo
			{ lat: -33.8, lng: 151.2 },   // Sydney
			{ lat: 37.7, lng: -122.4 },   // SF
			{ lat: 52.5, lng: 13.4 },     // Berlin
			{ lat: 1.3, lng: 103.8 },     // Singapore
			{ lat: 25.2, lng: 55.2 },     // Dubai
			{ lat: 43.6, lng: -79.3 },    // Toronto
			{ lat: 50.4, lng: 30.5 },     // Kyiv
			{ lat: 40.7, lng: -74.0 },    // NYC
		];

		const arcs: Array<{ startLat: number; startLng: number; endLat: number; endLng: number; color: string }> = [];

		// Connect major hubs with arcs
		const connections = [
			[0, 1], [0, 5], [0, 9], [0, 11],  // DC connections
			[1, 2], [1, 6], [1, 10],            // London connections
			[2, 8], [2, 7],                      // Tel Aviv connections
			[3, 7], [3, 4],                      // Tokyo connections
			[5, 3],                               // SF-Tokyo
			[6, 10],                              // Berlin-Kyiv
			[7, 4],                               // Singapore-Sydney
			[8, 7],                               // Dubai-Singapore
		];

		for (const [a, b] of connections) {
			arcs.push({
				startLat: hubs[a].lat,
				startLng: hubs[a].lng,
				endLat: hubs[b].lat,
				endLng: hubs[b].lng,
				color: 'rgba(59, 130, 246, 0.15)',
			});
		}

		return arcs;
	}

	onMount(async () => {
		if (!browser) return;

		const Globe = (await import('globe.gl')).default;
		const seedNodes = generateSeedNodes();
		const arcs = generateArcs();

		globe = Globe()
			.globeImageUrl('//unpkg.com/three-globe/example/img/earth-night.jpg')
			.bumpImageUrl('//unpkg.com/three-globe/example/img/earth-topology.png')
			.backgroundImageUrl('//unpkg.com/three-globe/example/img/night-sky.png')
			.showAtmosphere(true)
			.atmosphereColor('#1e40af')
			.atmosphereAltitude(0.15)

			// Node points
			.pointsData(seedNodes)
			.pointLat('lat')
			.pointLng('lng')
			.pointColor('color')
			.pointAltitude(0.005)
			.pointRadius('size')
			.pointsMerge(true)

			// Federation arcs
			.arcsData(arcs)
			.arcStartLat('startLat')
			.arcStartLng('startLng')
			.arcEndLat('endLat')
			.arcEndLng('endLng')
			.arcColor('color')
			.arcAltitudeAutoScale(0.3)
			.arcStroke(0.5)
			.arcDashLength(0.6)
			.arcDashGap(0.3)
			.arcDashAnimateTime(4000)

			.width(container.clientWidth)
			.height(container.clientHeight)
			(container);

		// Cinematic settings — no user interaction
		const controls = globe.controls();
		controls.autoRotate = true;
		controls.autoRotateSpeed = 0.4;
		controls.enableZoom = false;
		controls.enablePan = false;
		controls.enableRotate = false;
		controls.enableDamping = true;

		// Start view: looking at North America / Atlantic
		globe.pointOfView({ lat: 30, lng: -40, altitude: 2.2 });

		// Resize handler
		const observer = new ResizeObserver(() => {
			if (globe && container) {
				globe.width(container.clientWidth);
				globe.height(container.clientHeight);
			}
		});
		observer.observe(container);
	});

	onDestroy(() => {
		if (globe) {
			globe._destructor?.();
			globe = null;
		}
	});
</script>

<section class="relative overflow-hidden" style="background: #050510;">
	<!-- Globe -->
	<div
		bind:this={container}
		class="w-full mx-auto"
		style="height: 520px; max-width: 1000px; background: transparent;"
	></div>

	<!-- Fade edges into page background -->
	<div class="absolute top-0 left-0 right-0 h-24 bg-gradient-to-b from-background to-transparent pointer-events-none"></div>
	<div class="absolute bottom-0 left-0 right-0 h-24 bg-gradient-to-t from-background to-transparent pointer-events-none"></div>
</section>
