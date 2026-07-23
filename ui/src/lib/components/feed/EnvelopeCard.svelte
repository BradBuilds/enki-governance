<!--
  EnvelopeCard.svelte — generic card renderer for envelope-shaped nodes.

  Reads a card object returned by GET /envelope-cards (see
  services/api/src/routes/envelope_cards.py for the shape) and renders
  it without any per-signal-type branches. Layout decisions come from
  node_type / subtype / completeness tier; everything else is data.

  Designed to be drop-in additive: import alongside the existing card
  components, render any envelope-shaped node anywhere.
-->
<script lang="ts">
  import Star from 'lucide-svelte/icons/star';
  import { isBookmarked, bookmarkCategory, toggleBookmark } from '$lib/stores/bookmarks.svelte';

  type Card = {
    node_id: string;
    node_type?: string;
    subtype?: string;
    title?: string;
    headline?: string;
    narrative?: string;
    byline?: string | null;
    outlet?: string;
    url?: string | null;
    published?: string | null;
    thumbnail?: string | null;
    domain?: string;
    coordinates?: { lat: number; lon: number } | null;
    place_text?: string[];
    topics?: string[];
    entity_count?: number;
    completeness?: {
      score: number;
      tier: string;
      filled_slots: number;
      total_slots: number;
      gaps: string[];
    };
    updated_at?: string;
    active_set?: string;
  };

  let { card }: { card: Card } = $props();

  // ─── tier visuals — keyed by completenessvocab tier names ─────────
  const TIER_BADGE: Record<string, { color: string; label: string }> = {
    stub:     { color: "#6b7280", label: "Stub" },
    partial:  { color: "#f59e0b", label: "Partial" },
    solid:    { color: "#3b82f6", label: "Solid" },
    verified: { color: "#10b981", label: "Verified" },
  };

  // ─── domain accents — purely cosmetic, no domain logic ────────────
  const DOMAIN_ACCENT: Record<string, string> = {
    politics:    "#dc2626",
    defense:     "#7c2d12",
    weather:     "#0891b2",
    science:     "#0284c7",
    geo:         "#16a34a",
    tech:        "#7c3aed",
    energy:      "#d97706",
    diplomacy:   "#ca8a04",
    intelligence:"#1e40af",
    other:       "#6b7280",
  };

  // All derived values use safe fallbacks. The card is the invariant —
  // even an envelope with NO completeness data renders, with a 'stub'
  // badge and 0% score, instead of crashing.
  let tier = $derived(card?.completeness?.tier || "stub");
  let tierBadge = $derived(TIER_BADGE[tier] || TIER_BADGE.stub);
  let domainColor = $derived(DOMAIN_ACCENT[card?.domain || "other"] || DOMAIN_ACCENT.other);
  let scorePct = $derived(
    typeof card?.completeness?.score === "number"
      ? Math.round(card.completeness.score * 100)
      : 0
  );
  let typeLabel = $derived(card?.subtype
    ? `${card?.node_type || "node"}/${card.subtype}`
    : card?.node_type || "node");
  let timeAgo = $derived(formatTimeAgo(card?.published || card?.updated_at));

  let remembered = $derived(card?.node_id ? isBookmarked(card.node_id) : false);
  let rememberCategory = $derived(card?.node_id ? bookmarkCategory(card.node_id) : null);
  let busy = $state(false);

  async function handleRemember(e: MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (busy || !card?.node_id) return;
    busy = true;
    await toggleBookmark(card.node_id, 'save');
    busy = false;
  }

  // Coordinates may be partial — render only when both lat AND lon are
  // valid numbers. Prevents `.toFixed is not a function` crashes when
  // a connector ships partial geo data.
  let coordsLabel = $derived.by(() => {
    const c = card?.coordinates;
    if (!c) return null;
    const lat = c.lat;
    const lon = c.lon;
    if (typeof lat !== "number" || typeof lon !== "number") return null;
    return `${lat.toFixed(2)}, ${lon.toFixed(2)}`;
  });

  function formatTimeAgo(iso?: string | null): string {
    if (!iso) return "";
    const then = new Date(iso).getTime();
    if (isNaN(then)) return "";
    const diffSec = Math.floor((Date.now() - then) / 1000);
    if (diffSec < 60) return `${diffSec}s ago`;
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
    return `${Math.floor(diffSec / 86400)}d ago`;
  }
</script>

<article
  class="envelope-card"
  style="--accent: {domainColor}"
  data-researchable
  data-node-id={card.node_id}
