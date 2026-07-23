<script lang="ts">
	// Floating "Research" button that appears when the user selects text
	// inside a card, article body, or summary region. Mounted once at
	// the app-shell level; listens globally for selectionchange.
	//
	// Scope gate: we only offer the button when the selection lives
	// inside an element marked `data-researchable` (or a descendant of
	// one). This keeps the button out of chrome/buttons/forms.
	//
	// Click → routes through /api/research/from-selection, which dispatches
	// via core.research.selection_router: gateway-collapse for entity-
	// shaped selections, NER for multi-entity (Phase 3), topic admission
	// for no-entity cases (Phase 3). On success, renders a result chip
	// with a link to the resolved entity's dossier.

	import { researchFromSelection, type SelectionResearchResult } from '$lib/stores/research_activity.svelte';

	let x = $state(0);
	let y = $state(0);
	let visible = $state(false);
	let selectedText = $state('');
	let contextText = $state('');
	let parentNodeId = $state<string | null>(null);
	let loading = $state(false);
	let result = $state<SelectionResearchResult | null>(null);

	function hide() {
		visible = false;
		selectedText = '';
		parentNodeId = null;
		result = null;
	}

	function parentEntityId(): number | undefined {
		if (!parentNodeId || !parentNodeId.startsWith('entity:')) return undefined;
		const n = parseInt(parentNodeId.slice('entity:'.length), 10);
		return Number.isFinite(n) ? n : undefined;
	}

	function findResearchableHost(node: Node | null): HTMLElement | null {
		let el: Node | null = node;
		while (el) {
			if (el instanceof HTMLElement) {
				if (el.dataset && 'researchable' in el.dataset) return el;
			}
			el = el.parentNode;
		}
		return null;
	}

	function onSelectionChange() {
		// If a result chip is showing, leave it alone — operator is
		// reading it. The dismiss button or a fresh selection on top of
		// the chip will clear it.
		if (result) return;

		const sel = window.getSelection();
		if (!sel || sel.isCollapsed || !sel.rangeCount) {
			if (visible && !loading) hide();
			return;
		}
		const text = sel.toString().trim();
		// Min length 2, max 200 — block silly one-char selections and
		// gigantic paragraph grabs that would make for noisy research.
		if (text.length < 2 || text.length > 200) {
			if (visible && !loading) hide();
			return;
		}
		const range = sel.getRangeAt(0);
		const host = findResearchableHost(range.commonAncestorContainer);
		if (!host) {
			if (visible && !loading) hide();
			return;
		}

		// Derive context: up to 600 chars around the selection inside
		// the host. Good enough for disambiguating "Apple" from "Apple Inc.".
		const hostText = host.innerText || '';
		contextText = hostText.slice(0, 600);
		parentNodeId = host.dataset.nodeId || null;

		// Position the button just above the selection's bounding box
		const rect = range.getBoundingClientRect();
		// Default: centered above the selection
		x = Math.max(16, Math.min(window.innerWidth - 180, rect.left + rect.width / 2 - 70));
		y = Math.max(64, rect.top - 44);
		selectedText = text;
		visible = true;
	}

	async function onResearch() {
		if (!selectedText) return;
		loading = true;
		try {
			const res = await researchFromSelection(selectedText, {
				context: contextText,
				parent_entity_id: parentEntityId(),
			});
			result = res;
			// Clear the browser selection so the button doesn't bounce back,
			// but KEEP the popup visible so the result chip is readable.
			window.getSelection()?.removeAllRanges();
		} finally {
			loading = false;
		}
	}

	function dismiss() {
		hide();
	}

	function onDocClick(e: MouseEvent) {
		if (!visible) return;
		const t = e.target as HTMLElement;
		if (t && t.closest('[data-selection-researcher]')) return;
		// Allow the browser to settle the new selection first
		setTimeout(onSelectionChange, 0);
	}

	$effect(() => {
		document.addEventListener('selectionchange', onSelectionChange);
		document.addEventListener('mousedown', onDocClick);
		return () => {
			document.removeEventListener('selectionchange', onSelectionChange);
			document.removeEventListener('mousedown', onDocClick);
		};
	});
</script>

