/**
 * Feature visibility store.
 * All sections are always visible to all users.
 */

class FeatureStore {
	/** All sections always visible */
	isVisible(_section: string): boolean {
		return true;
	}

	/** No-op — kept so existing call sites don't break */
	recordPageVisit(_pathname: string) {}
}

export const features = new FeatureStore();
