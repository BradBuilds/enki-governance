<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';

	let { entityId, focalName = '', lensQueryFirst = '' } = $props<{
		entityId: number;
		focalName?: string;
		lensQueryFirst?: string;
	}>();

	let canvas = $state<HTMLCanvasElement>();
	let data = $state<{ facts: any[]; preds: [string, number][] }>({ facts: [], preds: [] });
	let active = $state('ALL');
	let sel = $state<any>(null);
	let loaded = $state(false);

	const PAL = ['#4bc4bf', '#6f9bd6', '#9d8fc4', '#c1a05f', '#cf8f9a', '#74b49a', '#b79a63', '#8aa6c9'];
	const colMap: Record<string, string> = {};
	const short = (s: string, n = 13) => (s && s.length > n ? s.slice(0, n - 1) + '…' : s || '?');
	const arrow = (dd: string) => (dd === 'out' ? '→' : dd === 'in' ? '←' : '↔');

	// canvas / physics working state (non-reactive)
	let nodes: any[] = [], ry = 0.3, rx = 0.28, dragging = false, lx = 0, ly = 0;
	let auto = true, ACC = '#4bc4bf', W = 0, H = 0, DPR = 1, proj: any[] = [];
	let hovering = false, overNode = false, spin = 0.0032;
	let ctx: CanvasRenderingContext2D | null = null;

	function buildNodes() {
		const fs = active === 'ALL' ? data.facts.slice(0, 38) : data.facts.filter((f) => f.edge === active);
		nodes = fs.map((f) => ({
			f, x: (Math.random() - 0.5) * 2, y: (Math.random() - 0.5) * 2, z: (Math.random() - 0.5) * 2,
			vx: 0, vy: 0, vz: 0
		}));
		sel = nodes[0] ? nodes[0].f : null;
	}
	function setActive(k: string) { active = k; buildNodes(); }

	onMount(() => {
		if (!canvas) return;
		ctx = canvas.getContext('2d');
		DPR = Math.min(2, window.devicePixelRatio || 1);
		auto = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;

		function resize() {
			W = canvas.clientWidth || 640;
			H = canvas.clientHeight || 420;
			canvas.width = W * DPR; canvas.height = H * DPR;
			ctx!.setTransform(DPR, 0, 0, DPR, 0, 0);
		}
		const ro = new ResizeObserver(resize);
		ro.observe(canvas);

		fetch(`/api/entities/${entityId}/fact_graph`)
			.then((r) => (r.ok ? r.json() : { facts: [], preds: [] }))
			.then((j) => {
				data = { facts: j.facts || [], preds: j.preds || [] };
				data.preds.forEach(([e], i) => (colMap[e] = PAL[i % PAL.length]));
				loaded = true;
				resize();
				buildNodes();
			})
			.catch(() => (loaded = true));

		function physics() {
			const RS = 0.92, KR = 0.0072, KC = 0.03;
			for (let i = 0; i < nodes.length; i++) {
				const a = nodes[i];
				for (let j = i + 1; j < nodes.length; j++) {
					const b = nodes[j];
					let dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
					const d2 = dx * dx + dy * dy + dz * dz + 0.04, f = KR / d2, d = Math.sqrt(d2);
					dx /= d; dy /= d; dz /= d;
					a.vx += dx * f; a.vy += dy * f; a.vz += dz * f;
					b.vx -= dx * f; b.vy -= dy * f; b.vz -= dz * f;
				}
				const r = Math.sqrt(a.x * a.x + a.y * a.y + a.z * a.z) + 1e-4, s = KC * (RS - r);
				a.vx += (a.x / r) * s; a.vy += (a.y / r) * s; a.vz += (a.z / r) * s;
			}
			for (const a of nodes) { a.vx *= 0.82; a.vy *= 0.82; a.vz *= 0.82; a.x += a.vx; a.y += a.vy; a.z += a.vz; }
		}
		let raf = 0;
		function frame() {
			if (ctx && W && H) {
				physics();
				// ease toward a target speed: near-stop over a node, gentle drift over the graph, full spin otherwise
				const target = dragging ? 0 : overNode ? 0.0002 : hovering ? 0.0009 : 0.0032;
				spin += (target - spin) * 0.08;
				if (auto) ry += spin;
				const cx = W / 2, cy = H / 2, F = 3.4;
				const R = Math.max(54, Math.min(W * 0.245, H * 0.32));
				proj = nodes.map((n) => {
					const x = n.x, y = n.y, z = n.z;
					const x1 = x * Math.cos(ry) + z * Math.sin(ry), z1 = -x * Math.sin(ry) + z * Math.cos(ry);
					const y2 = y * Math.cos(rx) - z1 * Math.sin(rx), z2 = y * Math.sin(rx) + z1 * Math.cos(rx);
					const s = F / (F - z2 * 0.55);
					return { n, sx: cx + x1 * R * s, sy: cy + y2 * R * s, depth: z2, s };
				}).sort((a, b) => a.depth - b.depth);
				ctx.clearRect(0, 0, W, H);
				const selF = sel;
				for (const p of proj) {
					const c = active === 'ALL' ? (colMap[p.n.f.edge] || '#888') : ACC;
					const isSel = p.n.f === selF;
					ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(p.sx, p.sy);
					ctx.strokeStyle = c; ctx.globalAlpha = isSel ? 0.95 : 0.14 + 0.3 * ((p.depth + 1) / 2);
					ctx.lineWidth = Math.max(0.5, (1 + Math.min(3, p.n.f.corrob)) * p.s * 0.5); ctx.stroke();
				}
				ctx.globalAlpha = 1;
				for (const p of proj) {
					const c = active === 'ALL' ? (colMap[p.n.f.edge] || '#888') : ACC;
					const rad = (4 + Math.min(8, p.n.f.corrob * 1.1)) * p.s;
					ctx.save(); ctx.shadowColor = c; ctx.shadowBlur = 9 * p.s;
					ctx.beginPath(); ctx.arc(p.sx, p.sy, rad, 0, 6.2832);
					ctx.globalAlpha = 0.62 + 0.38 * ((p.depth + 1) / 2); ctx.fillStyle = c; ctx.fill();
					ctx.restore(); ctx.globalAlpha = 1;
					if (p.depth > 0.02 || p.n.f === sel) {
						ctx.font = `${Math.round(11 * Math.max(0.85, p.s))}px ui-sans-serif, sans-serif`;
						ctx.fillStyle = `rgba(150,164,184,${0.35 + 0.6 * ((p.depth + 1) / 2)})`;
						const rt = p.sx >= cx; ctx.textAlign = rt ? 'left' : 'right';
						ctx.fillText(short(p.n.f.other), p.sx + (rt ? rad + 5 : -(rad + 5)), p.sy + 4);
					}
				}
				ctx.beginPath(); ctx.arc(cx, cy, 24, 0, 6.2832); ctx.fillStyle = '#3b82f6'; ctx.fill();
				ctx.fillStyle = '#fff'; ctx.font = 'bold 12px ui-sans-serif'; ctx.textAlign = 'center';
				ctx.fillText((focalName || '').split(' ').slice(-1)[0] || '?', cx, cy + 4);
			}
			raf = requestAnimationFrame(frame);
		}
		frame();

		function hit(mx: number, my: number) {
			let best: any = null, bd = 1e9;
			for (const p of proj) {
				const dx = p.sx - mx, dy = p.sy - my, dd = dx * dx + dy * dy, rr = (15 * p.s) * (15 * p.s);
				if (dd < rr && dd < bd) { bd = dd; best = p; }
			}
			return best;
		}
		const onMove = (e: MouseEvent) => {
			const r = canvas.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
			if (dragging) {
				ry += (e.clientX - lx) * 0.008; rx = Math.max(-1.3, Math.min(1.3, rx + (e.clientY - ly) * 0.006));
				lx = e.clientX; ly = e.clientY; return;
			}
			const p = hit(mx, my); overNode = !!p; if (p) sel = p.n.f; canvas.style.cursor = p ? 'pointer' : 'grab';
		};
		const onEnter = () => { hovering = true; };
		const onLeave = () => { hovering = false; overNode = false; };
		const onDown = (e: MouseEvent) => { dragging = true; lx = e.clientX; ly = e.clientY; };
		const onUp = () => { dragging = false; };
		const onClick = (e: MouseEvent) => {
			const r = canvas.getBoundingClientRect(), p = hit(e.clientX - r.left, e.clientY - r.top);
			if (p) goto(`/entity/${p.n.f.other_id}${lensQueryFirst}`);
		};
		canvas.addEventListener('mousemove', onMove);
		canvas.addEventListener('mouseenter', onEnter);
		canvas.addEventListener('mouseleave', onLeave);
		canvas.addEventListener('mousedown', onDown);
		canvas.addEventListener('click', onClick);
		window.addEventListener('mouseup', onUp);

		return () => {
			cancelAnimationFrame(raf); ro.disconnect();
			canvas.removeEventListener('mousemove', onMove);
			canvas.removeEventListener('mouseenter', onEnter);
			canvas.removeEventListener('mouseleave', onLeave);
			canvas.removeEventListener('mousedown', onDown);
			canvas.removeEventListener('click', onClick);
			window.removeEventListener('mouseup', onUp);
		};
	});
