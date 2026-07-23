<script lang="ts">
	import Lock from 'lucide-svelte/icons/lock';
	import ArrowLeft from 'lucide-svelte/icons/arrow-left';
	import Ticket from 'lucide-svelte/icons/ticket';
	import Check from 'lucide-svelte/icons/check';

	let code = $state('');
	let error = $state('');
	let loading = $state(false);
	let validated = $state(false);
	let codeInfo = $state<{ tier: string; max_users: number } | null>(null);

	async function validateCode() {
		error = '';
		loading = true;

		try {
			const res = await fetch('/api/invite/validate', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ code: code.trim() }),
			});
			const data = await res.json();

			if (data.valid) {
				validated = true;
				codeInfo = { tier: data.tier, max_users: data.max_users };
			} else {
				const reasons: Record<string, string> = {
					INVALID_CODE: 'Invalid invite code. Check your code and try again.',
					ALREADY_REDEEMED: 'This code has already been used.',
					CODE_REVOKED: 'This code has been revoked.',
					CODE_EXPIRED: 'This code has expired.',
					EMAIL_MISMATCH: 'This code is reserved for a different email.',
				};
				error = reasons[data.reason] || 'Invalid code.';
			}
		} catch {
			error = 'Could not validate code. Please try again.';
		}

		loading = false;
	}

	function formatCode(value: string): string {
		// Auto-format: ENKI-XXXX-XXXX
		const clean = value.replace(/[^A-Z0-9]/gi, '').toUpperCase();
		if (clean.length <= 4) return clean.startsWith('ENKI') ? clean : clean;
		if (clean.startsWith('ENKI')) {
			const rest = clean.slice(4);
			if (rest.length <= 4) return `ENKI-${rest}`;
			return `ENKI-${rest.slice(0, 4)}-${rest.slice(4, 8)}`;
		}
		if (clean.length <= 4) return clean;
		return `${clean.slice(0, 4)}-${clean.slice(4, 8)}`;
	}
</script>

<svelte:head>
	<title>Sign Up — Enki Governance</title>
</svelte:head>

<div class="min-h-screen bg-background flex items-center justify-center p-4">
	<div class="w-full max-w-sm">
		<a href="/" class="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground mb-8 transition-colors">
			<ArrowLeft class="w-4 h-4" />
			Back to example.com
		</a>

		<div class="rounded-xl border border-border bg-card p-8">
			<div class="flex items-center gap-3 mb-6">
				<img src="/enki-symbol.png" alt="Enki" class="h-12" />
				<div>
					<p class="font-semibold text-foreground">Enki</p>
					<p class="text-xs text-muted-foreground">Invite-only access</p>
				</div>
			</div>

			{#if !validated}
				<p class="text-sm text-muted-foreground mb-6">
					Enter your invite code to create an account. Don't have one?
					<a href="/#early-access" class="text-primary hover:underline">Request access</a>.
				</p>

				<form onsubmit={(e) => { e.preventDefault(); validateCode(); }} class="space-y-4">
					<div>
						<label for="code" class="text-xs text-muted-foreground uppercase tracking-wide">Invite Code</label>
						<input
							id="code"
							type="text"
							bind:value={code}
							oninput={(e) => {
								const target = e.target as HTMLInputElement;
								code = formatCode(target.value);
							}}
							placeholder="ENKI-XXXX-XXXX"
							autocomplete="off"
							spellcheck="false"
							class="w-full mt-1 bg-muted rounded-md px-3 py-2.5 text-sm text-foreground font-mono tracking-widest text-center outline-none focus:ring-2 focus:ring-ring uppercase"
						/>
					</div>

					{#if error}
						<p class="text-sm text-destructive">{error}</p>
					{/if}

					<button
						type="submit"
						disabled={loading || code.replace(/[-\s]/g, '').length < 8}
						class="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-md bg-primary text-primary-foreground font-medium hover:bg-primary/90 disabled:opacity-50 transition-colors"
					>
						<Ticket class="w-4 h-4" />
						{loading ? 'Validating...' : 'Validate Code'}
					</button>
				</form>
			{:else}
				<div class="flex items-center gap-2 mb-4 p-3 rounded-lg bg-enki-green/10 border border-enki-green/20">
					<Check class="w-5 h-5 text-enki-green" />
					<div>
						<p class="text-sm font-medium text-foreground">Code accepted</p>
						<p class="text-xs text-muted-foreground">
							{codeInfo?.tier === 'free' ? 'Free' : codeInfo?.tier} tier — up to {codeInfo?.max_users} users
						</p>
					</div>
				</div>

				<p class="text-sm text-muted-foreground mb-6">
					Create your account to activate your Enki node.
				</p>

				<a
					href="/auth/sign-up?code={encodeURIComponent(code)}"
					data-sveltekit-reload
					class="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-md bg-primary text-primary-foreground font-medium hover:bg-primary/90 transition-colors"
				>
					<Lock class="w-4 h-4" />
					Create Account
				</a>
			{/if}
		</div>

		<p class="text-center text-xs text-muted-foreground mt-6">
			Already have an account?
			<a href="/login" class="text-primary hover:underline">Sign in</a>
		</p>
	</div>
</div>