{#if visible}
	<div
		data-selection-researcher
		class="selection-btn"
		style="left: {x}px; top: {y}px;"
	>
		{#if result}
			<!-- Result chip — shown after the gateway has resolved the
			     selection. Renders differently per kind. -->
			{#if result.kind === 'entity' && result.entity_id}
				<div class="result-chip" class:result-new={result.is_new}>
					<span class="result-arrow">→</span>
					<a href={`/entity/${result.entity_id}`} class="result-link">
						{result.canonical_name || selectedText}
					</a>
					<span class="result-meta">
						{result.is_new ? 'new · investigating…' : result.canonical_type || 'entity'}
					</span>
					{#if result.edge_created}
						<span class="result-edge" title="A 'mentions' edge was created from this dossier to the resolved entity">
							edge ✓
						</span>
					{/if}
					<button class="dismiss" type="button" onclick={dismiss} aria-label="Dismiss">×</button>
				</div>
			{:else if result.kind === 'multi' && result.candidates && result.candidates.length > 0}
				<div class="result-chooser">
					<div class="result-chooser-head">
						<span>{result.candidates.length} entities found — pick one:</span>
						<button class="dismiss" type="button" onclick={dismiss} aria-label="Dismiss">×</button>
					</div>
					<ul class="result-chooser-list">
						{#each result.candidates as c}
							<li>
								<a href={`/entity/${c.entity_id}`} class="chooser-item">
									<span class="chooser-name">{c.canonical_name || c.name_raw}</span>
									<span class="chooser-meta">
										{c.canonical_type || 'entity'}
										{#if c.is_new}<span class="chooser-new">· new</span>{/if}
										{#if c.edge_created}<span class="chooser-edge">· edge ✓</span>{/if}
									</span>
								</a>
							</li>
						{/each}
					</ul>
				</div>
			{:else}
				<div class="result-chip result-error">
					<span class="result-meta">
						{result.message || 'no entity detected'}
					</span>
					<button class="dismiss" type="button" onclick={dismiss} aria-label="Dismiss">×</button>
				</div>
			{/if}
		{:else}
			<button type="button" onclick={onResearch} disabled={loading}>
				{#if loading}
					<span class="spinner"></span>
					Researching…
				{:else}
					<svg
						width="14"
						height="14"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2.2"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<circle cx="11" cy="11" r="7" />
						<path d="m21 21-4.35-4.35" />
					</svg>
					Research "{selectedText.length > 28
						? selectedText.slice(0, 28) + '…'
						: selectedText}"
				{/if}
			</button>
		{/if}
	</div>
{/if}

<style>
	.selection-btn {
		position: fixed;
		z-index: 9999;
		pointer-events: auto;
	}
	.selection-btn button {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 6px 12px;
		font-size: 12px;
		font-weight: 600;
		color: #fff;
		background: linear-gradient(180deg, #6d28d9 0%, #4c1d95 100%);
		border: 1px solid rgba(255, 255, 255, 0.15);
		border-radius: 999px;
		box-shadow:
			0 4px 14px rgba(0, 0, 0, 0.35),
			0 0 0 1px rgba(109, 40, 217, 0.3);
		cursor: pointer;
		letter-spacing: 0.01em;
		transition: transform 120ms ease, box-shadow 120ms ease;
	}
	.selection-btn button:hover:not(:disabled) {
		transform: translateY(-1px);
		box-shadow:
			0 6px 18px rgba(0, 0, 0, 0.45),
			0 0 0 1px rgba(139, 92, 246, 0.5);
	}
	.selection-btn button:disabled {
		opacity: 0.8;
		cursor: wait;
	}
	.spinner {
		width: 12px;
		height: 12px;
		border: 2px solid rgba(255, 255, 255, 0.3);
		border-top-color: #fff;
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
	.result-chip {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		padding: 6px 10px 6px 12px;
		font-size: 12px;
		color: #fff;
		background: linear-gradient(180deg, #065f46 0%, #064e3b 100%);
		border: 1px solid rgba(255, 255, 255, 0.18);
		border-radius: 999px;
		box-shadow:
			0 4px 14px rgba(0, 0, 0, 0.35),
			0 0 0 1px rgba(16, 185, 129, 0.35);
		letter-spacing: 0.01em;
	}
	.result-chip.result-new {
		background: linear-gradient(180deg, #1e40af 0%, #172554 100%);
		box-shadow:
			0 4px 14px rgba(0, 0, 0, 0.35),
			0 0 0 1px rgba(59, 130, 246, 0.4);
	}
	.result-chip.result-error {
		background: linear-gradient(180deg, #7f1d1d 0%, #450a0a 100%);
		box-shadow:
			0 4px 14px rgba(0, 0, 0, 0.35),
			0 0 0 1px rgba(239, 68, 68, 0.35);
	}
	.result-arrow {
		font-size: 14px;
		opacity: 0.85;
	}
	.result-link {
		color: #fff;
		font-weight: 600;
		text-decoration: none;
		max-width: 220px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.result-link:hover {
		text-decoration: underline;
	}
	.result-meta {
		font-size: 10px;
		opacity: 0.75;
		font-weight: 500;
	}
	.result-edge {
		font-size: 10px;
		padding: 1px 6px;
		background: rgba(255, 255, 255, 0.15);
		border-radius: 999px;
		font-weight: 500;
	}
	.dismiss {
		all: unset;
		cursor: pointer;
		font-size: 14px;
		line-height: 1;
		padding: 2px 4px;
		opacity: 0.6;
		border-radius: 4px;
	}
	.dismiss:hover {
		opacity: 1;
		background: rgba(255, 255, 255, 0.1);
	}
	.result-chooser {
		display: flex;
		flex-direction: column;
		gap: 4px;
		min-width: 280px;
		max-width: 380px;
		padding: 8px 10px;
		font-size: 12px;
		color: #fff;
		background: linear-gradient(180deg, #1e3a8a 0%, #0f172a 100%);
		border: 1px solid rgba(255, 255, 255, 0.18);
		border-radius: 8px;
		box-shadow:
			0 8px 24px rgba(0, 0, 0, 0.5),
			0 0 0 1px rgba(59, 130, 246, 0.35);
	}
	.result-chooser-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 8px;
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		opacity: 0.75;
		padding-bottom: 4px;
		border-bottom: 1px solid rgba(255, 255, 255, 0.1);
	}
	.result-chooser-list {
		list-style: none;
		padding: 0;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 1px;
	}
	.chooser-item {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 12px;
		padding: 6px 8px;
		color: #fff;
		text-decoration: none;
		border-radius: 4px;
		transition: background 100ms ease;
	}
	.chooser-item:hover {
		background: rgba(255, 255, 255, 0.1);
	}
	.chooser-name {
		font-weight: 500;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		max-width: 220px;
	}
	.chooser-meta {
		font-size: 10px;
		opacity: 0.65;
		white-space: nowrap;
	}
	.chooser-new {
		color: #93c5fd;
	}
	.chooser-edge {
		color: #86efac;
	}
</style>