>
  {#if card.thumbnail}
    <div class="thumb">
      <img src={card.thumbnail} alt="" loading="lazy" />
    </div>
  {/if}

  <header class="card-head">
    <span class="type-pill">{typeLabel}</span>
    <span class="tier-badge" style="background: {tierBadge.color}">
      {tierBadge.label} · {scorePct}%
    </span>
    <span class="time">{timeAgo}</span>
    <button
      class="remember-btn"
      class:remembered
      disabled={busy}
      onclick={handleRemember}
      title={remembered ? `Remembered (${rememberCategory || 'save'}) — click to forget` : 'Remember this'}
      aria-label={remembered ? 'Forget' : 'Remember'}
    >
      <Star size={14} fill={remembered ? 'currentColor' : 'none'} />
    </button>
  </header>

  <h3 class="title">
    {#if card.url}
      <a href={card.url} target="_blank" rel="noopener">{card.title || card.headline}</a>
    {:else}
      {card.title || card.headline || "Untitled"}
    {/if}
  </h3>

  {#if card.byline}
    <div class="byline">by {card.byline}</div>
  {/if}

  {#if card.narrative}
    <p class="narrative">{card.narrative}</p>
  {/if}

  <footer class="card-foot">
    {#if card.outlet}
      <span class="chip outlet">{card.outlet}</span>
    {/if}

    {#if coordsLabel}
      <span class="chip coords">{coordsLabel}</span>
    {/if}

    {#each (card.place_text || []).slice(0, 4) as place}
      <span class="chip place">{place}</span>
    {/each}

    {#each (card.topics || []).slice(0, 4) as topic}
      <span class="chip topic">#{topic}</span>
    {/each}

    {#if (card.entity_count || 0) > 0}
      <span class="chip entities">{card.entity_count} entities</span>
    {/if}
  </footer>

  {#if card.completeness && card.completeness.gaps && card.completeness.gaps.length > 0}
    <div class="gaps" title="Empty fields per the slot spec">
      gaps: {card.completeness.gaps.slice(0, 3).join(", ")}
      {#if card.completeness.gaps.length > 3}
        +{card.completeness.gaps.length - 3} more
      {/if}
    </div>
  {/if}
</article>

<style>
  .envelope-card {
    border: 1px solid #1f2937;
    border-left: 3px solid var(--accent);
    border-radius: 8px;
    background: #0f172a;
    color: #e5e7eb;
    padding: 12px 14px 10px;
    margin-bottom: 12px;
    font-family: -apple-system, system-ui, sans-serif;
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 12px;
  }
  .envelope-card > :not(.thumb) { grid-column: 2; }
  .envelope-card:has(> .thumb) > .thumb { grid-row: 1 / span 5; }
  .envelope-card:not(:has(> .thumb)) > * { grid-column: 1 / -1; }

  .thumb {
    width: 100px;
    height: 100px;
    overflow: hidden;
    border-radius: 6px;
    background: #1f2937;
  }
  .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }

  .card-head {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    margin-bottom: 4px;
  }
  .type-pill {
    background: #1e293b;
    color: #94a3b8;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 600;
    text-transform: lowercase;
  }
  .tier-badge {
    color: white;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 600;
  }
  .time { color: #64748b; margin-left: auto; }

  .remember-btn {
    background: transparent;
    border: 1px solid #334155;
    color: #64748b;
    border-radius: 6px;
    padding: 3px 5px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    transition: all 0.15s ease;
  }
  .remember-btn:hover:not(:disabled) {
    color: #fbbf24;
    border-color: #78350f;
  }
  .remember-btn.remembered {
    color: #fbbf24;
    border-color: #78350f;
    background: rgba(251, 191, 36, 0.08);
  }
  .remember-btn:disabled { opacity: 0.5; cursor: wait; }

  .title {
    font-size: 16px;
    font-weight: 600;
    line-height: 1.3;
    margin: 4px 0;
    color: #f1f5f9;
  }
  .title a { color: inherit; text-decoration: none; }
  .title a:hover { color: var(--accent); }

  .byline {
    font-size: 12px;
    color: #94a3b8;
    margin-bottom: 6px;
  }
  .narrative {
    font-size: 13px;
    line-height: 1.5;
    color: #cbd5e1;
    margin: 4px 0 8px;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .card-foot {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 6px;
  }
  .chip {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 12px;
    background: #1e293b;
    color: #94a3b8;
    border: 1px solid #334155;
  }
  .chip.outlet  { color: #fbbf24; border-color: #78350f; }
  .chip.coords  { color: #34d399; border-color: #064e3b; font-family: ui-monospace, monospace; }
  .chip.place   { color: #60a5fa; border-color: #1e40af; }
  .chip.topic   { color: #c084fc; border-color: #6b21a8; }
  .chip.entities{ color: #f472b6; border-color: #831843; }

  .gaps {
    font-size: 10px;
    color: #475569;
    margin-top: 6px;
    font-style: italic;
  }
</style>
