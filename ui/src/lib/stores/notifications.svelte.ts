/**
 * Tracks badge counts for nav items.
 * Polled periodically from the backend or updated by chat/API responses.
 */

class NotificationState {
	alertsNew = $state(0);
	casesOpen = $state(0);
	connectorsError = $state(0);
	processingPending = $state(0);

	get unread() {
		return this.alertsNew + this.connectorsError + this.processingPending;
	}

	get total() {
		return this.alertsNew + this.casesOpen + this.connectorsError + this.processingPending;
	}

	update(data: {
		alertsNew?: number;
		casesOpen?: number;
		connectorsError?: number;
		processingPending?: number;
	}) {
		if (data.alertsNew !== undefined) this.alertsNew = data.alertsNew;
		if (data.casesOpen !== undefined) this.casesOpen = data.casesOpen;
		if (data.connectorsError !== undefined) this.connectorsError = data.connectorsError;
		if (data.processingPending !== undefined) this.processingPending = data.processingPending;
	}
}

export const notifications = new NotificationState();
