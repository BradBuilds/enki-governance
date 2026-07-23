/**
 * Global state for the shared "+ Watch Topic" modal.
 * Any page can call watchTopicModal.open() to prompt the user and
 * await the resulting topic_id (or null if they cancel).
 */

type OnCreated = (topicId: string) => void;
type Prefill = { title?: string; keywords?: string[]; description?: string };

function createStore() {
	let isOpen = $state(false);
	let onCreated = $state<OnCreated | null>(null);
	let prefill = $state<Prefill | null>(null);

	return {
		get isOpen() {
			return isOpen;
		},
		get onCreated() {
			return onCreated;
		},
		get prefill() {
			return prefill;
		},
		open(cb?: OnCreated, initialData?: Prefill) {
			onCreated = cb ?? null;
			prefill = initialData ?? null;
			isOpen = true;
		},
		close() {
			isOpen = false;
			onCreated = null;
			prefill = null;
		},
	};
}

export const watchTopicModal = createStore();
