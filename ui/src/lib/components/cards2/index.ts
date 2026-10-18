/**
 * Card kit v2 — the canonical card components for the cyclical model.
 *
 * Each card takes a single ID prop and (optionally) prefetched data.
 * If no prefetched data, the card fetches from its corresponding
 * /api endpoint and renders a skeleton while loading.
 *
 * All 6 cards share CardShell which standardizes:
 *   - icon + title + subtitle + base60_address
 *   - 3 sizes: compact / default / detail
 *   - loading skeleton + error state
 *   - hover affordance + click-to-drill-in via `href`
 *
 * See the internal ui full plan.
 */
export { default as CardShell }        from './CardShell.svelte';
export { default as EntityCard }       from './EntityCard.svelte';
export { default as FactCard }         from './FactCard.svelte';
export { default as EventCard }        from './EventCard.svelte';
export { default as RefCard }          from './RefCard.svelte';
export { default as LibraryCard }      from './LibraryCard.svelte';
export { default as NeighborhoodCard } from './NeighborhoodCard.svelte';
export { default as PipelineCard }     from './PipelineCard.svelte';
export { default as SignalCard }       from './SignalCard.svelte';
