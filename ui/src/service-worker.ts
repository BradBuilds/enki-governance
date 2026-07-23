/// <reference types="@sveltejs/kit" />
/// <reference no-default-lib="true"/>
/// <reference lib="esnext" />
/// <reference lib="webworker" />

// SvelteKit auto-registers this file as the site's service worker.
// We use it for two things only:
//   1. Receive Web Push events when the hub approves an install
//   2. Open the right status URL when the notification is clicked
//
// We intentionally do NOT cache app shell assets — the marketing /
// status pages are small and Caddy already serves them gzipped. Caching
// would introduce a stale-content footgun for a low-traffic site.

const sw = self as unknown as ServiceWorkerGlobalScope;

sw.addEventListener('install', () => {
	sw.skipWaiting();
});

sw.addEventListener('activate', (event) => {
	event.waitUntil(sw.clients.claim());
});

sw.addEventListener('push', (event: PushEvent) => {
	let data: { title?: string; body?: string; url?: string } = {};
	try {
		data = event.data ? event.data.json() : {};
	} catch {
		// non-JSON payload — show generic notification
	}
	const title = data.title || 'Enki';
	const options: NotificationOptions = {
		body: data.body || '',
		icon: '/enki-symbol.png',
		badge: '/enki-symbol.png',
		data: { url: data.url || '/' },
		requireInteraction: true
	};
	event.waitUntil(sw.registration.showNotification(title, options));
});

sw.addEventListener('notificationclick', (event: NotificationEvent) => {
	event.notification.close();
	const url = (event.notification.data && event.notification.data.url) || '/';
	event.waitUntil(
		sw.clients.matchAll({ type: 'window' }).then(async (clientList) => {
			// Focus an existing tab if one is open at this URL
			for (const client of clientList) {
				if (client.url.endsWith(url) && 'focus' in client) {
					return (client as WindowClient).focus();
				}
			}
			if (sw.clients.openWindow) {
				return sw.clients.openWindow(url);
			}
			return null;
		})
	);
});

export {};
