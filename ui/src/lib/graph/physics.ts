/**
 * D3-force simulation configuration factory.
 * Standardizes physics across all graph surfaces.
 */

import { entityCategory, SECTOR_ORDER } from './categories';
import { nodeRadius } from './sizing';
import type { UniversalNode } from './types';

export interface SimulationConfig {
	charge: number;
	chargeDistanceMax: number;
	linkDistance: number;
	linkStrength: number;
	centerStrength: number;
	collisionPadding: number;
	radialStrength: number;
	alphaDecay: number;
	velocityDecay: number;
	tickThrottle: number;
}

/** Build simulation config scaled to node count */
export function getSimulationConfig(nodeCount: number): SimulationConfig {
	return {
		charge: -Math.max(600, 800 + nodeCount * 20),
		chargeDistanceMax: Math.min(1500, 400 + nodeCount * 8),
		linkDistance: Math.min(300, 120 + nodeCount * 2),
		linkStrength: 0.15,
		centerStrength: 0.01,
		collisionPadding: 14,
		radialStrength: 0.07,
		alphaDecay: 0.015,
		velocityDecay: 0.38,
		tickThrottle: 3,
	};
}

/**
 * Pre-position nodes in a ring grouped by type sector.
 * Gives the simulation a sensible starting layout instead of chaos.
 */
export function prePositionNodes(nodes: UniversalNode[]): void {
	const byCategory: Record<string, UniversalNode[]> = {};
	for (const sector of SECTOR_ORDER) byCategory[sector] = [];

	for (const n of nodes) {
		const cat = entityCategory(n.subtype);
		(byCategory[cat] || byCategory['other']).push(n);
	}

	const ringRadius = Math.max(250, nodes.length * 8);
	const sectorAngle = (Math.PI * 2) / SECTOR_ORDER.length;

	for (let s = 0; s < SECTOR_ORDER.length; s++) {
		const list = byCategory[SECTOR_ORDER[s]];
		const baseAngle = s * sectorAngle - Math.PI / 2;
		const subAngle = sectorAngle / Math.max(list.length, 1);

		for (let i = 0; i < list.length; i++) {
			if (list[i].x == null) {
				const a = baseAngle + subAngle * (i + 0.5);
				list[i].x = Math.cos(a) * ringRadius * (0.7 + Math.random() * 0.3);
				list[i].y = Math.sin(a) * ringRadius * (0.7 + Math.random() * 0.3);
			}
		}
	}
}

/**
 * Get the radial anchor target for a node (for forceX / forceY).
 * Pushes nodes toward their type's sector of the ring.
 */
export function sectorTarget(subtype: string | null | undefined, axis: 'x' | 'y', nodeCount: number): number {
	const cat = entityCategory(subtype);
	const idx = SECTOR_ORDER.indexOf(cat);
	const sectorAngle = (Math.PI * 2) / SECTOR_ORDER.length;
	const angle = idx * sectorAngle - Math.PI / 2;
	const ringRadius = Math.max(250, nodeCount * 8) * 0.6;
	return axis === 'x' ? Math.cos(angle) * ringRadius : Math.sin(angle) * ringRadius;
}
