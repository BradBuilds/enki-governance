<script lang="ts">
	import Lock from 'lucide-svelte/icons/lock';
	import UserPlus from 'lucide-svelte/icons/user-plus';
	import AlertCircle from 'lucide-svelte/icons/alert-circle';
	import Loader from 'lucide-svelte/icons/loader-2';
	import ShieldCheck from 'lucide-svelte/icons/shield-check';
	import Copy from 'lucide-svelte/icons/copy';
	import Check from 'lucide-svelte/icons/check';
	import QRCode from 'qrcode';

	let { data } = $props();

	type Mode = 'login' | 'signup';
	type SignupStep = 'init' | 'qr';

	let mode = $state<Mode>(data.needsSetup ? 'signup' : 'login');
	let signupStep = $state<SignupStep>('init');

	let email = $state('');
	let displayName = $state('');
	let stationName = $state('');
	let error = $state('');
	let loading = $state(false);

	let code = $state('');
	let useBackupCode = $state(false);

	let signupToken = $state('');
	let totpSecret = $state('');
	let otpauthUrl = $state('');
	let qrDataUrl = $state('');
	let backupCodes = $state<string[]>([]);
	let copiedSecret = $state(false);
	let copiedBackups = $state(false);
	let backupsAcknowledged = $state(false);

	async function handleSignupInit() {
		error = '';
		if (!email.trim() || !email.includes('@')) {
			error = 'Valid email required';
			return;
		}
		if (!displayName.trim()) {
			error = 'Name required';
			return;
		}
		loading = true;
		try {
			const res = await fetch('/api/auth/local/signup', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					email: email.trim(),
					display_name: displayName.trim(),
					tenant_name: stationName.trim() || null,
				}),
			});
			if (!res.ok) {
				const d = await res.json().catch(() => ({}));
				error = d.detail || 'Signup failed';
				loading = false;
				return;
			}
			const r = await res.json();
			signupToken = r.signup_token;
			totpSecret = r.secret;
			otpauthUrl = r.otpauth_url;
			backupCodes = r.backup_codes;
			qrDataUrl = await QRCode.toDataURL(r.otpauth_url, {
				width: 220,
				margin: 1,
				color: { dark: '#000000', light: '#ffffff' },
			});
			signupStep = 'qr';
			loading = false;
		} catch {
			error = 'Cannot reach server. Is Enki running?';
			loading = false;
		}
	}

	async function handleSignupVerify() {
		error = '';
		if (!backupsAcknowledged) {
			error = 'Save your backup codes first — they cannot be shown again';
			return;
		}
		loading = true;
		try {
			const res = await fetch('/api/auth/local/signup/verify', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					signup_token: signupToken,
					code: code.trim().replace(/\s+/g, ''),
				}),
			});
			if (!res.ok) {
				const d = await res.json().catch(() => ({}));
				error = d.detail || 'Invalid code';
				loading = false;
				return;
			}
			const r = await res.json();
			document.cookie = `enki_session=${r.access_token}; path=/; max-age=${48 * 3600}; SameSite=Lax`;
			window.location.href = '/intelligence';
		} catch {
			error = 'Cannot reach server.';
			loading = false;
		}
	}

	async function handleLogin() {
		error = '';
		loading = true;
		try {
			const res = await fetch('/api/auth/local/login', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					email: email.trim(),
					code: code.trim().replace(/\s+/g, ''),
					is_backup_code: useBackupCode,
				}),
			});
			if (!res.ok) {
				const d = await res.json().catch(() => ({}));
				error = d.detail || 'Invalid email or code';
				loading = false;
				return;
			}
			const r = await res.json();
			document.cookie = `enki_session=${r.access_token}; path=/; max-age=${48 * 3600}; SameSite=Lax`;
			window.location.href = '/intelligence';
		} catch {
			error = 'Cannot reach server.';
			loading = false;
		}
	}

	async function copyToClipboard(text: string, which: 'secret' | 'backups') {
		try {
			await navigator.clipboard.writeText(text);
			if (which === 'secret') {
				copiedSecret = true;
				setTimeout(() => (copiedSecret = false), 2000);
			} else {
				copiedBackups = true;
				setTimeout(() => (copiedBackups = false), 2000);
			}
		} catch {}
	}

	function handleSubmit(e: Event) {
		e.preventDefault();
		if (mode === 'login') handleLogin();
		else if (mode === 'signup' && signupStep === 'init') handleSignupInit();
		else if (mode === 'signup' && signupStep === 'qr') handleSignupVerify();
	}

	const titles = {
		'login': 'Sign in',
		'signup-init': 'Create account',
		'signup-qr': 'Scan to enroll',
	};
	const pageTitle = $derived(
		`${titles[mode === 'signup' ? `signup-${signupStep}` : 'login'] ?? 'Login'} — Enki Governance`
	);