</script>

{#if loaded && data.facts.length === 0}
	<div class="text-xs text-muted-foreground/60 py-3">No typed relationships extracted yet for this entity.</div>
{:else}
	<div class="mb-3">
		<div class="flex flex-wrap gap-1.5 mb-2">
			<button class="fgtag" class:on={active === 'ALL'} onclick={() => setActive('ALL')}>All facts <span class="n">{data.facts.length}</span></button>
			{#each data.preds.slice(0, 16) as [e, n] (e)}
				<button class="fgtag" class:on={active === e} onclick={() => setActive(e)}>{e} <span class="n">{n}</span></button>
			{/each}
		</div>
		<canvas bind:this={canvas} class="w-full rounded-lg" style="height:360px; touch-action:none; cursor:grab; background:transparent"></canvas>
		<div class="px-1 text-xs" style="min-height:52px">
			{#if sel}
				<div class="font-medium text-foreground">
					<span class="text-muted-foreground/50">{arrow(sel.dir)}</span>
					<span class="font-mono text-[11px] text-primary">{sel.edge}</span>
					{sel.other}
					{#if sel.corrob > 1}<span class="text-muted-foreground/60 text-[10px]">· {sel.corrob} sources</span>{/if}
				</div>
				{#if sel.quote}
					{#if sel.doc}
						<a href={`/document/${sel.doc}${sel.stmt ? `?statement=${sel.stmt}` : ''}`}
							class="group block mt-1 text-[11px] leading-snug text-muted-foreground/70 hover:text-foreground">
							<span class="italic line-clamp-2">“{sel.quote}”</span>
							<span class="text-primary/80 group-hover:text-primary">read source document →</span>
						</a>
					{:else}
						<div class="mt-1 text-[11px] leading-snug text-muted-foreground/70 italic line-clamp-2">“{sel.quote}”</div>
					{/if}
				{/if}
			{:else}
				<div class="text-muted-foreground/50 pt-2">Pick a relationship above to follow it as its own web · drag to rotate · click a node to open it.</div>
			{/if}
		</div>
	</div>
{/if}

<style>
	.fgtag{border:1px solid hsl(var(--border));background:transparent;color:hsl(var(--muted-foreground));font:inherit;font-size:11.5px;font-weight:560;padding:4px 10px;border-radius:20px;cursor:pointer;white-space:nowrap;transition:all .12s}
	.fgtag:hover{color:hsl(var(--foreground))}
	.fgtag.on{color:hsl(var(--primary));border-color:hsl(var(--primary));background:hsl(var(--primary)/0.10)}
	.fgtag .n{opacity:.55;font-variant-numeric:tabular-nums}
</style>
