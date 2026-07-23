/**
 * Map command store — dispatches commands from AI chat to the map page.
 * The chat page writes commands, the map page reads and executes them.
 */

export type MapCommand =
	| { type: 'navigate'; lat: number; lon: number; zoom: number; label?: string }
	| { type: 'highlight'; west: number; south: number; east: number; north: number; label?: string; color?: string }
	| { type: 'search_nearby'; lat: number; lon: number; radius_km: number; signals?: any }
	| { type: 'geofence_created'; lat: number; lon: number; radius_km: number; name?: string }
	| { type: 'show_track'; analysis: any };

let listeners: ((cmd: MapCommand) => void)[] = [];

export function dispatchMapCommand(cmd: MapCommand) {
	for (const fn of listeners) fn(cmd);
}

export function onMapCommand(fn: (cmd: MapCommand) => void): () => void {
	listeners.push(fn);
	return () => { listeners = listeners.filter(l => l !== fn); };
}
