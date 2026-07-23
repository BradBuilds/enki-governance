/**
 * Client-side access logging store.
 * Batches access events and sends them to the backend every 10 seconds,
 * or on page unload via sendBeacon.
 *
 * Every data retrieval (chat query, story view, entity click) leaves a trace.
 */

export interface AccessEvent {
	action: string;
	resource_type: string;
	resource_id?: string;
	context: string;
	space: string;
	timestamp: string;
	metadata?: Record<string, any>;
}

class AccessLogState {
	private buffer: AccessEvent[] = [];
	private timer: ReturnType<typeof setInterval> | null = null;

	constructor() {
		if (typeof window !== 'undefined') {
			this.timer = setInterval(() => this.flush(), 10000);
			window.addEventListener('beforeunload', () => this.flushBeacon());
		}
	}

	log(event: Omit<AccessEvent, 'timestamp'>) {
		this.buffer.push({
			...event,
			timestamp: new Date().toISOString(),
		});
	}

	private async flush() {
		if (this.buffer.length === 0) return;
		const batch = this.buffer.splice(0);
		try {
			await fetch('/api/access-log', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ events: batch }),
			});
		} catch {
			// Backend unavailable — re-queue events
			this.buffer.unshift(...batch);
		}
	}

	private flushBeacon() {
		if (this.buffer.length === 0) return;
		const batch = this.buffer.splice(0);
		const blob = new Blob(
			[JSON.stringify({ events: batch })],
			{ type: 'application/json' }
		);
		navigator.sendBeacon('/api/access-log', blob);
	}
}

export const accessLog = new AccessLogState();
