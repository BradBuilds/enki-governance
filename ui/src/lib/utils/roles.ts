/**
 * Role check utilities. Used by NavRail and page components
 * to show/hide elements based on user role.
 */

const ROLE_LEVELS: Record<string, number> = {
	member: 0,
	viewer: 1,
	analyst: 2,
	admin: 3
};

export function hasRole(userRole: string, requiredRole: string): boolean {
	return (ROLE_LEVELS[userRole] ?? 0) >= (ROLE_LEVELS[requiredRole] ?? 0);
}

export function isAdmin(role: string): boolean {
	return role === 'admin';
}

export function isAnalyst(role: string): boolean {
	return hasRole(role, 'analyst');
}
