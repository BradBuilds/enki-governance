import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		tailwindcss(),
		sveltekit(),
	],
	optimizeDeps: {
		// satellite.js v7 is clean ESM (no WASM / top-level await — the old
		// exclusion was stale). Pre-bundle it so the propagator's dynamic
		// import resolves reliably instead of being optimized on-demand, which
		// is fragile behind a reverse proxy / tunnel.
		include: ['satellite.js'],
	},
	server: {
		// Leading dot matches all subdomains — covers node1, alice, bob, etc.
		allowedHosts: ['example.com', '.example.com', 'localhost'],
		proxy: {
			'/api': {
				// VITE_API_PROXY is server-side (docker: http://api:8000, local: http://localhost:8000)
				// Backend routers mount at root; strip /api here so UI can use /api/* uniformly.
				target: process.env.VITE_API_PROXY || 'http://localhost:8000',
				changeOrigin: true,
				rewrite: (path) => path.replace(/^\/api/, ''),
			},
		},
	},
});
