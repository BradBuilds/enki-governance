// Shared graph module — single source of truth for all visualization
export type { UniversalNode, UniversalEdge } from './types';
export { ENTITY_COLORS, EPISTEMIC_COLORS, EDGE_COLORS, DEFAULT_COLOR, nodeColor, edgeColor } from './colors';
export { entityCategory, SECTOR_ORDER, CATEGORY_LABELS, type EntityCategory } from './categories';
export { nodeRadius, nodeOpacity, nodeBorderDash, nodeLabel, nodeIcon, edgeWidth, edgeOpacity, edgeDash } from './sizing';
export { getSimulationConfig, prePositionNodes, sectorTarget, type SimulationConfig } from './physics';
export { fromLiveNetwork } from './adapters/fromLiveNetwork';