</script>

<svelte:head>
	<title>{pageTitle}</title>
</svelte:head>

<div class="min-h-screen bg-background flex items-center justify-center p-4">
	<div class="w-full max-w-sm">
		<div class="rounded-xl border border-border bg-card p-8">
			<div class="flex items-center gap-3 mb-6">
				<img src="/enki-symbol.png" alt="Enki" class="h-12" />
				<div>
					<p class="font-semibold text-foreground">Enki</p>
					<p class="text-xs text-muted-foreground">
						{#if mode === 'signup' && signupStep === 'init'}Create your admin account
						{:else if mode === 'signup' && signupStep === 'qr'}Scan with your authenticator app
						{:else}Sign in with your authenticator{/if}
					</p>
				</div>
			</div>

			{#if error}
				<div class="flex items-start gap-2 p-3 mb-4 rounded-lg bg-destructive/10 border border-destructive/20">
					<AlertCircle class="w-4 h-4 text-destructive shrink-0 mt-0.5" />
					<p class="text-sm text-destructive">{error}</p>
				</div>
			{/if}

			<!-- ─── SIGNUP STEP 1: email + name ─────────────────────────── -->
			{#if mode === 'signup' && signupStep === 'init'}
				<p class="text-sm text-muted-foreground mb-6">
					Welcome to your Enki station. Your authenticator app is your credential — no password required.
				</p>
				<form onsubmit={handleSubmit} class="space-y-4">
					<div>
						<label for="displayName" class="block text-sm font-medium text-foreground mb-1.5">Name</label>
						<input id="displayName" type="text" bind:value={displayName}
							placeholder="Your name" required
							class="w-full px-3 py-2 rounded-md border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary" />
					</div>
					<div>
						<label for="email" class="block text-sm font-medium text-foreground mb-1.5">Email</label>
						<input id="email" type="email" bind:value={email}
							placeholder="you@example.com" required autocomplete="email"
							class="w-full px-3 py-2 rounded-md border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary" />
					</div>
					<div>
						<label for="stationName" class="block text-sm font-medium text-foreground mb-1.5">
							Station Name <span class="text-muted-foreground font-normal">(optional)</span>
						</label>
						<input id="stationName" type="text" bind:value={stationName}
							placeholder="My Enki Station"
							class="w-full px-3 py-2 rounded-md border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary" />
					</div>
					<button type="submit" disabled={loading}
						class="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-md bg-primary text-primary-foreground font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
						{#if loading}
							<Loader class="w-4 h-4 animate-spin" /> Generating QR…
						{:else}
							<UserPlus class="w-4 h-4" /> Continue
						{/if}
					</button>
				</form>

			<!-- ─── SIGNUP STEP 2: QR + verify ──────────────────────────── -->
			{:else if mode === 'signup' && signupStep === 'qr'}
				<p class="text-sm text-muted-foreground mb-4">
					Scan with <span class="text-foreground">Google Authenticator</span>,
					<span class="text-foreground">1Password</span>, <span class="text-foreground">Bitwarden</span>,
					<span class="text-foreground">Authy</span>, or any RFC&nbsp;6238 app.
				</p>

				<div class="flex flex-col items-center mb-4">
					<div class="rounded-lg bg-white p-3 border border-border">
						{#if qrDataUrl}
							<img src={qrDataUrl} alt="TOTP enrollment QR code" class="block" width="220" height="220" />
						{/if}
					</div>
					<a href={otpauthUrl} class="mt-2 text-xs text-primary hover:underline">
						📱 On mobile? Tap to auto-add
					</a>
				</div>

				<div class="mb-4 rounded-lg border border-border bg-background/50 p-3">
					<div class="flex items-center justify-between mb-1">
						<span class="text-[10px] uppercase tracking-wider text-muted-foreground">Secret (manual entry)</span>
						<button type="button" onclick={() => copyToClipboard(totpSecret, 'secret')}
							class="text-muted-foreground hover:text-primary">
							{#if copiedSecret}<Check class="w-3.5 h-3.5 text-emerald-400" />{:else}<Copy class="w-3.5 h-3.5" />{/if}
						</button>
					</div>
					<code class="block font-mono text-xs break-all text-foreground">{totpSecret}</code>
				</div>

				<div class="mb-4 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
					<div class="flex items-center justify-between mb-2">
						<span class="text-[10px] uppercase tracking-wider text-amber-300 font-medium">
							Backup codes — save these now
						</span>
						<button type="button" onclick={() => copyToClipboard(backupCodes.join('\n'), 'backups')}
							class="text-muted-foreground hover:text-primary">
							{#if copiedBackups}<Check class="w-3.5 h-3.5 text-emerald-400" />{:else}<Copy class="w-3.5 h-3.5" />{/if}
						</button>
					</div>
					<p class="text-[11px] text-muted-foreground mb-2">
						Each works ONCE if you lose your phone. These are your ONLY recovery — there is no password reset.
					</p>
					<div class="grid grid-cols-2 gap-1 text-xs font-mono">
						{#each backupCodes as bc}
							<div class="px-2 py-1 rounded bg-background text-foreground">{bc}</div>
						{/each}
					</div>
					<label class="mt-3 flex items-center gap-2 text-xs text-muted-foreground cursor-pointer">
						<input type="checkbox" bind:checked={backupsAcknowledged} class="rounded" />
						I have saved these codes
					</label>
				</div>

				<form onsubmit={handleSubmit} class="space-y-3">
					<div>
						<label for="verifyCode" class="block text-sm font-medium text-foreground mb-1.5">
							Enter the current 6-digit code from your app
						</label>
						<input id="verifyCode" type="text" bind:value={code}
							placeholder="123 456" inputmode="numeric" autocomplete="one-time-code"
							required disabled={!backupsAcknowledged}
							class="w-full px-3 py-2 rounded-md border border-border bg-background text-foreground font-mono text-lg tracking-widest text-center placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 disabled:opacity-50" />
					</div>
					<button type="submit" disabled={loading || !backupsAcknowledged}
						class="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-md bg-primary text-primary-foreground font-medium hover:bg-primary/90 disabled:opacity-50">
						{#if loading}<Loader class="w-4 h-4 animate-spin" /> Verifying…
						{:else}<ShieldCheck class="w-4 h-4" /> Create account{/if}
					</button>
				</form>

			<!-- ─── LOGIN ────────────────────────────────────────────────── -->
			{:else}
				<form onsubmit={handleSubmit} class="space-y-4">
					<div>
						<label for="email" class="block text-sm font-medium text-foreground mb-1.5">Email</label>
						<input id="email" type="email" bind:value={email}
							placeholder="you@example.com" required autocomplete="email"
							class="w-full px-3 py-2 rounded-md border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary" />
					</div>
					<div>
						<label for="code" class="block text-sm font-medium text-foreground mb-1.5">
							{useBackupCode ? 'Backup code' : '6-digit code from authenticator'}
						</label>
						<input id="code" type="text" bind:value={code}
							placeholder={useBackupCode ? 'XXXXXXXX' : '123 456'}
							inputmode={useBackupCode ? 'text' : 'numeric'}
							autocomplete="one-time-code" required
							class="w-full px-3 py-2 rounded-md border border-border bg-background text-foreground font-mono text-lg tracking-widest text-center placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary" />
					</div>
					<button type="submit" disabled={loading}
						class="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-md bg-primary text-primary-foreground font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
						{#if loading}
							<Loader class="w-4 h-4 animate-spin" /> Signing in…
						{:else}
							<Lock class="w-4 h-4" /> Sign in
						{/if}
					</button>
				</form>
				<div class="mt-4 text-center">
					<button onclick={() => { useBackupCode = !useBackupCode; code = ''; error = ''; }}
						class="text-xs text-muted-foreground hover:text-primary transition-colors">
						{useBackupCode ? 'Use 6-digit code instead' : 'Lost your phone? Use a backup code'}
					</button>
				</div>
			{/if}
		</div>

		<p class="text-center text-xs text-muted-foreground mt-6">
			Enki Governance is open source.<br />
			<a href="https://github.com/BradBuilds/enki-governance" target="_blank" rel="noopener" class="text-primary hover:underline">Deploy your own instance</a>
		</p>
	</div>
</div>
