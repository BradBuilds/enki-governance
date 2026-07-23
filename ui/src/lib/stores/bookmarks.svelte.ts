// Shared bookmark state. The feed loads the full id→category map once
// on mount so every card can render filled/empty stars without a
// per-card round trip.

const state = $state<{ ids: Record<string, string>; loaded: boolean }>({
	ids: {},
	loaded: false,
});

export function getBookmarkIds(): Record<string, string> {
	return state.ids;
}

export function isBookmarked(nodeId: string): boolean {
	return nodeId in state.ids;
}

export function bookmarkCategory(nodeId: string): string | null {
	return state.ids[nodeId] || null;
}

export async function loadBookmarks() {
	if (state.loaded) return;
	try {
		const res = await fetch('/api/bookmarks/ids');
		if (res.ok) {
			const data = await res.json();
			state.ids = data.ids || {};
		}
	} catch (e) {
		console.error('loadBookmarks failed', e);
	}
	state.loaded = true;
}

export async function toggleBookmark(
	nodeId: string,
	category: string = 'save',
): Promise<boolean> {
	const currentlyBookmarked = nodeId in state.ids;
	if (currentlyBookmarked) {
		try {
			const res = await fetch(`/api/bookmarks/${encodeURIComponent(nodeId)}`, {
				method: 'DELETE',
			});
			if (res.ok) {
				const next = { ...state.ids };
				delete next[nodeId];
				state.ids = next;
				return false;
			}
		} catch (e) {
			console.error('delete bookmark failed', e);
		}
		return true;
	}
	try {
		const res = await fetch('/api/bookmarks', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ node_id: nodeId, category }),
		});
		if (res.ok) {
			state.ids = { ...state.ids, [nodeId]: category };
			return true;
		}
	} catch (e) {
		console.error('create bookmark failed', e);
	}
	return false;
}
