<script lang="ts">
	import AppShell from '$lib/components/layout/AppShell.svelte';
	import WatchTopicModal from '$lib/components/topics/WatchTopicModal.svelte';
	import SelectionResearcher from '$lib/components/research/SelectionResearcher.svelte';
	import { setAuthContext } from '$lib/api/mock';

	let { children, data } = $props();

	$effect(() => {
		if (data?.auth) {
			setAuthContext(data.auth);
		}
	});
</script>

<svelte:head>
	<title>Enki Governance</title>
</svelte:head>

<AppShell user={data?.user}>
	{@render children()}
</AppShell>

<WatchTopicModal />
<SelectionResearcher />
