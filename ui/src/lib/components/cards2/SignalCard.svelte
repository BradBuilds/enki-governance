<script lang="ts">
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Library from 'lucide-svelte/icons/library';
	import Newspaper from 'lucide-svelte/icons/newspaper';
	import Radio from 'lucide-svelte/icons/radio';
	import Plane from 'lucide-svelte/icons/plane';
	import Ship from 'lucide-svelte/icons/ship';
	import Activity from 'lucide-svelte/icons/activity';
	import Globe from 'lucide-svelte/icons/globe';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
	import TrendingUp from 'lucide-svelte/icons/trending-up';
	import User from 'lucide-svelte/icons/user';
	import Building from 'lucide-svelte/icons/building';
	import Flag from 'lucide-svelte/icons/flag';
	import Landmark from 'lucide-svelte/icons/landmark';

	interface CardTopic {
		topic_id: number;
		title: string;
	}

	interface FeedEntity {
		entity_id: number;
		name: string;
		type: string;
	}

	interface FeedCard {
		signal_id: string;
		signal_type: string;
		severity?: string;
		timestamp: string | null;
		headline?: string | null;
		summary?: string | null;
		source?: string | null;
		source_url?: string | null;
		thumbnail_url?: string | null;
		latitude?: number | null;
		longitude?: number | null;
		badges?: Array<{ label: string; color?: string }>;
		metrics?: Record<string, any>;
		domain?: string;
		region?: string;
		category?: string;
		language?: string;
		topics?: CardTopic[];
		matched_libraries?: Array<{ library_id: number; title: string; address?: string }>;
		entities?: { subjects?: FeedEntity[]; sources?: FeedEntity[] };
	}

	interface Props {
		card: FeedCard;
	}

	let { card }: Props = $props();

	const DomainIcon = $derived.by(() => {
		const d = card.domain || '';
		if (d === 'content') return Newspaper;
		if (d === 'environmental') return AlertTriangle;
		if (d === 'geospatial') {
			if (card.signal_type?.includes('aircraft')) return Plane;
			if (card.signal_type?.includes('vessel')) return Ship;
			return Globe;
		}
		if (d === 'financial') return TrendingUp;
		if (d === 'security') return AlertTriangle;
		if (d === 'infrastructure' || d === 'device') return Activity;
		return Radio;
	});

	const sevColor = $derived.by(() => {
		const s = (card.severity || '').toLowerCase();
		if (s === 'critical') return 'border-l-red-500';
		if (s === 'high') return 'border-l-orange-500';
		if (s === 'medium' || s === 'moderate') return 'border-l-amber-500';
		return 'border-l-transparent';
	});

	const badgeColor = (color?: string): string => {
		const c = color || 'slate';
		const map: Record<string, string> = {
			red: 'bg-red-500/10 text-red-400 border-red-500/20',
			amber: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
			emerald: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
			blue: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
			cyan: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20',
			purple: 'bg-purple-500/10 text-purple-400 border-purple-500/20',
			slate: 'bg-muted text-muted-foreground border-border',
		};
		return map[c] || map.slate;
	};

	function timeAgo(ts: string | null): string {
		if (!ts) return '';
		const diff = Date.now() - new Date(ts).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 1) return 'now';
		if (mins < 60) return `${mins}m`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h`;
		return `${Math.floor(hrs / 24)}d`;
	}

	const libChips = $derived(card.matched_libraries ?? []);

	function stripHtml(s: string | null | undefined): string {
		if (!s) return '';
		// Drop tags, decode common entities, collapse whitespace.
		return s
			.replace(/<[^>]*>/g, ' ')
			.replace(/&nbsp;/g, ' ')
			.replace(/&amp;/g, '&')
			.replace(/&lt;/g, '<')
			.replace(/&gt;/g, '>')
			.replace(/&quot;/g, '"')
			.replace(/&#39;/g, "'")
			.replace(/\s+/g, ' ')
			.trim();
	}

	const cleanHeadline = $derived(stripHtml(card.headline));
	const cleanSummary = $derived(stripHtml(card.summary));

	// Entity chips — subjects are who/what is in the story; sources are publishers
	// being cited (mass_media canonical type).
	const subjects = $derived(card.entities?.subjects ?? []);
	const sourceEntities = $derived(card.entities?.sources ?? []);

	function entityIcon(type: string) {
		const t = (type || '').toLowerCase();
		if (t === 'person') return User;
		if (t === 'country' || t === 'region') return Flag;
		if (t === 'government_agency' || t === 'military_unit' || t === 'facility' || t === 'military_facility') return Landmark;
		return Building;
	}

	function entityColor(type: string): string {
		const t = (type || '').toLowerCase();
		if (t === 'person') return 'bg-purple-500/10 text-purple-300 border-purple-500/20';
		if (t === 'country' || t === 'region') return 'bg-blue-500/10 text-blue-300 border-blue-500/20';
		if (t === 'organization') return 'bg-amber-500/10 text-amber-300 border-amber-500/20';
		if (t === 'government_agency') return 'bg-red-500/10 text-red-300 border-red-500/20';
		if (t === 'facility' || t === 'military_facility') return 'bg-cyan-500/10 text-cyan-300 border-cyan-500/20';
		return 'bg-muted text-muted-foreground border-border';
	}
</script>

<article class="rounded-xl border border-border bg-card hover:border-primary/40 transition-colors border-l-4 {sevColor}">
	<div class="p-4">
		<!-- Header row: icon + source + time -->
		<div class="flex items-center gap-2 text-xs text-muted-foreground mb-2">
			<DomainIcon size={14} class="text-primary shrink-0" />
			{#if card.source}
				<span class="truncate font-medium text-foreground/80">{card.source}</span>
			{/if}
			{#if card.region}
				<span class="text-muted-foreground">·</span>
				<span class="truncate">{card.region}</span>
			{/if}
			<span class="flex-1"></span>
			<time class="shrink-0">{timeAgo(card.timestamp)}</time>
		</div>

		<!-- Main content row: thumbnail + text -->
		<div class="flex gap-3">
			{#if card.thumbnail_url}
				<img
					src={card.thumbnail_url}
					alt=""
					loading="lazy"
					class="w-20 h-20 sm:w-24 sm:h-24 rounded-md object-cover shrink-0 bg-muted"
					onerror={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none'; }}
				/>
			{/if}
			<div class="flex-1 min-w-0">
				{#if cleanHeadline}
					{#if card.source_url}
						<a
							href={card.source_url}
							target="_blank"
							rel="noopener noreferrer"
							class="text-sm font-semibold text-foreground hover:text-primary transition-colors line-clamp-2"
						>
							{cleanHeadline}
						</a>
					{:else}
						<p class="text-sm font-semibold text-foreground line-clamp-2">{cleanHeadline}</p>
					{/if}
				{/if}
				{#if cleanSummary}
					<p class="text-xs text-muted-foreground mt-1 line-clamp-2">{cleanSummary}</p>
				{/if}
			</div>
		</div>

		<!-- Entity row: subjects (who/what is in the story) + cited sources -->
		{#if subjects.length > 0 || sourceEntities.length > 0}
			<div class="flex flex-wrap items-center gap-1 mt-2.5 text-[10px]">
				{#each subjects.slice(0, 8) as ent (ent.entity_id)}
					{@const EIcon = entityIcon(ent.type)}
					<a
						href={`/entity/${ent.entity_id}`}
						class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border {entityColor(ent.type)} hover:opacity-80 transition-opacity"
						title={ent.type.replace(/_/g, ' ')}
					>
						<EIcon size={9} />
						<span class="truncate max-w-[140px]">{ent.name}</span>
					</a>
				{/each}
				{#each sourceEntities.slice(0, 3) as ent (ent.entity_id)}
					<a
						href={`/entity/${ent.entity_id}`}
						class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border bg-foreground/5 text-foreground/70 border-foreground/10 hover:bg-foreground/10 transition-colors"
						title="cited source"
					>
						<Newspaper size={9} />
						<span class="truncate max-w-[120px]">{ent.name}</span>
					</a>
				{/each}
				{#if subjects.length > 8}
					<span class="text-muted-foreground/60">+{subjects.length - 8} more</span>
				{/if}
			</div>
		{/if}

		<!-- Footer row: badges, library chips, metrics, location -->
		<div class="flex flex-wrap items-center gap-1.5 mt-3 text-[11px]">
			{#each (card.badges ?? []) as badge}
				<span class="inline-flex items-center px-1.5 py-0.5 rounded border {badgeColor(badge.color)}">
					{badge.label}
				</span>
			{/each}

			{#each libChips as lib}
				<a
					href={`/library/${lib.library_id}`}
					class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border bg-primary/10 text-primary border-primary/20 hover:bg-primary/20 transition-colors"
					title={lib.address ? `library ${lib.address}` : ''}
				>
					<Library size={10} />
					<span class="truncate max-w-[120px]">{lib.title}</span>
				</a>
			{/each}

			{#if card.metrics && Object.keys(card.metrics).length > 0}
				{#each Object.entries(card.metrics).slice(0, 3) as [key, value]}
					{#if value !== null && value !== undefined}
						<span class="text-muted-foreground">
							<span class="text-muted-foreground/70">{key.replace(/_/g, ' ')}:</span>
							<span class="text-foreground/80 font-mono">{value}</span>
						</span>
					{/if}
				{/each}
			{/if}

			{#if card.latitude != null && card.longitude != null}
				<a
					href={`/map?lat=${card.latitude}&lon=${card.longitude}&z=8`}
					class="inline-flex items-center gap-1 text-muted-foreground hover:text-primary transition-colors"
				>
					<MapPin size={10} />
					<span class="font-mono">{card.latitude.toFixed(2)}, {card.longitude.toFixed(2)}</span>
				</a>
			{/if}

			<span class="flex-1"></span>

			{#if card.source_url}
				<a
					href={card.source_url}
					target="_blank"
					rel="noopener noreferrer"
					class="inline-flex items-center gap-1 text-muted-foreground hover:text-primary transition-colors"
					aria-label="Open external link"
				>
					<ExternalLink size={10} /> Open
				</a>
			{/if}
		</div>
	</div>
</article>
