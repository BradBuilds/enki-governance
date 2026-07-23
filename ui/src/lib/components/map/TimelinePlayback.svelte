<script lang="ts">
	/**
	 * Timeline playback scrubber for map context views.
	 * Shows a horizontal timeline bar with event dots, play/pause, speed control,
	 * and a draggable playhead. Emits the current time position for map filtering.
	 */
	import { onDestroy } from 'svelte';
	import Play from 'lucide-svelte/icons/play';
	import Pause from 'lucide-svelte/icons/pause';
	import SkipBack from 'lucide-svelte/icons/skip-back';
	import SkipForward from 'lucide-svelte/icons/skip-forward';
	import Clock from 'lucide-svelte/icons/clock';

	interface TimelineEvent {
		id: string;
		timestamp: string;
		title: string;
		event_type?: string;
		latitude?: number;
		longitude?: number;
	}

	interface Props {
		events?: TimelineEvent[];
		onTimeChange?: (currentTime: Date | null) => void;
		onEventClick?: (event: TimelineEvent) => void;
	}

	let { events = [], onTimeChange, onEventClick }: Props = $props();

	let playing = $state(false);
	let speed = $state(1); // days per second
	let playheadPosition = $state(1.0); // 0-1 normalized position
	let trackEl: HTMLDivElement;
	let isDragging = $state(false);
	let animFrame: number | null = null;
	let lastTick = 0;

	const speeds = [
		{ label: '1d/s', value: 1 },
		{ label: '7d/s', value: 7 },
		{ label: '30d/s', value: 30 },
		{ label: '90d/s', value: 90 },
	];

	// Sort events chronologically
	let sortedEvents = $derived.by(() => {
		return [...events]
			.filter(e => e.timestamp)
			.sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
	});

	let timeRange = $derived.by(() => {
		if (sortedEvents.length === 0) return { start: new Date(), end: new Date(), span: 1 };
		const start = new Date(sortedEvents[0].timestamp);
		const end = new Date(sortedEvents[sortedEvents.length - 1].timestamp);
		const span = Math.max(end.getTime() - start.getTime(), 86400000); // min 1 day
		return { start, end, span };
	});

	let currentTime = $derived.by(() => {
		return new Date(timeRange.start.getTime() + playheadPosition * timeRange.span);
	});

	let currentDateLabel = $derived.by(() => {
		const d = currentTime;
		return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
	});

	// Which events are "visible" (before playhead)
	let visibleCount = $derived(
		sortedEvents.filter(e => new Date(e.timestamp).getTime() <= currentTime.getTime()).length
	);

	// Event dot positions on the track (0-1)
	let eventDots = $derived.by(() => {
		return sortedEvents.map(e => {
			const t = new Date(e.timestamp).getTime();
			const pos = (t - timeRange.start.getTime()) / timeRange.span;
			return { ...e, pos: Math.max(0, Math.min(1, pos)) };
		});
	});

	// Event type colors
	const typeColors: Record<string, string> = {
		military_action: '#ef4444', diplomatic: '#3b82f6', political: '#a855f7',
		economic: '#22c55e', disaster: '#f97316', scientific: '#06b6d4',
		cultural: '#ec4899', signal: '#6366f1', alert: '#ef4444',
		case_update: '#3b82f6', article: '#8b5cf6',
	};

	function eventColor(e: TimelineEvent): string {
		return typeColors[e.event_type || ''] || '#6b7280';
	}

	// Playback loop
	function tick(now: number) {
		if (!playing) return;
		const dt = (now - lastTick) / 1000; // seconds elapsed
		lastTick = now;

		const daysPerSec = speed;
		const msPerSec = daysPerSec * 86400000;
		const advance = (msPerSec * dt) / timeRange.span;
		playheadPosition = Math.min(1, playheadPosition + advance);

		onTimeChange?.(currentTime);

		if (playheadPosition >= 1) {
			playing = false;
		} else {
			animFrame = requestAnimationFrame(tick);
		}
	}

	function togglePlay() {
		if (playing) {
			playing = false;
			if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
		} else {
			if (playheadPosition >= 1) playheadPosition = 0;
			playing = true;
			lastTick = performance.now();
			animFrame = requestAnimationFrame(tick);
		}
	}

	function skipToStart() {
		playing = false;
		if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
		playheadPosition = 0;
		onTimeChange?.(currentTime);
	}

	function skipToEnd() {
		playing = false;
		if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
		playheadPosition = 1;
		onTimeChange?.(null); // null = show all
	}

	function onTrackMouseDown(e: MouseEvent) {
		isDragging = true;
		updatePositionFromMouse(e);
	}

	function onTrackMouseMove(e: MouseEvent) {
		if (!isDragging) return;
		updatePositionFromMouse(e);
	}

	function onTrackMouseUp() {
		isDragging = false;
	}

	function updatePositionFromMouse(e: MouseEvent) {
		if (!trackEl) return;
		const rect = trackEl.getBoundingClientRect();
		const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
		playheadPosition = x;
		onTimeChange?.(currentTime);
	}

	function cycleSpeed() {
		const idx = speeds.findIndex(s => s.value === speed);
		speed = speeds[(idx + 1) % speeds.length].value;
	}

	onDestroy(() => {
		if (animFrame) cancelAnimationFrame(animFrame);
	});
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="inline-flex items-center gap-1 px-2 py-1.5 rounded-lg bg-card/90 backdrop-blur-sm border border-border shadow-lg text-xs">
	<button onclick={skipToStart} class="text-muted-foreground hover:text-foreground" title="Start"><SkipBack size={10} /></button>
	<button onclick={togglePlay} class="px-0.5 {playing ? 'text-primary' : 'text-foreground'}" title={playing ? 'Pause' : 'Play'}>
		{#if playing}<Pause size={10} />{:else}<Play size={10} />{/if}
	</button>
	<button onclick={skipToEnd} class="text-muted-foreground hover:text-foreground" title="All"><SkipForward size={10} /></button>
	<button onclick={cycleSpeed} class="text-[9px] text-muted-foreground hover:text-foreground px-0.5" title="Speed">{speeds.find(s => s.value === speed)?.label}</button>
	<div class="w-px h-3 bg-border/50"></div>
	<!-- Track -->
	<div
		bind:this={trackEl}
		class="relative w-[140px] h-3 cursor-pointer"
		onmousedown={onTrackMouseDown}
		onmousemove={onTrackMouseMove}
		onmouseup={onTrackMouseUp}
		onmouseleave={onTrackMouseUp}
	>
		<div class="absolute inset-x-0 top-1/2 -translate-y-1/2 h-[2px] rounded-full bg-border/50"></div>
		<div class="absolute top-1/2 -translate-y-1/2 h-[2px] rounded-full bg-primary/50" style:width="{playheadPosition * 100}%"></div>
		{#each eventDots as dot}
			{@const isVisible = dot.pos <= playheadPosition}
			<button
				class="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 rounded-full {isVisible ? 'opacity-90' : 'opacity-20'}"
				style:left="{dot.pos * 100}%" style:width="3px" style:height="3px" style:background={eventColor(dot)}
				title="{dot.title}" onclick={(e) => { e.stopPropagation(); onEventClick?.(dot); }}
			></button>
		{/each}
		<div class="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 w-2 h-2 rounded-full bg-primary border border-card z-10" style:left="{playheadPosition * 100}%"></div>
	</div>
	<div class="w-px h-3 bg-border/50"></div>
	<span class="text-[9px] text-muted-foreground whitespace-nowrap">{currentDateLabel}</span>
</div>
