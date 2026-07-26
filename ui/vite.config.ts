import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		tailwindcss(),
		sveltekit(),
	],
	optimizeDeps: {
		// satellite.js v7 is NOT plain ESM: it ships a WASM/pthreads build
		// (satellite.js/wasm-build/pthreads-release/index.js) that imports
		// node:worker_threads. Pre-bundling it made `npm run build` fail in
		// rollup — which is what broke the production UI build. The propagator
		// loads it via a dynamic import at runtime, so leave it out of the
		// dep optimizer and let the browser resolve it.
		exclude: ['satellite.js'],
	},
	build: {
		rollupOptions: {
			// Never try to walk into the WASM/pthreads entry point.
			external: [/satellite\.js\/wasm-build\//],
		},
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
