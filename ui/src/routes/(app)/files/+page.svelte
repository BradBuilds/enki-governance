<script lang="ts">
	import { onMount } from 'svelte';
	import { api } from '$lib/api/mock';
	import Badge from '$lib/components/shared/Badge.svelte';
	import LoadingSkeleton from '$lib/components/shared/LoadingSkeleton.svelte';
	import EmptyState from '$lib/components/shared/EmptyState.svelte';
	import Pagination from '$lib/components/shared/Pagination.svelte';
	import FolderOpen from 'lucide-svelte/icons/folder-open';
	import File from 'lucide-svelte/icons/file';
	import FileText from 'lucide-svelte/icons/file-text';
	import FileImage from 'lucide-svelte/icons/file-image';
	import FileVideo from 'lucide-svelte/icons/file-video';
	import FileAudio from 'lucide-svelte/icons/file-audio';
	import FileCode from 'lucide-svelte/icons/file-code';
	import FileArchive from 'lucide-svelte/icons/file-archive';
	import Search from 'lucide-svelte/icons/search';
	import Smartphone from 'lucide-svelte/icons/smartphone';
	import Monitor from 'lucide-svelte/icons/monitor';
	import Download from 'lucide-svelte/icons/download';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
	import Clock from 'lucide-svelte/icons/clock';
	import HardDrive from 'lucide-svelte/icons/hard-drive';

	let loading = $state(true);
	let files = $state<any[]>([]);
	let devices = $state<any[]>([]);
	let searchQuery = $state('');
	let selectedDevice = $state('');
	let selectedType = $state('');
	let sortBy = $state<'modified' | 'size' | 'name'>('modified');
	let currentPage = $state(1);
	let totalFiles = $state(0);
	let alerts = $state<any[]>([]);
	const pageSize = 50;

	const fileTypeIcons: Record<string, any> = {
		image: FileImage, video: FileVideo, audio: FileAudio,
		document: FileText, code: FileCode, archive: FileArchive,
	};

	const extToType: Record<string, string> = {
		'.jpg': 'image', '.jpeg': 'image', '.png': 'image', '.gif': 'image', '.webp': 'image',
		'.mp4': 'video', '.avi': 'video', '.mov': 'video', '.mkv': 'video',
		'.mp3': 'audio', '.wav': 'audio', '.flac': 'audio',
		'.pdf': 'document', '.doc': 'document', '.docx': 'document', '.xlsx': 'document', '.csv': 'document', '.txt': 'document',
		'.py': 'code', '.js': 'code', '.ts': 'code', '.html': 'code', '.css': 'code', '.json': 'code',
		'.zip': 'archive', '.rar': 'archive', '.tar': 'archive', '.gz': 'archive',
		'.exe': 'executable', '.bat': 'executable', '.msi': 'executable',
	};

	function getFileType(ext: string): string {
		return extToType[ext?.toLowerCase()] || 'other';
	}

	function getFileIcon(ext: string): any {
		return fileTypeIcons[getFileType(ext)] || File;
	}

	function formatSize(bytes: number): string {
		if (!bytes) return '—';
		if (bytes < 1024) return `${bytes} B`;
		if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
		if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
		return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
	}

	function timeAgo(ts: number | string): string {
		if (!ts) return '—';
		const ms = typeof ts === 'number' ? ts * 1000 : new Date(ts).getTime();
		const diff = Date.now() - ms;
		const mins = Math.floor(diff / 60000);
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}

	onMount(async () => {
		// Load file index signals and file change/alert signals
		try {
			const [fileData, alertData, deviceData] = await Promise.all([
				fetch('/api/signals?signal_type=signal.device.file_index&limit=5', { headers: { 'Content-Type': 'application/json' } }).then(r => r.json()).catch(() => ({ signals: [] })),
				fetch('/api/signals?signal_type=signal.device.file_alert&limit=20', { headers: { 'Content-Type': 'application/json' } }).then(r => r.json()).catch(() => ({ signals: [] })),
				api.listDevices().catch(() => ({ devices: [] })),
			]);

			// Extract files from file_index signals (each signal contains a files array)
			const allFiles: any[] = [];
			for (const sig of fileData.signals || []) {
				const payload = sig.payload || {};
				const fileList = payload.files || payload.entries || [];
				const deviceId = sig.device_id;
				for (const f of fileList) {
					allFiles.push({
						...f,
						device_id: deviceId,
						signal_id: sig.signal_id,
						indexed_at: sig.timestamp,
					});
				}
			}
			files = allFiles;
			totalFiles = allFiles.length;

			// Extract alerts
			alerts = (alertData.signals || []).map((s: any) => ({
				...s.payload,
				device_id: s.device_id,
				timestamp: s.timestamp,
			}));

			// Devices
			devices = (deviceData.devices || []).filter((d: any) => d.status === 'active');
		} catch {}
		loading = false;
	});

	// Filtered + sorted + paginated files
	const filteredFiles = $derived.by(() => {
		let result = files;

		if (searchQuery.trim()) {
			const q = searchQuery.toLowerCase();
			result = result.filter(f =>
				(f.path || f.name || '').toLowerCase().includes(q) ||
				(f.ext || '').toLowerCase().includes(q)
			);
		}

		if (selectedDevice) {
			result = result.filter(f => f.device_id === selectedDevice);
		}

		if (selectedType) {
			result = result.filter(f => getFileType(f.ext) === selectedType);
		}

		// Sort
		if (sortBy === 'modified') {
			result = [...result].sort((a, b) => (b.modified || 0) - (a.modified || 0));
		} else if (sortBy === 'size') {
			result = [...result].sort((a, b) => (b.size || 0) - (a.size || 0));
		} else {
			result = [...result].sort((a, b) => (a.name || '').localeCompare(b.name || ''));
		}

		return result;
	});

	const pagedFiles = $derived(filteredFiles.slice((currentPage - 1) * pageSize, currentPage * pageSize));

	const fileTypeStats = $derived.by(() => {
		const counts: Record<string, number> = {};
		for (const f of files) {
			const t = getFileType(f.ext);
			counts[t] = (counts[t] || 0) + 1;
		}
		return Object.entries(counts).sort((a, b) => b[1] - a[1]);
	});

	async function requestFile(filePath: string, deviceId: string) {
		try {
			await fetch(`/api/devices/${deviceId}/commands`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ type: 'get_file', params: { path: filePath } }),
			});
		} catch {}
	}
