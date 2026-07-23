<script lang="ts">
	import Badge from '$lib/components/shared/Badge.svelte';
	import Smartphone from 'lucide-svelte/icons/smartphone';
	import Tablet from 'lucide-svelte/icons/tablet';
	import Monitor from 'lucide-svelte/icons/monitor';
	import Radio from 'lucide-svelte/icons/radio';
	import Camera from 'lucide-svelte/icons/camera';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Wifi from 'lucide-svelte/icons/wifi';
	import Bluetooth from 'lucide-svelte/icons/bluetooth';

	interface Props {
		device: any;
		variant?: 'compact' | 'standard' | 'expanded';
		onCommand?: (type: string) => void;
	}

	let { device, variant = 'standard', onCommand }: Props = $props();

	const typeIcons: Record<string, any> = {
		phone: Smartphone,
		tablet: Tablet,
		sensor: Radio,
		camera: Camera,
		default: Monitor,
	};

	const DeviceIcon = $derived(typeIcons[device?.device_type] ?? typeIcons.default);

	const statusVariant = $derived(
		device?.status === 'active' ? 'success' as const :
		device?.status === 'pending' ? 'medium' as const : 'neutral' as const
	);

	function timeAgo(ts: string): string {
		if (!ts) return 'never';
		const diff = Date.now() - new Date(ts).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 1) return 'just now';
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}

	const commands = [
		{ type: 'take_photo', label: 'Photo', icon: Camera },
		{ type: 'get_location', label: 'Location', icon: MapPin },
		{ type: 'wifi_scan', label: 'WiFi', icon: Wifi },
		{ type: 'bt_scan', label: 'Bluetooth', icon: Bluetooth },
	];
</script>

<div class="rounded-lg border border-border bg-card overflow-hidden hover:border-primary/20 transition-colors">
	{#if variant === 'compact'}
		<div class="flex items-center gap-2 px-3 py-2">
			<svelte:component this={DeviceIcon} size={14} class="text-muted-foreground shrink-0" />
			<span class="text-sm text-foreground truncate flex-1">{device?.display_name ?? device?.device_id ?? 'Device'}</span>
			<Badge text={device?.status ?? 'unknown'} variant={statusVariant} size="sm" />
			<span class="text-[10px] text-muted-foreground shrink-0">{timeAgo(device?.last_seen_at)}</span>
		</div>
	{:else}
		<div class="px-4 py-3">
			<div class="flex items-start gap-3">
				<div class="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center shrink-0">
					<svelte:component this={DeviceIcon} size={20} class="text-primary" />
				</div>
				<div class="flex-1 min-w-0">
					<div class="flex items-center gap-2">
						<span class="text-sm font-medium text-foreground">{device?.display_name ?? device?.device_id ?? 'Device'}</span>
						<Badge text={device?.status ?? 'unknown'} variant={statusVariant} size="sm" />
					</div>
					<div class="flex items-center gap-3 mt-1 text-xs text-muted-foreground">
						{#if device?.platform}
							<span class="capitalize">{device.platform}</span>
						{/if}
						<span>Last seen {timeAgo(device?.last_seen_at)}</span>
						{#if device?.signals_count}
							<span>{device.signals_count.toLocaleString()} signals</span>
						{/if}
					</div>
				</div>
			</div>

			{#if variant === 'expanded' && onCommand}
				<div class="mt-3 pt-3 border-t border-border/50">
					<p class="text-xs text-muted-foreground mb-2">Commands</p>
					<div class="grid grid-cols-4 gap-2">
						{#each commands as cmd}
							<button
								onclick={() => onCommand(cmd.type)}
								class="flex flex-col items-center gap-1 py-2 rounded-lg hover:bg-muted transition-colors text-muted-foreground hover:text-foreground"
							>
								<svelte:component this={cmd.icon} size={16} />
								<span class="text-[10px]">{cmd.label}</span>
							</button>
						{/each}
					</div>
				</div>
			{/if}
		</div>
	{/if}
</div>