</script>

<div class="max-w-6xl mx-auto space-y-4">
	<div class="flex items-center justify-between">
		<div>
			<h1 class="text-xl font-semibold text-foreground flex items-center gap-2">
				<FolderOpen class="w-5 h-5" /> Files
			</h1>
			<p class="text-sm text-muted-foreground mt-0.5">{totalFiles.toLocaleString()} files indexed across {devices.length} device{devices.length !== 1 ? 's' : ''}</p>
		</div>
	</div>

	{#if loading}
		<LoadingSkeleton type="table" lines={8} />
	{:else}
		<!-- Alerts -->
		{#if alerts.length > 0}
			<div class="rounded-lg border border-yellow-500/30 bg-yellow-500/5 p-3">
				<div class="flex items-center gap-2 text-sm text-yellow-400 font-medium mb-2">
					<AlertTriangle size={16} /> File Alerts ({alerts.length})
				</div>
				{#each alerts.slice(0, 3) as alert}
					<div class="text-xs text-foreground ml-6 mb-1">
						{alert.alert_type || alert.type}: {alert.description || alert.message || JSON.stringify(alert).slice(0, 100)}
						<span class="text-muted-foreground ml-1">· {alert.device_id} · {timeAgo(alert.timestamp)}</span>
					</div>
				{/each}
			</div>
		{/if}

		<!-- Stats -->
		{#if fileTypeStats.length > 0}
			<div class="flex gap-2 flex-wrap">
				{#each fileTypeStats.slice(0, 8) as [type, count]}
					<button
						onclick={() => selectedType = selectedType === type ? '' : type}
						class="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs transition-colors
							{selectedType === type ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:text-foreground'}"
					>
						<svelte:component this={fileTypeIcons[type] || File} size={12} />
						{type} ({count.toLocaleString()})
					</button>
				{/each}
			</div>
		{/if}

		<!-- Filters -->
		<div class="flex gap-2 flex-wrap">
			<div class="flex-1 min-w-[200px] relative">
				<Search size={14} class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
				<input type="text" bind:value={searchQuery} placeholder="Search by filename or path..."
					class="w-full pl-9 pr-3 py-2 rounded-lg border border-border bg-card text-foreground text-sm" />
			</div>
			<select bind:value={selectedDevice} class="px-3 py-2 rounded-lg border border-border bg-card text-foreground text-sm">
				<option value="">All devices</option>
				{#each devices as d}
					<option value={d.device_id}>{d.display_name || d.device_id}</option>
				{/each}
			</select>
			<select bind:value={sortBy} class="px-3 py-2 rounded-lg border border-border bg-card text-foreground text-sm">
				<option value="modified">Recent first</option>
				<option value="size">Largest first</option>
				<option value="name">Name A-Z</option>
			</select>
		</div>

		<!-- File list -->
		{#if pagedFiles.length > 0}
			<div class="rounded-lg border border-border bg-card overflow-hidden">
				<div class="overflow-x-auto">
					<table class="w-full text-sm">
						<thead>
							<tr class="border-b border-border bg-muted/30">
								<th class="text-left p-2 text-xs text-muted-foreground font-medium">File</th>
								<th class="text-left p-2 text-xs text-muted-foreground font-medium hidden md:table-cell">Path</th>
								<th class="text-right p-2 text-xs text-muted-foreground font-medium w-20">Size</th>
								<th class="text-right p-2 text-xs text-muted-foreground font-medium w-24 hidden sm:table-cell">Modified</th>
								<th class="text-center p-2 text-xs text-muted-foreground font-medium w-20 hidden lg:table-cell">Device</th>
								<th class="text-center p-2 w-16"></th>
							</tr>
						</thead>
						<tbody>
							{#each pagedFiles as f}
								{@const Icon = getFileIcon(f.ext)}
								{@const fType = getFileType(f.ext)}
								<tr class="border-b border-border/50 hover:bg-muted/30">
									<td class="p-2">
										<div class="flex items-center gap-2">
											<svelte:component this={Icon} size={16} class="{fType === 'executable' ? 'text-red-400' : 'text-muted-foreground'}" />
											<span class="text-foreground truncate max-w-[200px]">{f.name || f.path?.split(/[/\\]/).pop() || '?'}</span>
											{#if fType === 'executable'}
												<Badge text="exe" variant="critical" size="sm" />
											{/if}
											{#if f.is_hidden}
												<Badge text="hidden" variant="neutral" size="sm" />
											{/if}
										</div>
									</td>
									<td class="p-2 text-xs text-muted-foreground font-mono truncate max-w-[300px] hidden md:table-cell">
										{f.path || '—'}
									</td>
									<td class="p-2 text-right text-xs text-muted-foreground">{formatSize(f.size)}</td>
									<td class="p-2 text-right text-xs text-muted-foreground hidden sm:table-cell">{timeAgo(f.modified)}</td>
									<td class="p-2 text-center hidden lg:table-cell">
										<span class="text-[10px] text-muted-foreground">{(f.device_id || '').slice(0, 10)}</span>
									</td>
									<td class="p-2 text-center">
										<button
											onclick={() => requestFile(f.path, f.device_id)}
											class="p-1 rounded hover:bg-muted text-muted-foreground hover:text-primary transition-colors"
											title="Request file from device"
										>
											<Download size={14} />
										</button>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</div>

			<Pagination page={currentPage} pageSize={pageSize} total={filteredFiles.length} onPageChange={(p) => currentPage = p} />
		{:else if files.length > 0}
			<EmptyState icon={Search} title="No matching files" description="Try adjusting your search or filters." />
		{:else}
			<EmptyState icon={HardDrive} title="No files indexed yet"
				description="Pair a device with Enki Pulse to start indexing files. The desktop agent scans the filesystem every hour."
				action={{ label: 'Pair Device', href: '/admin/devices' }} />
		{/if}
	{/if}
</div>
