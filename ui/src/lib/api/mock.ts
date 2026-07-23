/**
 * Mock API + Real API client.
 *
 * fetchOrMock tries the real backend first. If unreachable, falls back to mock data.
 * Write methods (PATCH, POST, DELETE) only work against the real backend.
 *
 * Usage:
 *   import { api } from '$lib/api/mock';
 *   const data = await api.dashboard();
 *   await api.updateAlert('alert_123', { status: 'suppressed', suppression_reason: 'known test' });
 */

const MOCK_DELAY = 200;

function delay<T>(data: T): Promise<T> {
	return new Promise(resolve => setTimeout(() => resolve(data), MOCK_DELAY));
}

export const mock = {
	dashboard: () => delay({
		signals: { total: 12847, last_24h: 1247, last_1h: 89 },
		alerts: { total: 47, new: 3, by_status: { new: 3, triaged: 28, escalated: 12, suppressed: 4 } },
		cases: { total: 12, open: 3, by_status: { open: 2, investigating: 3, resolved: 5, closed: 2 } },
		ldu_p_pending: 2,
		top_signal_types_24h: [
			{ type: 'signal.personal.location', count: 480 },
			{ type: 'signal.content.image', count: 234 },
			{ type: 'signal.communications.email', count: 189 },
			{ type: 'signal.security.authentication_failure', count: 87 },
			{ type: 'signal.environmental.temperature', count: 56 },
			{ type: 'signal.financial.transaction', count: 42 },
			{ type: 'signal.content.rss_entry', count: 38 },
			{ type: 'signal.infrastructure.sensor_reading', count: 31 },
		],
	}),

	alerts: () => delay({ alerts: [
		{ alert_id: 'alert_a1b2c3', severity: 'high', status: 'new', category: 'credential_access',
		  title: '5 auth failures from 127.0.0.1 in 3 minutes', created_at: '2026-03-22T14:32:00Z' },
		{ alert_id: 'alert_d4e5f6', severity: 'medium', status: 'new', category: 'geofence',
		  title: 'Sarah left School Zone', created_at: '2026-03-22T15:01:00Z' },
		{ alert_id: 'alert_g7h8i9', severity: 'low', status: 'new', category: 'content_match',
		  title: 'Keyword "classified" found in email', created_at: '2026-03-22T15:30:00Z' },
		{ alert_id: 'alert_j1k2l3', severity: 'high', status: 'triaged', category: 'observable_match',
		  title: 'Watched plate ABC-1234 spotted at 5th & Main', created_at: '2026-03-22T14:32:07Z' },
		{ alert_id: 'alert_m4n5o6', severity: 'medium', status: 'triaged', category: 'threshold',
		  title: 'Temperature exceeded 100F on sensor-outdoor', created_at: '2026-03-22T13:15:00Z' },
	], count: 5 }),

	cases: () => delay({ cases: [
		{ case_id: 'case_47', status: 'investigating', priority: 'high', title: 'Stolen Vehicle Investigation',
		  project_id: 'proj_sec_audit', owner: 'Det. Rodriguez', tags: ['vehicle', 'theft'], created_at: '2026-03-10T09:00:00Z', updated_at: '2026-03-22T14:35:00Z' },
		{ case_id: 'case_51', status: 'open', priority: 'medium', title: 'Q3 Security Audit Review',
		  project_id: 'proj_sec_audit', owner: 'Marcus', tags: ['security', 'audit'], created_at: '2026-03-18T10:00:00Z', updated_at: '2026-03-22T11:00:00Z' },
		{ case_id: 'case_63', status: 'open', priority: 'high', title: 'Network Intrusion Analysis',
		  project_id: 'proj_sec_audit', owner: null, tags: ['security', 'network'], created_at: '2026-03-22T08:00:00Z', updated_at: '2026-03-22T08:00:00Z' },
		{ case_id: 'case_38', status: 'resolved', priority: 'low', title: 'Phishing Campaign Tracking',
		  project_id: 'proj_sec_audit', owner: 'Sarah', tags: ['phishing'], created_at: '2026-03-01T09:00:00Z', updated_at: '2026-03-15T16:00:00Z' },
		{ case_id: 'case_safety1', status: 'open', priority: 'medium', title: 'School Zone Monitoring',
		  project_id: 'proj_family', owner: 'Sam', tags: ['family', 'geofence'], created_at: '2026-02-01T09:00:00Z', updated_at: '2026-03-20T10:00:00Z' },
	], count: 5 }),

	signals: () => delay({ signals: [
		{ signal_id: 'sig_a3f7_001', signal_type: 'signal.security.authentication_failure', source_domain: 'syslog',
		  severity: 'high', timestamp: '2026-03-22T14:32:07Z', device_id: 'firewall-01',
		  cid: 'a3f7c9e1b2d4f6a8',
		  payload: {
			source_ip: '127.0.0.1', target_user: 'admin', attempt_count: 5, method: 'ssh',
			source_port: 44821, target_port: 22, geo_country: 'US', hostname: 'fw-edge-01',
		  },
		  metadata: { card_recipe: {
			card_type: 'authentication_failure', title: 'Auth Failure', icon: 'shield-alert',
			summary: 'Attempts: 5 · Source: 127.0.0.1 · User: admin',
			alert: { severity: 'high', message: '5 failures from 127.0.0.1 in 3 minutes — possible brute force' },
			fields: [
				{ label: 'Source IP', path: 'source_ip', value: '127.0.0.1', format: 'ip' },
				{ label: 'Target User', path: 'target_user', value: 'admin', format: 'text' },
				{ label: 'Attempts', path: 'attempt_count', value: '5', format: 'numeric', highlight: true },
				{ label: 'Method', path: 'method', value: 'ssh', format: 'badge', badge_color: 'orange' },
				{ label: 'Source Port', path: 'source_port', value: '44821', format: 'numeric', highlight: true },
				{ label: 'Target Port', path: 'target_port', value: '22', format: 'numeric', highlight: true },
			],
			trackable: [
				{ path: 'attempt_count', label: 'Attempts', unit: '', current: 5 },
			],
			media: [],
		  }},
		  observables: [
			{ observable_type: 'ip_address', observable_value: '127.0.0.1', extracted_at: '2026-03-22T14:32:07Z' },
			{ observable_type: 'hostname', observable_value: 'fw-edge-01', extracted_at: '2026-03-22T14:32:07Z' },
		  ],
		},
		{ signal_id: 'sig_a3f7_002', signal_type: 'signal.personal.location', source_domain: 'phone_app',
		  severity: 'informational', timestamp: '2026-03-22T14:32:05Z', device_id: 'phone-sarah',
		  latitude: 38.89, longitude: -77.03,
		  cid: 'b4e8d2f1c3a5e7b9',
		  payload: {
			latitude: 38.89, longitude: -77.03, accuracy: 8.2, speed: 2.1,
			altitude: 45, battery: 72, provider: 'gps', name: 'Sarah',
		  },
		  metadata: { card_recipe: {
			card_type: 'location', title: 'Location Update', icon: 'map-pin',
			summary: 'Sarah · Battery: 72% · Accuracy: 8.2m',
			alert: null,
			fields: [
				{ label: 'Name', path: 'name', value: 'Sarah', format: 'text' },
				{ label: 'Accuracy', path: 'accuracy', value: '8.2', format: 'numeric', highlight: true },
				{ label: 'Speed', path: 'speed', value: '2.1', format: 'numeric', highlight: true },
				{ label: 'Altitude', path: 'altitude', value: '45', format: 'numeric', highlight: true },
				{ label: 'Battery', path: 'battery', value: '72', format: 'numeric', highlight: true },
				{ label: 'Provider', path: 'provider', value: 'gps', format: 'badge', badge_color: 'green' },
			],
			trackable: [
				{ path: 'battery', label: 'Battery', unit: '%', current: 72 },
				{ path: 'speed', label: 'Speed', unit: 'mph', current: 2.1 },
				{ path: 'accuracy', label: 'Accuracy', unit: 'm', current: 8.2 },
			],
			media: [
				{ type: 'map', lat: 38.89, lon: -77.03, lat_field: 'latitude', lon_field: 'longitude', label: 'Location' },
				{ type: 'gauge', value: 72, source_field: 'battery', min: 0, max: 100, unit: '%', thresholds: [] },
			],
		  }},
		  observables: [],
		},
		{ signal_id: 'sig_a3f7_003', signal_type: 'signal.environmental.temperature', source_domain: 'sensor_iot',
		  severity: 'informational', timestamp: '2026-03-22T14:31:58Z', device_id: 'sensor-outdoor',
		  cid: 'c5f9e3a2d4b6c8d0',
		  payload: {
			temperature: 87.3, humidity: 62, pressure: 1013.2, uv_index: 7,
			wind_speed: 12.4, dew_point: 71.8, sensor_name: 'Backyard Station',
		  },
		  metadata: { card_recipe: {
			card_type: 'temperature', title: 'Environmental Reading', icon: 'thermometer',
			summary: 'Temperature: 87.3°F · Humidity: 62% · UV: 7',
			alert: null,
			fields: [
				{ label: 'Sensor', path: 'sensor_name', value: 'Backyard Station', format: 'text' },
				{ label: 'Temperature', path: 'temperature', value: '87.3', format: 'numeric', highlight: true },
				{ label: 'Humidity', path: 'humidity', value: '62', format: 'numeric', highlight: true },
				{ label: 'Pressure', path: 'pressure', value: '1013.2', format: 'numeric', highlight: true },
				{ label: 'UV Index', path: 'uv_index', value: '7', format: 'numeric', highlight: true },
				{ label: 'Wind Speed', path: 'wind_speed', value: '12.4', format: 'numeric', highlight: true },
			],
			trackable: [
				{ path: 'temperature', label: 'Temperature', unit: '°F', current: 87.3 },
				{ path: 'humidity', label: 'Humidity', unit: '%', current: 62 },
				{ path: 'pressure', label: 'Pressure', unit: 'hPa', current: 1013.2 },
			],
			media: [
				{ type: 'gauge', value: 87.3, source_field: 'temperature', min: 0, max: 120, unit: '°F', thresholds: [] },
				{ type: 'gauge', value: 62, source_field: 'humidity', min: 0, max: 100, unit: '%', thresholds: [] },
			],
		  }},
		  observables: [],
		},
		{ signal_id: 'sig_a3f7_004', signal_type: 'signal.communications.email', source_domain: 'email_imap',
		  severity: 'informational', timestamp: '2026-03-22T14:31:45Z', device_id: 'imap-connector',
		  cid: 'd6a0f4b3e5c7d9e1',
		  payload: {
			from: 'sarah@example.com', to: ['sam@example.com'], subject: 'Q3 Security Audit — action items',
			body: 'Hi Sam,\n\nAttached are the remaining action items from the Q3 security audit. The credential rotation deadline is Friday.\n\nPlease review and confirm.\n\nThanks,\nSarah',
			has_attachments: true, attachment_count: 1,
		  },
		  metadata: { card_recipe: {
			card_type: 'email', title: 'Email', icon: 'mail',
			summary: 'From: sarah@example.com · Q3 Security Audit — action items',
			alert: null,
			fields: [
				{ label: 'From', path: 'from', value: 'sarah@example.com', format: 'email' },
				{ label: 'To', path: 'to', value: 'sam@example.com', format: 'email' },
				{ label: 'Subject', path: 'subject', value: 'Q3 Security Audit — action items', format: 'text' },
				{ label: 'Attachments', path: 'attachment_count', value: '1', format: 'numeric', highlight: true },
				{ label: 'Has Attachments', path: 'has_attachments', value: 'Yes', format: 'badge', badge_color: 'green' },
			],
			trackable: [],
			media: [
				{ type: 'email', source_field: 'email', label: 'Email',
				  from: 'sarah@example.com', to: ['sam@example.com'],
				  subject: 'Q3 Security Audit — action items',
				  body: 'Hi Sam,\n\nAttached are the remaining action items from the Q3 security audit. The credential rotation deadline is Friday.\n\nPlease review and confirm.\n\nThanks,\nSarah' },
			],
		  }},
		  observables: [
			{ observable_type: 'email_address', observable_value: 'sarah@example.com', extracted_at: '2026-03-22T14:31:45Z' },
		  ],
		},
		{ signal_id: 'sig_a3f7_005', signal_type: 'signal.content.image', source_domain: 'camera_alpr',
		  severity: 'informational', timestamp: '2026-03-22T14:31:30Z', device_id: 'camera-01',
		  cid: 'e7b1a5c4d6e8f0a2',
		  payload: {
			plate: 'ABC-1234', confidence: 94.7, camera_name: 'Camera 01 — 5th & Main',
			state: 'VA', vehicle_color: 'Black', vehicle_type: 'Sedan',
			latitude: 38.8995, longitude: -77.0365,
		  },
		  metadata: { card_recipe: {
			card_type: 'image', title: 'ALPR Capture', icon: 'camera',
			summary: 'Plate: ABC-1234 · Confidence: 94.7% · 5th & Main',
			alert: null,
			fields: [
				{ label: 'Plate', path: 'plate', value: 'ABC-1234', format: 'text' },
				{ label: 'Confidence', path: 'confidence', value: '94.7', format: 'numeric', highlight: true },
				{ label: 'Camera', path: 'camera_name', value: 'Camera 01 — 5th & Main', format: 'text' },
				{ label: 'State', path: 'state', value: 'VA', format: 'badge', badge_color: 'blue' },
				{ label: 'Color', path: 'vehicle_color', value: 'Black', format: 'text' },
				{ label: 'Type', path: 'vehicle_type', value: 'Sedan', format: 'text' },
			],
			trackable: [
				{ path: 'confidence', label: 'Confidence', unit: '%', current: 94.7 },
			],
			media: [
				{ type: 'map', lat: 38.8995, lon: -77.0365, lat_field: 'latitude', lon_field: 'longitude', label: 'Capture Location' },
			],
		  }},
		  observables: [
			{ observable_type: 'license_plate', observable_value: 'ABC-1234', extracted_at: '2026-03-22T14:31:30Z' },
		  ],
		},
		{ signal_id: 'sig_a3f7_006', signal_type: 'signal.financial.transaction', source_domain: 'http_api',
		  severity: 'informational', timestamp: '2026-03-22T14:30:00Z', device_id: 'bank-connector',
		  cid: 'f8c2b6d5e7f9a1b3',
		  payload: {
			amount: 247.83, merchant: 'Home Depot', category: 'Home Improvement',
			method: 'debit', card_last4: '4821', status: 'posted',
			location: 'Arlington, VA',
		  },
		  metadata: { card_recipe: {
			card_type: 'transaction', title: 'Transaction', icon: 'credit-card',
			summary: '$247.83 · Home Depot · Debit',
			alert: null,
			fields: [
				{ label: 'Amount', path: 'amount', value: '$247.83', format: 'numeric', highlight: true },
				{ label: 'Merchant', path: 'merchant', value: 'Home Depot', format: 'text' },
				{ label: 'Category', path: 'category', value: 'Home Improvement', format: 'text' },
				{ label: 'Method', path: 'method', value: 'debit', format: 'badge', badge_color: 'blue' },
				{ label: 'Card', path: 'card_last4', value: '••4821', format: 'text' },
				{ label: 'Status', path: 'status', value: 'posted', format: 'badge', badge_color: 'green' },
			],
			trackable: [
				{ path: 'amount', label: 'Amount', unit: '$', current: 247.83 },
			],
			media: [],
		  }},
		  observables: [],
		},
		{ signal_id: 'sig_a3f7_007', signal_type: 'signal.content.rss_entry', source_domain: 'rss_feed',
		  severity: 'informational', timestamp: '2026-03-22T14:00:00Z', device_id: 'rss-archaeology',
		  cid: 'a9d3c7e6f8a0b2c4',
		  payload: {
			title: 'New Discovery at Gobekli Tepe Rewrites Timeline',
			source: 'Archaeology Magazine', author: 'Dr. Elena Vasquez',
			url: 'https://archaeology.org/2026/03/gobekli-tepe-update',
			summary: 'Excavations at the southeastern quadrant of Gobekli Tepe have uncovered a previously unknown enclosure with pillar carvings dating to 9600 BCE, pushing the site\'s earliest construction back by 400 years.',
			category: 'Archaeology', published_at: '2026-03-22T12:00:00Z',
		  },
		  metadata: { card_recipe: {
			card_type: 'rss_entry', title: 'RSS Article', icon: 'rss',
			summary: 'Archaeology Magazine · Dr. Elena Vasquez',
			alert: null,
			fields: [
				{ label: 'Title', path: 'title', value: 'New Discovery at Gobekli Tepe Rewrites Timeline', format: 'text' },
				{ label: 'Source', path: 'source', value: 'Archaeology Magazine', format: 'text' },
				{ label: 'Author', path: 'author', value: 'Dr. Elena Vasquez', format: 'text' },
				{ label: 'Category', path: 'category', value: 'Archaeology', format: 'badge', badge_color: 'blue' },
				{ label: 'Link', path: 'url', value: 'https://archaeology.org/2026/03/gobekli-tepe-update', format: 'url' },
			],
			trackable: [],
			media: [],
		  }},
		  observables: [
			{ observable_type: 'domain_name', observable_value: 'archaeology.org', extracted_at: '2026-03-22T14:00:00Z' },
		  ],
		},
	], count: 7 }),

	gallery: () => delay({ items: [
		{ rid: 1, signal_id: 'sig_img_001', signal_type: 'signal.content.image', source: 'camera_alpr',
		  timestamp: '2026-03-22T14:31:30Z', device_id: 'camera-01', title: 'ALPR Capture — 5th & Main',
		  format: 'jpeg', size_bytes: 2847291, has_thumbnail: true, seen: false, tags: ['image', 'has_location'] },
		{ rid: 2, signal_id: 'sig_img_002', signal_type: 'signal.content.image', source: 'camera_alpr',
		  timestamp: '2026-03-22T14:28:15Z', device_id: 'camera-03', title: 'ALPR Capture — Parking Lot B',
		  format: 'jpeg', size_bytes: 1923847, has_thumbnail: true, seen: true, tags: ['image'] },
		{ rid: 3, signal_id: 'sig_doc_001', signal_type: 'signal.content.document', source: 'email_imap',
		  timestamp: '2026-03-22T14:00:00Z', device_id: 'imap-connector', title: 'Q3 Security Audit Report.pdf',
		  format: 'pdf', size_bytes: 847000, has_thumbnail: false, seen: false, tags: ['document'] },
		{ rid: 4, signal_id: 'sig_rss_001', signal_type: 'signal.interest.article', source: 'rss_feed',
		  timestamp: '2026-03-22T12:00:00Z', device_id: 'rss-archaeology', title: 'New Discovery at Gobekli Tepe',
		  format: 'html', size_bytes: 15000, has_thumbnail: false, seen: true, tags: ['content'] },
		{ rid: 5, signal_id: 'sig_img_003', signal_type: 'signal.content.image', source: 'phone_app',
		  timestamp: '2026-03-22T10:30:00Z', device_id: 'phone-sam', title: 'Site Photo — Building C',
		  format: 'jpeg', size_bytes: 4200000, has_thumbnail: true, seen: false, tags: ['image', 'has_location'] },
	], total: 5, limit: 50, offset: 0 }),

	financialAccounts: () => delay({ accounts: [
		{ account_id: 'acct_chase_chk', name: 'Chase Checking ····4891', institution: 'Chase', account_type: 'checking',
		  is_liability: false, current_balance: 7304.65, status: 'active', account_number_masked: '····4891' },
		{ account_id: 'acct_chase_sav', name: 'Chase Savings ····2103', institution: 'Chase', account_type: 'savings',
		  is_liability: false, current_balance: 22450.00, status: 'active', account_number_masked: '····2103' },
		{ account_id: 'acct_visa', name: 'Chase Visa ····7823', institution: 'Chase', account_type: 'credit_card',
		  is_liability: true, current_balance: -3241.50, credit_limit: 15000, interest_rate: 24.99, status: 'active' },
		{ account_id: 'acct_car', name: 'Auto Loan ····9012', institution: 'Capital One', account_type: 'loan',
		  is_liability: true, current_balance: -14200.00, interest_rate: 5.49, original_balance: -28000, status: 'active' },
		{ account_id: 'acct_mortgage', name: 'Mortgage ····3456', institution: 'Wells Fargo', account_type: 'mortgage',
		  is_liability: true, current_balance: -245000.00, interest_rate: 3.25, original_balance: -280000, status: 'active' },
	], count: 5 }),

	netWorth: () => delay({
		net_worth: 127413.15,
		total_assets: 389854.65,
		total_liabilities: 262441.50,
		assets: [
			{ id: 'acct_chase_chk', name: 'Chase Checking', type: 'checking', balance: 7304.65, source: 'financial_account' },
			{ id: 'acct_chase_sav', name: 'Chase Savings', type: 'savings', balance: 22450.00, source: 'financial_account' },
			{ id: 'asset_001', name: '2019 BMW X5 — ABC-1234', type: 'vehicle', balance: 48000, source: 'asset' },
			{ id: 'asset_home', name: 'Primary Residence', type: 'real_estate', balance: 310000, source: 'asset' },
			{ id: 'asset_crypto_001', name: 'Bitcoin Wallet', type: 'crypto', balance: 2100, source: 'asset' },
		],
		liabilities: [
			{ id: 'acct_visa', name: 'Chase Visa', type: 'credit_card', balance: -3241.50, source: 'financial_account' },
			{ id: 'acct_car', name: 'Auto Loan', type: 'loan', balance: -14200.00, source: 'financial_account' },
			{ id: 'acct_mortgage', name: 'Mortgage', type: 'mortgage', balance: -245000.00, source: 'financial_account' },
		],
	}),

	financialRecurring: () => delay({ recurring: [
		{ recurring_id: 'rec_001', merchant: 'netflix.com', typical_amount: -15.99, frequency: 'monthly', category: 'Subscriptions & Digital', is_income: false, occurrence_count: 6 },
		{ recurring_id: 'rec_002', merchant: 'spotify', typical_amount: -10.99, frequency: 'monthly', category: 'Subscriptions & Digital', is_income: false, occurrence_count: 6 },
		{ recurring_id: 'rec_003', merchant: 'direct deposit', typical_amount: 3200.00, frequency: 'biweekly', category: 'Transfers & Payments', is_income: true, occurrence_count: 4 },
		{ recurring_id: 'rec_004', merchant: 'shell oil', typical_amount: -52.40, frequency: 'weekly', category: 'Fuel & Gas', is_income: false, occurrence_count: 8 },
		{ recurring_id: 'rec_005', merchant: 'mortgage payment', typical_amount: -1847.00, frequency: 'monthly', category: 'Transfers & Payments', is_income: false, occurrence_count: 3 },
	], count: 5 }),

	financialSummary: () => delay({
		period_days: 30,
		transaction_count: 127,
		total_debits: -4823.47,
		total_credits: 6400.00,
		net: 1576.53,
		categories: [
			{ category: 'Fuel & Gas', count: 12, total: -892.40 },
			{ category: 'Groceries', count: 18, total: -741.23 },
			{ category: 'Subscriptions & Digital', count: 8, total: -487.92 },
			{ category: 'Restaurants & Dining', count: 22, total: -456.80 },
			{ category: 'Shopping', count: 9, total: -398.50 },
			{ category: 'Utilities', count: 4, total: -342.10 },
			{ category: 'Home Improvement', count: 3, total: -289.45 },
			{ category: 'Automotive', count: 2, total: -187.30 },
			{ category: 'Entertainment', count: 5, total: -156.00 },
			{ category: 'Uncategorized', count: 6, total: -871.77 },
		],
		daily_trend: [
			{ date: '2026-03-06', spent: 145.20 },
			{ date: '2026-03-07', spent: 67.80 },
			{ date: '2026-03-08', spent: 234.50 },
			{ date: '2026-03-09', spent: 12.99 },
			{ date: '2026-03-10', spent: 89.00 },
			{ date: '2026-03-11', spent: 178.40 },
			{ date: '2026-03-12', spent: 45.60 },
			{ date: '2026-03-13', spent: 312.00 },
			{ date: '2026-03-14', spent: 56.30 },
			{ date: '2026-03-15', spent: 198.70 },
		],
		top_merchants: [
			{ merchant: 'SHELL OIL 04523', count: 8, total: 623.40 },
			{ merchant: 'AMAZON.COM', count: 6, total: 398.50 },
			{ merchant: 'KROGER #1247', count: 5, total: 312.80 },
			{ merchant: 'NETFLIX.COM', count: 1, total: 15.99 },
			{ merchant: 'HOME DEPOT #4521', count: 2, total: 289.45 },
		],
	}),

	presets: () => delay({ presets: [
		{ preset_id: 'fin_large_transaction', name: 'Large Transaction', description: 'Transaction exceeds dollar threshold',
		  category: 'financial', severity: 'medium', default_enabled: true, rule_type: 'match', active: true, enabled: true, match_count: 7, last_matched_at: '2026-03-22T14:30:00Z' },
		{ preset_id: 'fin_very_large_transaction', name: 'Very Large Transaction', description: 'Single transaction over $5,000',
		  category: 'financial', severity: 'high', default_enabled: true, rule_type: 'match', active: false, enabled: false, match_count: 0 },
		{ preset_id: 'fin_duplicate_charge', name: 'Duplicate Charge', description: 'Same amount from same merchant within 24 hours',
		  category: 'financial', severity: 'medium', default_enabled: true, rule_type: 'threshold', active: true, enabled: true, match_count: 2, last_matched_at: '2026-03-20T09:15:00Z' },
		{ preset_id: 'fin_rapid_spending', name: 'Rapid Spending Burst', description: '5+ transactions in 30 minutes',
		  category: 'financial', severity: 'high', default_enabled: true, rule_type: 'threshold', active: false, enabled: false, match_count: 0 },
		{ preset_id: 'fin_declined_transaction', name: 'Declined Transaction', description: 'Transaction was declined or failed',
		  category: 'financial', severity: 'medium', default_enabled: true, rule_type: 'match', active: true, enabled: true, match_count: 1 },
		{ preset_id: 'sec_auth_failure_burst', name: 'Authentication Failure Burst', description: '5+ failed logins from same source in 5 minutes',
		  category: 'security', severity: 'high', default_enabled: true, rule_type: 'threshold', active: true, enabled: true, match_count: 8, last_matched_at: '2026-03-22T14:32:00Z' },
		{ preset_id: 'sec_intrusion_detected', name: 'Intrusion Detected', description: 'IDS/IPS flagged intrusion attempt',
		  category: 'security', severity: 'critical', default_enabled: true, rule_type: 'match', active: true, enabled: true, match_count: 0 },
		{ preset_id: 'sec_malware_detected', name: 'Malware Detected', description: 'Antivirus or endpoint protection flagged malware',
		  category: 'security', severity: 'critical', default_enabled: true, rule_type: 'match', active: true, enabled: true, match_count: 0 },
		{ preset_id: 'sec_port_scan', name: 'Port Scan Detected', description: 'Multiple connection attempts to different ports',
		  category: 'security', severity: 'high', default_enabled: true, rule_type: 'threshold', active: false, enabled: false, match_count: 0 },
		{ preset_id: 'sec_privilege_escalation', name: 'Privilege Escalation', description: 'User gained elevated privileges',
		  category: 'security', severity: 'critical', default_enabled: true, rule_type: 'content', active: true, enabled: true, match_count: 0 },
		{ preset_id: 'geo_speed_alert', name: 'Speed Alert', description: 'Entity exceeding speed threshold',
		  category: 'geospatial', severity: 'medium', default_enabled: true, rule_type: 'match', active: true, enabled: true, match_count: 3 },
		{ preset_id: 'geo_aircraft_squawk_7700', name: 'Aircraft Emergency (Squawk 7700)', description: 'Aircraft broadcasting emergency squawk code',
		  category: 'geospatial', severity: 'critical', default_enabled: true, rule_type: 'match', active: true, enabled: true, match_count: 0 },
		{ preset_id: 'geo_aircraft_squawk_7500', name: 'Aircraft Hijack (Squawk 7500)', description: 'Aircraft broadcasting hijack squawk code',
		  category: 'geospatial', severity: 'critical', default_enabled: true, rule_type: 'match', active: true, enabled: true, match_count: 0 },
		{ preset_id: 'geo_geofence_exit', name: 'Geofence Exit', description: 'Entity left a monitored boundary',
		  category: 'geospatial', severity: 'medium', default_enabled: true, rule_type: 'match', active: true, enabled: true, match_count: 5 },
		{ preset_id: 'env_severe_weather', name: 'Severe Weather Alert', description: 'NWS tornado, hurricane, or flash flood warning',
		  category: 'environmental', severity: 'critical', default_enabled: true, rule_type: 'content', active: true, enabled: true, match_count: 1 },
		{ preset_id: 'env_earthquake', name: 'Earthquake Detected', description: 'Seismic event magnitude 4.0+',
		  category: 'environmental', severity: 'high', default_enabled: true, rule_type: 'match', active: true, enabled: true, match_count: 0 },
		{ preset_id: 'dev_low_battery', name: 'Low Battery', description: 'Device battery below 15%',
		  category: 'device', severity: 'medium', default_enabled: true, rule_type: 'match', active: true, enabled: true, match_count: 12, last_matched_at: '2026-03-22T10:00:00Z' },
		{ preset_id: 'dev_critical_battery', name: 'Critical Battery', description: 'Device battery below 5%',
		  category: 'device', severity: 'high', default_enabled: true, rule_type: 'match', active: true, enabled: true, match_count: 2 },
		{ preset_id: 'dev_root_detected', name: 'Root/Jailbreak Detected', description: 'Device reports root or jailbreak status',
		  category: 'device', severity: 'critical', default_enabled: true, rule_type: 'match', active: true, enabled: true, match_count: 0 },
		{ preset_id: 'veh_speeding', name: 'Vehicle Speeding', description: 'Vehicle exceeding speed limit',
		  category: 'vehicle', severity: 'medium', default_enabled: true, rule_type: 'match', active: false, enabled: false, match_count: 0 },
		{ preset_id: 'veh_tow_alert', name: 'Possible Tow / Unauthorized Movement', description: 'Vehicle moving with ignition off',
		  category: 'vehicle', severity: 'high', default_enabled: true, rule_type: 'match', active: false, enabled: false, match_count: 0 },
		{ preset_id: 'pers_sos', name: 'SOS / Panic Button', description: 'Emergency SOS signal from personal device',
		  category: 'personal', severity: 'critical', default_enabled: true, rule_type: 'match', active: true, enabled: true, match_count: 0 },
		{ preset_id: 'comm_sos_received', name: 'SOS / Emergency Message', description: 'Emergency or SOS keyword in any communication',
		  category: 'communications', severity: 'critical', default_enabled: true, rule_type: 'content', active: true, enabled: true, match_count: 0 },
		{ preset_id: 'infra_alpr_hit', name: 'License Plate Hit', description: 'ALPR reading matched a watchlist plate',
		  category: 'infrastructure', severity: 'high', default_enabled: true, rule_type: 'match', active: true, enabled: true, match_count: 2 },
		{ preset_id: 'ops_pipeline_error', name: 'Pipeline Error', description: 'Signal ingestion or processing error rate spike',
		  category: 'operational', severity: 'high', default_enabled: true, rule_type: 'threshold', active: false, enabled: false, match_count: 0 },
	], count: 25 }),

	organized: () => delay({ domains: [
		{ domain: 'personal', total: 480, last_24h: 120, types: [
			{ signal_type: 'signal.personal.location', label: 'Location', count: 450, last_24h: 110 },
			{ signal_type: 'signal.personal.message', label: 'Message', count: 30, last_24h: 10 },
		]},
		{ domain: 'content', total: 310, last_24h: 80, types: [
			{ signal_type: 'signal.content.image', label: 'Image', count: 234, last_24h: 55 },
			{ signal_type: 'signal.content.document', label: 'Document', count: 38, last_24h: 10 },
			{ signal_type: 'signal.content.rss_entry', label: 'RSS Entry', count: 38, last_24h: 15 },
		]},
		{ domain: 'communications', total: 189, last_24h: 45, types: [
			{ signal_type: 'signal.communications.email', label: 'Email', count: 189, last_24h: 45 },
		]},
		{ domain: 'security', total: 87, last_24h: 22, types: [
			{ signal_type: 'signal.security.authentication_failure', label: 'Auth Failure', count: 87, last_24h: 22 },
		]},
		{ domain: 'financial', total: 42, last_24h: 12, types: [
			{ signal_type: 'signal.financial.transaction', label: 'Transaction', count: 42, last_24h: 12 },
		]},
		{ domain: 'environmental', total: 56, last_24h: 14, types: [
			{ signal_type: 'signal.environmental.temperature', label: 'Temperature', count: 56, last_24h: 14 },
		]},
	]}),

	lduPEntries: () => delay({ entries: [
		{ ldu_p_id: 'ldp_001', ldu_t_id: 'ldt_x1y2z3', format_detected: 'json',
		  proposed_signal_type: 'signal.iot.water_quality', vocabulary_gaps: ['signal.iot.water_quality'],
		  detection_confidence: 0.78,
		  extracted_structure: { ph: 7.2, turbidity: 3.1, dissolved_oxygen: 8.4, temperature: 18.5, sensor_id: 'wq-pond-01' },
		  proposed_mapping: { signal_type_proposed: 'signal.iot.water_quality', observables_proposed: [] },
		  resolution: 'pending_review', created_at: '2026-03-22T14:45:00Z' },
		{ ldu_p_id: 'ldp_002', ldu_t_id: 'ldt_a4b5c6', format_detected: 'json',
		  proposed_signal_type: 'signal.social.post', vocabulary_gaps: ['signal.social.post'],
		  detection_confidence: 0.62,
		  extracted_structure: { platform: 'mastodon', username: '@analyst@infosec.exchange', body: 'Interesting CVE activity on port 443 targets this week', timestamp: '2026-03-22T14:40:00Z', hashtags: ['cve', 'infosec'] },
		  proposed_mapping: { signal_type_proposed: 'signal.social.post', observables_proposed: [{ observable_type: 'username', observable_value: '@analyst@infosec.exchange', normalized_value: 'analyst@infosec.exchange', source_field: 'username' }] },
		  resolution: 'pending_review', created_at: '2026-03-22T14:42:00Z' },
	], count: 2 }),

	projects: () => delay({ projects: [
		{ project_id: 'proj_sec_audit', name: 'Security Audit Q3', description: 'Comprehensive Q3 security review covering vehicle theft, network intrusions, and phishing campaigns.',
		  status: 'active', node_count: 23, case_count: 4, topic_count: 1, created_at: '2026-03-01T09:00:00Z' },
		{ project_id: 'proj_architecture', name: 'Architecture Research', description: 'Archaeological site analysis — Gobekli Tepe, Stonehenge, and megalithic alignments.',
		  status: 'active', node_count: 47, case_count: 0, topic_count: 0, created_at: '2026-02-15T10:00:00Z' },
		{ project_id: 'proj_family', name: 'Family Safety', description: 'Family member location monitoring, school zone geofencing, and personal safety alerts.',
		  status: 'active', node_count: 8, case_count: 1, topic_count: 0, created_at: '2026-01-20T08:00:00Z' },
	]}),

	profile: () => delay({
		identity: { identity_id: 'person_sam_abc123', display_name: 'Sam', role: 'admin' },
		preferences: { theme: 'dark', default_view: 'dashboard', timezone: 'America/New_York' },
		projects: [],
		collections: [],
		active_cases: [],
		stats: { signal_count: 0, unseen_count: 0, project_count: 0, collection_count: 0, active_case_count: 0 },
	}),

	geoSignals: (_params?: any) => delay({
		type: 'FeatureCollection',
		features: [
			{ type: 'Feature', geometry: { type: 'Point', coordinates: [-77.0365, 38.8977] },
			  properties: { signal_id: 'sig_geo_001', signal_type: 'signal.geospatial.position_report',
				device_id: 'traccar_phone-01', severity: 'informational', timestamp: '2026-03-22T14:00:00Z',
				latitude: 38.8977, longitude: -77.0365 }},
			{ type: 'Feature', geometry: { type: 'Point', coordinates: [-77.0320, 38.9002] },
			  properties: { signal_id: 'sig_geo_002', signal_type: 'signal.geospatial.position_report',
				device_id: 'traccar_phone-01', severity: 'informational', timestamp: '2026-03-22T14:05:00Z',
				latitude: 38.9002, longitude: -77.0320 }},
			{ type: 'Feature', geometry: { type: 'Point', coordinates: [-118.2437, 34.0522] },
			  properties: { signal_id: 'sig_geo_003', signal_type: 'signal.personal.location',
				device_id: 'garmin-inreach-01', severity: 'informational', timestamp: '2026-03-22T15:30:00Z',
				latitude: 34.0522, longitude: -118.2437 }},
			{ type: 'Feature', geometry: { type: 'Point', coordinates: [-73.9857, 40.7484] },
			  properties: { signal_id: 'sig_geo_004', signal_type: 'signal.infrastructure.alpr_reading',
				device_id: 'cam-broadway-01', severity: 'low', timestamp: '2026-03-22T16:00:00Z',
				latitude: 40.7484, longitude: -73.9857 }},
			{ type: 'Feature', geometry: { type: 'Point', coordinates: [-87.6298, 41.8781] },
			  properties: { signal_id: 'sig_geo_005', signal_type: 'signal.security.network_anomaly',
				device_id: 'sensor-chi-01', severity: 'high', timestamp: '2026-03-22T17:15:00Z',
				latitude: 41.8781, longitude: -87.6298 }},
		],
	}),

	tracks: () => delay({ tracks: [
		{ track_id: 'trk_001', entity_id: 'traccar_phone-01', display_name: 'Phone — Sam',
		  signal_count: 6, start_time: '2026-03-22T08:00:00Z', end_time: '2026-03-22T14:05:00Z',
		  geometry: { type: 'LineString', coordinates: [
			[-77.0500, 38.8850], [-77.0450, 38.8900], [-77.0400, 38.8950],
			[-77.0380, 38.8960], [-77.0365, 38.8977], [-77.0320, 38.9002],
		  ]}},
		{ track_id: 'trk_002', entity_id: 'garmin-inreach-01', display_name: 'Garmin — Field Unit',
		  signal_count: 4, start_time: '2026-03-22T09:00:00Z', end_time: '2026-03-22T15:30:00Z',
		  geometry: { type: 'LineString', coordinates: [
			[-118.2800, 34.0300], [-118.2600, 34.0400], [-118.2500, 34.0480], [-118.2437, 34.0522],
		  ]}},
	], count: 2 }),
	geofences: () => delay({ geofences: [
		{ geofence_id: 'gf_001', name: 'School Zone — Lincoln Elementary', description: 'Alert when family members leave',
		  color: '#22c55e', enabled: true, alert_on: 'exit',
		  geometry: { type: 'Polygon', coordinates: [[
			[-77.040, 38.900], [-77.035, 38.900], [-77.035, 38.895], [-77.040, 38.895], [-77.040, 38.900],
		  ]]}},
		{ geofence_id: 'gf_002', name: 'Warehouse District — Restricted', description: 'After-hours monitoring perimeter',
		  color: '#ef4444', enabled: true, alert_on: 'enter',
		  geometry: { type: 'Polygon', coordinates: [[
			[-87.640, 41.885], [-87.625, 41.885], [-87.625, 41.875], [-87.640, 41.875], [-87.640, 41.885],
		  ]]}},
		{ geofence_id: 'gf_003', name: 'Office Campus', description: 'Employee presence zone',
		  color: '#3b82f6', enabled: false, alert_on: 'both',
		  geometry: { type: 'Polygon', coordinates: [[
			[-77.045, 38.905], [-77.038, 38.905], [-77.038, 38.900], [-77.045, 38.900], [-77.045, 38.905],
		  ]]}},
	], count: 3 }),
	annotations: () => delay({ type: 'FeatureCollection', features: [
		{ type: 'Feature',
		  geometry: { type: 'Point', coordinates: [-77.0369, 38.8976] },
		  properties: { annotation_id: 'ann_001', annotation_type: 'pin', title: 'Observation Point Alpha',
			style: { color: '#a855f7', weight: 2 }}},
		{ type: 'Feature',
		  geometry: { type: 'LineString', coordinates: [[-77.050, 38.890], [-77.040, 38.895], [-77.035, 38.898]] },
		  properties: { annotation_id: 'ann_002', annotation_type: 'route', title: 'Patrol Route — Evening',
			style: { color: '#f59e0b', weight: 3 }}},
		{ type: 'Feature',
		  geometry: { type: 'Polygon', coordinates: [[[-87.635, 41.882], [-87.628, 41.882], [-87.628, 41.878], [-87.635, 41.878], [-87.635, 41.882]]] },
		  properties: { annotation_id: 'ann_003', annotation_type: 'polygon', title: 'Search Area — Building C',
			style: { color: '#06b6d4', weight: 2, fillOpacity: 0.15 }}},
	]}),

	geoNodes: (_params?: any) => delay({
		type: 'FeatureCollection',
		features: [
			{ type: 'Feature', geometry: { type: 'Point', coordinates: [35.2345, 37.2235] },
			  properties: { node_id: 'node_mock_001', title: 'Gobekli Tepe — Enclosure D Pillars',
				epistemic_type: 'observation', project_id: 'proj_near_east',
				concepts: ['neolithic', 'megalith', 'pillar', 'animal-relief'],
				description: 'Stone T-shaped pillars with animal relief carvings. Enclosure D has the largest pillars, approximately 5.5m tall. Fox, crane, and wild boar motifs visible.',
				images: [{ url: 'https://upload.wikimedia.org/wikipedia/commons/thumb/e/e7/G%C3%B6bekli_Tepe%2C_Urfa.jpg/1280px-G%C3%B6bekli_Tepe%2C_Urfa.jpg', caption: 'Enclosure D aerial view' }],
				source_authority: 'verified', latitude: 37.2235, longitude: 35.2345,
				created_at: '2026-03-15T10:00:00Z' }},
			{ type: 'Feature', geometry: { type: 'Point', coordinates: [35.2350, 37.2230] },
			  properties: { node_id: 'node_mock_002', title: 'Enclosure C — Lion Pillar',
				epistemic_type: 'fact', project_id: 'proj_near_east',
				concepts: ['neolithic', 'megalith', 'lion', 'predator-motif'],
				description: 'Pillar 27 features a leaping lion or large feline. Unique among the enclosures.',
				images: [], source_authority: 'verified', latitude: 37.2230, longitude: 35.2350,
				created_at: '2026-03-16T11:30:00Z' }},
			{ type: 'Feature', geometry: { type: 'Point', coordinates: [-1.8262, 51.1789] },
			  properties: { node_id: 'node_mock_003', title: 'Stonehenge — Station Stones',
				epistemic_type: 'observation', project_id: 'proj_megaliths',
				concepts: ['neolithic', 'megalith', 'alignment', 'solstice'],
				description: 'Four Station Stones mark a rectangle. Their alignment points to lunar and solar events.',
				images: [{ url: 'https://upload.wikimedia.org/wikipedia/commons/thumb/3/3c/Stonehenge2007_07_30.jpg/1280px-Stonehenge2007_07_30.jpg', caption: 'Stonehenge at dusk' }],
				source_authority: 'verified', latitude: 51.1789, longitude: -1.8262,
				created_at: '2026-03-10T09:00:00Z' }},
			{ type: 'Feature', geometry: { type: 'Point', coordinates: [-72.5450, -13.1631] },
			  properties: { node_id: 'node_mock_004', title: 'Machu Picchu — Intihuatana Stone',
				epistemic_type: 'claim', project_id: 'proj_megaliths',
				concepts: ['inca', 'solar-observatory', 'ritual', 'alignment'],
				description: 'The Intihuatana ("hitching post of the sun") is believed to be an astronomic clock. Disputed whether it served a calendrical or purely ritual function.',
				images: [], source_authority: 'unverified', latitude: -13.1631, longitude: -72.5450,
				created_at: '2026-03-18T14:00:00Z' }},
		],
	}),

	// ─── Watch Topics ───────────────────────────────────────────────────
	watchTopics: () => delay({ topics: [
		{ topic_id: 'topic_ukraine', title: 'Ukraine Conflict', status: 'active',
		  keywords: ['ukraine', 'kyiv', 'petrenko', 'crimea', 'donbas', 'NATO'],
		  sources: { gdelt: true, reddit: true, rss: true, twitter: false },
		  update_interval: 15, article_count: 234, cluster_count: 47, entity_count: 89, location_count: 23,
		  ai_summary: 'Escalation near Zaporizhzhia NPP with drone strikes intercepted. EU moving toward 14th sanctions package with vote expected Thursday. Grain corridor talks show limited progress with Turkey mediating. Russian forces intensifying operations in Donetsk Oblast.',
		  summary_updated_at: '2026-03-22T16:00:00Z', backfill_status: 'complete',
		  created_at: '2026-02-01T09:00:00Z', updated_at: '2026-03-22T16:00:00Z' },
		{ topic_id: 'topic_quantum', title: 'Quantum Computing', status: 'active',
		  keywords: ['quantum computing', 'qubit', 'quantum error correction', 'quantum supremacy'],
		  sources: { gdelt: true, reddit: true, rss: true, twitter: false },
		  update_interval: 60, article_count: 67, cluster_count: 18, entity_count: 34, location_count: 8,
		  ai_summary: 'IBM announced 1121-qubit Condor processor benchmarks showing 3x improvement in error rates. Google published quantum error correction milestone in Nature. MIT and Harvard collaboration on topological qubits showing promise. Industry consensus shifting toward error-corrected quantum advantage by 2028.',
		  summary_updated_at: '2026-03-21T10:00:00Z', backfill_status: 'complete',
		  created_at: '2026-03-01T09:00:00Z', updated_at: '2026-03-21T10:00:00Z' },
		{ topic_id: 'topic_artist', title: 'Nova Lane', status: 'active',
		  keywords: ['Nova Lane', 'World Tour', 'fans', 'A celebrity guest'],
		  sources: { gdelt: true, reddit: true, rss: true, twitter: true },
		  update_interval: 30, article_count: 412, cluster_count: 83, entity_count: 56, location_count: 15,
		  ai_summary: 'World Tour adding 3 new European dates in Madrid, Lisbon, and Milan for September. Arena Night 3 featured surprise acoustic set with 3 unreleased tracks. A celebrity guest spotted in VIP section. Billboard reports tour has now grossed $2.1B, making it the highest-grossing tour in history.',
		  summary_updated_at: '2026-03-22T14:00:00Z', backfill_status: 'complete',
		  created_at: '2026-03-10T09:00:00Z', updated_at: '2026-03-22T14:00:00Z' },
	], count: 3 }),

	watchTopic: (id: string) => delay({
		...(id === 'topic_ukraine' ? {
			topic_id: 'topic_ukraine', title: 'Ukraine Conflict', status: 'active',
			description: 'Monitoring the ongoing conflict in Ukraine including military operations, diplomatic efforts, humanitarian impact, and international response.',
			keywords: ['ukraine', 'kyiv', 'petrenko', 'crimea', 'donbas', 'NATO', 'sanctions', 'grain corridor'],
			sources: { gdelt: true, reddit: true, rss: true, twitter: false },
			auto_extract: { people: true, locations: true, organizations: true, assets: true, summaries: true },
			update_interval: 15, article_count: 234, cluster_count: 47, entity_count: 89, location_count: 23,
			ai_summary: 'Escalation near Zaporizhzhia NPP with drone strikes intercepted. EU moving toward 14th sanctions package with vote expected Thursday. Grain corridor talks show limited progress with Turkey mediating. Russian forces intensifying operations in Donetsk Oblast.',
			summary_updated_at: '2026-03-22T16:00:00Z',
			backfill_status: 'complete', backfill_months: 6,
			created_at: '2026-02-01T09:00:00Z', updated_at: '2026-03-22T16:00:00Z',
		} : {
			topic_id: id, title: 'Unknown Topic', status: 'active', keywords: [],
			article_count: 0, cluster_count: 0, entity_count: 0, location_count: 0,
			ai_summary: null, created_at: '2026-03-01T00:00:00Z', updated_at: '2026-03-01T00:00:00Z',
		}),
	}),

	watchTopicClusters: (_id: string) => delay({ clusters: [
		{ cluster_id: 'cluster_zap_drone', headline: 'Drone strikes intercepted over Zaporizhzhia nuclear plant',
		  summary: 'Three Shahed-136 drones were intercepted over the cooling pond of the Zaporizhzhia nuclear power plant. IAEA director Ferri condemned the attack. Petrenko called for emergency UN Security Council session. EU announced emergency meeting for 14th sanctions package.',
		  event_type: 'military_action', event_date: '2026-03-22', source_count: 5, article_count: 5,
		  people: [
			{ name: 'Marco Ferri', role: 'IAEA Director General', context: 'Condemned attack as serious escalation' },
			{ name: 'Andriy Petrenko', role: 'President of Ukraine', context: 'Called for emergency UN session' },
			{ name: 'Yuriy Bondar', role: 'Ukrainian Air Force spokesperson', context: 'Confirmed interception' },
			{ name: 'Jordi Castell', role: 'EU foreign policy chief', context: 'Announced emergency EU meeting' },
		  ],
		  organizations: [
			{ name: 'IAEA', type: 'ngo', context: 'Inspectors confirmed no reactor damage' },
			{ name: 'European Union', type: 'government', context: 'Planning 14th sanctions package' },
		  ],
		  locations: [
			{ name: 'Zaporizhzhia', lat: 47.8388, lon: 35.1396, country: 'Ukraine', context: 'Location of nuclear plant' },
		  ],
		  assets: [{ name: 'Shahed-136', type: 'weapon', context: 'Iranian-made drones used in attack' }],
		  conflicts: [
			{ claim: 'Drone origin', positions: [
				{ stance: 'Iranian-made Shahed-136', sources: ['Reuters', 'BBC'] },
				{ stance: 'Origin unconfirmed', sources: ['Al Jazeera'] },
			]},
			{ claim: 'Impact on plant', positions: [
				{ stance: 'All intercepted, no impact', sources: ['Ukrinform', 'BBC'] },
				{ stance: 'One struck cooling infrastructure', sources: ['RT'] },
			]},
		  ],
		  key_claims: [
			{ claim: 'Three drones intercepted over cooling pond', source: 'Yuriy Bondar', confidence: 'confirmed' },
			{ claim: 'No damage to reactor buildings', source: 'IAEA inspectors', confidence: 'confirmed' },
			{ claim: 'Plant under Russian control since March 2022', source: 'Reuters', confidence: 'confirmed' },
		  ],
		  sentiment: 'negative', keywords: ['nuclear', 'drone', 'IAEA', 'sanctions'],
		  created_at: '2026-03-22T04:00:00Z', updated_at: '2026-03-22T16:00:00Z' },

		{ cluster_id: 'cluster_sanctions', headline: 'EU moves toward 14th sanctions package against Russia',
		  summary: 'European Union foreign policy chief Castell announced emergency meeting for Monday to discuss 14th sanctions package. Package expected to target Russian energy sector and military-industrial complex. Hungary signaling potential veto on energy provisions.',
		  event_type: 'diplomatic', event_date: '2026-03-22', source_count: 4, article_count: 4,
		  people: [
			{ name: 'Jordi Castell', role: 'EU foreign policy chief', context: 'Convening emergency meeting' },
			{ name: 'Viktor Halász', role: 'PM of Hungary', context: 'Signaling potential veto on energy sanctions' },
		  ],
		  organizations: [
			{ name: 'European Union', type: 'government', context: 'Drafting 14th sanctions package' },
		  ],
		  locations: [
			{ name: 'Brussels', lat: 50.8503, lon: 4.3517, country: 'Belgium', context: 'EU headquarters' },
			{ name: 'Budapest', lat: 47.4979, lon: 19.0402, country: 'Hungary', context: 'Hungarian objections' },
		  ],
		  conflicts: [
			{ claim: 'Energy sector scope', positions: [
				{ stance: 'Broad energy sanctions including LNG', sources: ['Reuters', 'FT'] },
				{ stance: 'Energy carve-outs likely due to Hungarian veto', sources: ['Politico EU'] },
			]},
		  ],
		  key_claims: [
			{ claim: 'Emergency meeting scheduled for Monday', source: 'Castell office', confidence: 'confirmed' },
			{ claim: 'Hungary may veto energy provisions', source: 'Halász spokesperson', confidence: 'alleged' },
		  ],
		  sentiment: 'mixed', keywords: ['sanctions', 'EU', 'energy', 'Russia'],
		  created_at: '2026-03-22T08:00:00Z', updated_at: '2026-03-22T14:00:00Z' },

		{ cluster_id: 'cluster_grain', headline: 'Grain corridor extension talks stall in Istanbul',
		  summary: 'Turkey-mediated negotiations on extending the Black Sea grain corridor failed to reach agreement. Ukraine accused Russia of using grain exports as leverage. UN Secretary General Teixeira urged both sides to compromise. Global wheat futures rose 3% on the news.',
		  event_type: 'diplomatic', event_date: '2026-03-21', source_count: 3, article_count: 3,
		  people: [
			{ name: 'António Teixeira', role: 'UN Secretary General', context: 'Urged compromise on grain deal' },
			{ name: 'Kemal Aydın', role: 'President of Turkey', context: 'Hosting mediation talks' },
		  ],
		  organizations: [
			{ name: 'United Nations', type: 'government', context: 'Mediating grain corridor extension' },
		  ],
		  locations: [
			{ name: 'Istanbul', lat: 41.0082, lon: 28.9784, country: 'Turkey', context: 'Negotiation venue' },
			{ name: 'Odesa', lat: 46.4825, lon: 30.7233, country: 'Ukraine', context: 'Key grain export port' },
		  ],
		  conflicts: [],
		  key_claims: [
			{ claim: 'Talks failed to reach agreement', source: 'Turkish FM office', confidence: 'confirmed' },
			{ claim: 'Wheat futures rose 3%', source: 'Bloomberg', confidence: 'confirmed' },
		  ],
		  sentiment: 'negative', keywords: ['grain', 'Black Sea', 'Turkey', 'wheat'],
		  created_at: '2026-03-21T14:00:00Z', updated_at: '2026-03-21T18:00:00Z' },
	], count: 3 }),

	watchTopicArticles: (_id: string, _clusterId?: string) => delay({ articles: [
		{ article_id: 'art_001', cluster_id: 'cluster_zap_drone', title: 'Three drones intercepted over Zaporizhzhia nuclear plant',
		  url: 'https://news.example.com/world/europe/zaporizhzhia-drone-strikes', source_name: 'Reuters', author: 'Staff Reporter',
		  published_at: '2026-03-22T04:00:00Z', word_count: 847, processing_status: 'clustered' },
		{ article_id: 'art_002', cluster_id: 'cluster_zap_drone', title: 'Ukraine reports drone attacks near nuclear plant',
		  url: 'https://news.example.org/news/world-europe-zaporizhzhia', source_name: 'BBC', author: 'Staff Reporter',
		  published_at: '2026-03-22T05:30:00Z', word_count: 623, processing_status: 'clustered' },
		{ article_id: 'art_003', cluster_id: 'cluster_zap_drone', title: 'IAEA condemns latest attack on Zaporizhzhia plant',
		  url: 'https://aljazeera.com/news/zaporizhzhia-iaea', source_name: 'Al Jazeera', author: null,
		  published_at: '2026-03-22T07:00:00Z', word_count: 534, processing_status: 'clustered' },
		{ article_id: 'art_004', cluster_id: 'cluster_sanctions', title: 'EU scrambles for new Russia sanctions after nuclear plant attack',
		  url: 'https://news.example.com/world/europe/eu-14th-sanctions', source_name: 'Reuters', author: 'Staff Reporter',
		  published_at: '2026-03-22T09:00:00Z', word_count: 912, processing_status: 'clustered' },
		{ article_id: 'art_005', cluster_id: 'cluster_grain', title: 'Black Sea grain talks collapse in Istanbul',
		  url: 'https://news.example.org/news/world-grain-corridor', source_name: 'BBC', author: 'Staff Reporter',
		  published_at: '2026-03-21T15:00:00Z', word_count: 701, processing_status: 'clustered' },
	], count: 5 }),

	watchTopicEntities: (_id: string) => delay({ entities: [
		{ entity_id: 'ent_petrenko', entity_type: 'person', name: 'Andriy Petrenko', aliases: ['Petrenko'],
		  mention_count: 87, first_seen: '2026-02-01T09:00:00Z', last_seen: '2026-03-22T14:00:00Z',
		  properties: [
			{ key: 'role', value: 'President of Ukraine', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
			{ key: 'action', value: 'Called for emergency UN Security Council session', source: 'art_001', confidence: 1.0, discovered_at: '2026-03-22T04:00:00Z' },
			{ key: 'location', value: 'Kyiv', source: 'art_004', confidence: 1.0, discovered_at: '2026-03-22T09:00:00Z' },
		  ], article_ids: ['art_001', 'art_002', 'art_004'], promoted_to: null },
		{ entity_id: 'ent_ferri', entity_type: 'person', name: 'Marco Ferri', aliases: [],
		  mention_count: 23, first_seen: '2026-02-10T09:00:00Z', last_seen: '2026-03-22T07:00:00Z',
		  properties: [
			{ key: 'role', value: 'IAEA Director General', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-10T09:00:00Z' },
			{ key: 'quote', value: 'serious escalation risking nuclear disaster', source: 'art_001', confidence: 1.0, discovered_at: '2026-03-22T04:00:00Z' },
			{ key: 'action', value: 'Led 5th inspection visit to Zaporizhzhia NPP', source: 'art_003', confidence: 1.0, discovered_at: '2026-03-18T10:00:00Z' },
		  ], article_ids: ['art_001', 'art_003'], promoted_to: null },
		{ entity_id: 'ent_castell', entity_type: 'person', name: 'Jordi Castell', aliases: [],
		  mention_count: 15, first_seen: '2026-02-15T09:00:00Z', last_seen: '2026-03-22T09:00:00Z',
		  properties: [
			{ key: 'role', value: 'EU foreign policy chief', source: 'art_004', confidence: 1.0, discovered_at: '2026-02-15T09:00:00Z' },
			{ key: 'action', value: 'Announced emergency EU meeting for 14th sanctions', source: 'art_004', confidence: 1.0, discovered_at: '2026-03-22T09:00:00Z' },
		  ], article_ids: ['art_004'], promoted_to: null },
		{ entity_id: 'ent_nato', entity_type: 'organization', name: 'NATO', aliases: ['North Atlantic Treaty Organization'],
		  mention_count: 45, first_seen: '2026-02-01T09:00:00Z', last_seen: '2026-03-22T14:00:00Z',
		  properties: [
			{ key: 'type', value: 'Military alliance', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
		  ], article_ids: ['art_001', 'art_002', 'art_004'], promoted_to: null },
		{ entity_id: 'ent_iaea', entity_type: 'organization', name: 'IAEA', aliases: ['International Atomic Energy Agency'],
		  mention_count: 34, first_seen: '2026-02-05T09:00:00Z', last_seen: '2026-03-22T07:00:00Z',
		  properties: [
			{ key: 'type', value: 'International organization', source: 'art_003', confidence: 1.0, discovered_at: '2026-02-05T09:00:00Z' },
			{ key: 'action', value: 'Inspectors confirmed no reactor damage', source: 'art_003', confidence: 1.0, discovered_at: '2026-03-22T07:00:00Z' },
		  ], article_ids: ['art_001', 'art_003'], promoted_to: null },
		{ entity_id: 'ent_zaporizhzhia', entity_type: 'location', name: 'Zaporizhzhia', aliases: ['Zaporizhzhia NPP'],
		  mention_count: 56, first_seen: '2026-02-01T09:00:00Z', last_seen: '2026-03-22T07:00:00Z',
		  latitude: 47.8388, longitude: 35.1396,
		  properties: [
			{ key: 'type', value: 'City / Nuclear facility', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
			{ key: 'context', value: "Europe's largest nuclear power plant", source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
			{ key: 'status', value: 'Under Russian military control since March 2022', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
		  ], article_ids: ['art_001', 'art_002', 'art_003'], promoted_to: null },
		{ entity_id: 'ent_kyiv', entity_type: 'location', name: 'Kyiv', aliases: ['Kiev'],
		  mention_count: 78, first_seen: '2026-02-01T09:00:00Z', last_seen: '2026-03-22T14:00:00Z',
		  latitude: 50.4501, longitude: 30.5234,
		  properties: [
			{ key: 'type', value: 'Capital city', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
		  ], article_ids: ['art_001', 'art_002', 'art_004'], promoted_to: null },
		{ entity_id: 'ent_brussels', entity_type: 'location', name: 'Brussels', aliases: [],
		  mention_count: 18, first_seen: '2026-02-15T09:00:00Z', last_seen: '2026-03-22T09:00:00Z',
		  latitude: 50.8503, longitude: 4.3517,
		  properties: [
			{ key: 'type', value: 'City / EU headquarters', source: 'art_004', confidence: 1.0, discovered_at: '2026-02-15T09:00:00Z' },
		  ], article_ids: ['art_004'], promoted_to: null },
		{ entity_id: 'ent_ukraine', entity_type: 'country', name: 'Ukraine', aliases: [],
		  mention_count: 234, first_seen: '2026-02-01T09:00:00Z', last_seen: '2026-03-22T16:00:00Z',
		  latitude: 48.3794, longitude: 31.1656,
		  properties: [
			{ key: 'type', value: 'Country', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
			{ key: 'capital', value: 'Kyiv', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
			{ key: 'region', value: 'Eastern Europe', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
		  ], article_ids: ['art_001', 'art_002', 'art_003', 'art_004', 'art_005'], promoted_to: null },
		{ entity_id: 'ent_russia', entity_type: 'country', name: 'Russia', aliases: ['Russian Federation'],
		  mention_count: 189, first_seen: '2026-02-01T09:00:00Z', last_seen: '2026-03-22T14:00:00Z',
		  latitude: 55.7558, longitude: 37.6173,
		  properties: [
			{ key: 'type', value: 'Country', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
			{ key: 'capital', value: 'Moscow', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
			{ key: 'role_in_conflict', value: 'Occupying force at Zaporizhzhia NPP since March 2022', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
		  ], article_ids: ['art_001', 'art_002', 'art_004'], promoted_to: null },
		{ entity_id: 'ent_turkey', entity_type: 'country', name: 'Turkey', aliases: ['Türkiye', 'Republic of Turkey'],
		  mention_count: 34, first_seen: '2026-02-10T09:00:00Z', last_seen: '2026-03-21T18:00:00Z',
		  latitude: 39.9334, longitude: 32.8597,
		  properties: [
			{ key: 'type', value: 'Country', source: 'art_005', confidence: 1.0, discovered_at: '2026-02-10T09:00:00Z' },
			{ key: 'role', value: 'Mediator in grain corridor negotiations', source: 'art_005', confidence: 1.0, discovered_at: '2026-03-21T14:00:00Z' },
		  ], article_ids: ['art_005'], promoted_to: null },
		{ entity_id: 'ent_belgium', entity_type: 'country', name: 'Belgium', aliases: [],
		  mention_count: 12, first_seen: '2026-02-15T09:00:00Z', last_seen: '2026-03-22T09:00:00Z',
		  latitude: 50.5039, longitude: 4.4699,
		  properties: [
			{ key: 'type', value: 'Country', source: 'art_004', confidence: 1.0, discovered_at: '2026-02-15T09:00:00Z' },
			{ key: 'significance', value: 'EU & NATO headquarters located in Brussels', source: 'art_004', confidence: 1.0, discovered_at: '2026-02-15T09:00:00Z' },
		  ], article_ids: ['art_004'], promoted_to: null },
		{ entity_id: 'ent_hungary', entity_type: 'country', name: 'Hungary', aliases: [],
		  mention_count: 8, first_seen: '2026-03-22T09:00:00Z', last_seen: '2026-03-22T14:00:00Z',
		  latitude: 47.1625, longitude: 19.5033,
		  properties: [
			{ key: 'type', value: 'Country', source: 'art_004', confidence: 1.0, discovered_at: '2026-03-22T09:00:00Z' },
			{ key: 'role', value: 'Potential veto on EU energy sanctions', source: 'art_004', confidence: 1.0, discovered_at: '2026-03-22T09:00:00Z' },
		  ], article_ids: ['art_004'], promoted_to: null },
		{ entity_id: 'ent_donbas', entity_type: 'region', name: 'Donbas', aliases: ['Donbass', 'Donetsk Oblast', 'Luhansk Oblast'],
		  mention_count: 45, first_seen: '2026-02-01T09:00:00Z', last_seen: '2026-03-22T16:00:00Z',
		  latitude: 48.0159, longitude: 37.8028,
		  properties: [
			{ key: 'type', value: 'Region (Eastern Ukraine)', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
			{ key: 'status', value: 'Active conflict zone', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
		  ], article_ids: ['art_001', 'art_002'], promoted_to: null },
		{ entity_id: 'ent_crimea', entity_type: 'region', name: 'Crimea', aliases: ['Crimean Peninsula'],
		  mention_count: 28, first_seen: '2026-02-01T09:00:00Z', last_seen: '2026-03-20T09:00:00Z',
		  latitude: 44.9521, longitude: 34.1024,
		  properties: [
			{ key: 'type', value: 'Peninsula (disputed)', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
			{ key: 'status', value: 'Annexed by Russia 2014, internationally disputed', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
		  ], article_ids: ['art_001'], promoted_to: null },
		{ entity_id: 'ent_istanbul', entity_type: 'location', name: 'Istanbul', aliases: [],
		  mention_count: 12, first_seen: '2026-03-21T14:00:00Z', last_seen: '2026-03-21T18:00:00Z',
		  latitude: 41.0082, longitude: 28.9784,
		  properties: [
			{ key: 'type', value: 'City', source: 'art_005', confidence: 1.0, discovered_at: '2026-03-21T14:00:00Z' },
			{ key: 'significance', value: 'Venue for grain corridor negotiations', source: 'art_005', confidence: 1.0, discovered_at: '2026-03-21T14:00:00Z' },
		  ], article_ids: ['art_005'], promoted_to: null },
		{ entity_id: 'ent_odesa', entity_type: 'location', name: 'Odesa', aliases: ['Odessa'],
		  mention_count: 15, first_seen: '2026-02-05T09:00:00Z', last_seen: '2026-03-21T18:00:00Z',
		  latitude: 46.4825, longitude: 30.7233,
		  properties: [
			{ key: 'type', value: 'City / Port', source: 'art_005', confidence: 1.0, discovered_at: '2026-02-05T09:00:00Z' },
			{ key: 'significance', value: 'Key grain export port on Black Sea', source: 'art_005', confidence: 1.0, discovered_at: '2026-02-05T09:00:00Z' },
		  ], article_ids: ['art_005'], promoted_to: null },
		{ entity_id: 'ent_eu', entity_type: 'organization', name: 'European Union', aliases: ['EU'],
		  mention_count: 67, first_seen: '2026-02-01T09:00:00Z', last_seen: '2026-03-22T14:00:00Z',
		  properties: [
			{ key: 'type', value: 'Supranational organization', source: 'art_004', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
			{ key: 'action', value: 'Drafting 14th sanctions package against Russia', source: 'art_004', confidence: 1.0, discovered_at: '2026-03-22T09:00:00Z' },
		  ], article_ids: ['art_001', 'art_004'], promoted_to: null },
		{ entity_id: 'ent_un', entity_type: 'organization', name: 'United Nations', aliases: ['UN'],
		  mention_count: 34, first_seen: '2026-02-01T09:00:00Z', last_seen: '2026-03-22T14:00:00Z',
		  properties: [
			{ key: 'type', value: 'International organization', source: 'art_001', confidence: 1.0, discovered_at: '2026-02-01T09:00:00Z' },
			{ key: 'action', value: 'Emergency Security Council session requested', source: 'art_001', confidence: 1.0, discovered_at: '2026-03-22T04:00:00Z' },
		  ], article_ids: ['art_001', 'art_005'], promoted_to: null },
		{ entity_id: 'ent_halasz', entity_type: 'person', name: 'Viktor Halász', aliases: ['Halasz'],
		  mention_count: 8, first_seen: '2026-03-22T09:00:00Z', last_seen: '2026-03-22T14:00:00Z',
		  properties: [
			{ key: 'role', value: 'Prime Minister of Hungary', source: 'art_004', confidence: 1.0, discovered_at: '2026-03-22T09:00:00Z' },
			{ key: 'action', value: 'Signaling potential veto on EU energy sanctions', source: 'art_004', confidence: 1.0, discovered_at: '2026-03-22T09:00:00Z' },
		  ], article_ids: ['art_004'], promoted_to: null },
		{ entity_id: 'ent_teixeira', entity_type: 'person', name: 'António Teixeira', aliases: ['Teixeira'],
		  mention_count: 12, first_seen: '2026-02-10T09:00:00Z', last_seen: '2026-03-21T18:00:00Z',
		  properties: [
			{ key: 'role', value: 'UN Secretary General', source: 'art_005', confidence: 1.0, discovered_at: '2026-02-10T09:00:00Z' },
			{ key: 'action', value: 'Urged compromise on grain corridor extension', source: 'art_005', confidence: 1.0, discovered_at: '2026-03-21T14:00:00Z' },
		  ], article_ids: ['art_005'], promoted_to: null },
		{ entity_id: 'ent_aydin', entity_type: 'person', name: 'Kemal Aydın', aliases: ['Aydin'],
		  mention_count: 10, first_seen: '2026-02-10T09:00:00Z', last_seen: '2026-03-21T18:00:00Z',
		  properties: [
			{ key: 'role', value: 'President of Turkey', source: 'art_005', confidence: 1.0, discovered_at: '2026-02-10T09:00:00Z' },
			{ key: 'action', value: 'Hosting grain corridor mediation talks in Istanbul', source: 'art_005', confidence: 1.0, discovered_at: '2026-03-21T14:00:00Z' },
		  ], article_ids: ['art_005'], promoted_to: null },
	], count: 21 }),

	watchTopicTimeline: (_id: string) => delay({ events: [
		{ id: 'evt_001', timestamp: '2022-03-04T00:00:00Z', title: 'Russian forces seize Zaporizhzhia NPP', event_type: 'military_action', latitude: 47.5069, longitude: 34.5853 },
		{ id: 'evt_002', timestamp: '2022-07-22T00:00:00Z', title: 'Black Sea grain corridor agreement signed', event_type: 'diplomatic', latitude: 41.0082, longitude: 28.9784 },
		{ id: 'evt_003', timestamp: '2023-06-15T00:00:00Z', title: 'Russia exits grain corridor deal', event_type: 'diplomatic', latitude: 55.7558, longitude: 37.6173 },
		{ id: 'evt_004', timestamp: '2024-02-01T00:00:00Z', title: 'EU adopts 13th sanctions package', event_type: 'diplomatic', latitude: 50.8503, longitude: 4.3517 },
		{ id: 'evt_005', timestamp: '2025-06-20T00:00:00Z', title: 'IAEA 3rd inspection of Zaporizhzhia NPP', event_type: 'diplomatic', latitude: 47.5069, longitude: 34.5853 },
		{ id: 'evt_006', timestamp: '2025-11-15T00:00:00Z', title: 'Donbas offensive intensifies', event_type: 'military_action', latitude: 48.0159, longitude: 37.8028 },
		{ id: 'evt_007', timestamp: '2026-02-01T00:00:00Z', title: 'Enki begins monitoring Ukraine conflict', event_type: 'article', latitude: null, longitude: null },
		{ id: 'evt_008', timestamp: '2026-03-10T00:00:00Z', title: 'Petrenko addresses NATO summit', event_type: 'diplomatic', latitude: 50.8503, longitude: 4.3517 },
		{ id: 'evt_009', timestamp: '2026-03-15T00:00:00Z', title: 'Ferri briefed UN Security Council', event_type: 'diplomatic', latitude: 40.7484, longitude: -73.9857 },
		{ id: 'evt_010', timestamp: '2026-03-18T00:00:00Z', title: 'IAEA 5th inspection visit to Zaporizhzhia', event_type: 'diplomatic', latitude: 47.5069, longitude: 34.5853 },
		{ id: 'evt_011', timestamp: '2026-03-20T00:00:00Z', title: 'Ferri calls for permanent IAEA presence', event_type: 'diplomatic', latitude: 47.5069, longitude: 34.5853 },
		{ id: 'evt_012', timestamp: '2026-03-21T14:00:00Z', title: 'Grain corridor talks stall in Istanbul', event_type: 'diplomatic', latitude: 41.0082, longitude: 28.9784 },
		{ id: 'evt_013', timestamp: '2026-03-22T03:47:00Z', title: 'Three drones intercepted over Zaporizhzhia NPP', event_type: 'military_action', latitude: 47.5069, longitude: 34.5853 },
		{ id: 'evt_014', timestamp: '2026-03-22T07:00:00Z', title: 'IAEA condemns Zaporizhzhia drone attack', event_type: 'diplomatic', latitude: 47.5069, longitude: 34.5853 },
		{ id: 'evt_015', timestamp: '2026-03-22T09:00:00Z', title: 'EU announces emergency meeting for 14th sanctions', event_type: 'diplomatic', latitude: 50.8503, longitude: 4.3517 },
		{ id: 'evt_016', timestamp: '2026-03-22T14:00:00Z', title: 'Petrenko calls emergency UN Security Council session', event_type: 'diplomatic', latitude: 50.4501, longitude: 30.5234 },
		{ id: 'evt_017', timestamp: '2026-03-22T14:30:00Z', title: 'Hungary signals potential veto on energy sanctions', event_type: 'political', latitude: 47.4979, longitude: 19.0402 },
	]}),

	watchTopicGraph: (_id: string, _focalId?: string) => delay({ nodes: [
		{ id: 'ent_petrenko', title: 'Andriy Petrenko', entity_type: 'person', source_authority: 'verified', mention_count: 87, traverse_depth: 0 },
		{ id: 'ent_ferri', title: 'Marco Ferri', entity_type: 'person', source_authority: 'verified', mention_count: 23, traverse_depth: 1 },
		{ id: 'ent_castell', title: 'Jordi Castell', entity_type: 'person', source_authority: 'verified', mention_count: 15, traverse_depth: 1 },
		{ id: 'ent_halasz', title: 'Viktor Halász', entity_type: 'person', source_authority: 'verified', mention_count: 8, traverse_depth: 2 },
		{ id: 'ent_teixeira', title: 'António Teixeira', entity_type: 'person', source_authority: 'verified', mention_count: 12, traverse_depth: 2 },
		{ id: 'ent_aydin', title: 'Kemal Aydın', entity_type: 'person', source_authority: 'verified', mention_count: 10, traverse_depth: 2 },
		{ id: 'ent_ukraine', title: 'Ukraine', entity_type: 'country', source_authority: 'verified', mention_count: 234, traverse_depth: 1 },
		{ id: 'ent_russia', title: 'Russia', entity_type: 'country', source_authority: 'verified', mention_count: 189, traverse_depth: 1 },
		{ id: 'ent_turkey', title: 'Turkey', entity_type: 'country', source_authority: 'verified', mention_count: 34, traverse_depth: 2 },
		{ id: 'ent_belgium', title: 'Belgium', entity_type: 'country', source_authority: 'verified', mention_count: 12, traverse_depth: 3 },
		{ id: 'ent_hungary', title: 'Hungary', entity_type: 'country', source_authority: 'verified', mention_count: 8, traverse_depth: 3 },
		{ id: 'ent_zaporizhzhia', title: 'Zaporizhzhia', entity_type: 'location', source_authority: 'verified', mention_count: 56, traverse_depth: 1 },
		{ id: 'ent_kyiv', title: 'Kyiv', entity_type: 'location', source_authority: 'verified', mention_count: 78, traverse_depth: 2 },
		{ id: 'ent_donbas', title: 'Donbas', entity_type: 'region', source_authority: 'verified', mention_count: 45, traverse_depth: 2 },
		{ id: 'ent_crimea', title: 'Crimea', entity_type: 'region', source_authority: 'disputed', mention_count: 28, traverse_depth: 2 },
		{ id: 'ent_istanbul', title: 'Istanbul', entity_type: 'location', source_authority: 'verified', mention_count: 12, traverse_depth: 3 },
		{ id: 'ent_odesa', title: 'Odesa', entity_type: 'location', source_authority: 'verified', mention_count: 15, traverse_depth: 2 },
		{ id: 'ent_brussels', title: 'Brussels', entity_type: 'location', source_authority: 'verified', mention_count: 18, traverse_depth: 2 },
		{ id: 'ent_iaea', title: 'IAEA', entity_type: 'organization', source_authority: 'verified', mention_count: 34, traverse_depth: 1 },
		{ id: 'ent_nato', title: 'NATO', entity_type: 'organization', source_authority: 'verified', mention_count: 45, traverse_depth: 1 },
		{ id: 'ent_eu', title: 'European Union', entity_type: 'organization', source_authority: 'verified', mention_count: 67, traverse_depth: 1 },
		{ id: 'ent_un', title: 'United Nations', entity_type: 'organization', source_authority: 'verified', mention_count: 34, traverse_depth: 2 },
	], edges: [
		{ source: 'ent_petrenko', target: 'ent_ukraine', relationship: 'leads', strength: 1.0 },
		{ source: 'ent_petrenko', target: 'ent_ferri', relationship: 'met_with', strength: 0.8 },
		{ source: 'ent_petrenko', target: 'ent_nato', relationship: 'member_of', strength: 0.9 },
		{ source: 'ent_petrenko', target: 'ent_un', relationship: 'addressed', strength: 0.7 },
		{ source: 'ent_ferri', target: 'ent_iaea', relationship: 'leads', strength: 1.0 },
		{ source: 'ent_ferri', target: 'ent_zaporizhzhia', relationship: 'inspected', strength: 0.95 },
		{ source: 'ent_castell', target: 'ent_eu', relationship: 'leads', strength: 1.0 },
		{ source: 'ent_castell', target: 'ent_brussels', relationship: 'located_in', strength: 0.9 },
		{ source: 'ent_halasz', target: 'ent_hungary', relationship: 'leads', strength: 1.0 },
		{ source: 'ent_halasz', target: 'ent_eu', relationship: 'contrasts', strength: 0.7, evidence: 'Potential veto on energy sanctions' },
		{ source: 'ent_teixeira', target: 'ent_un', relationship: 'leads', strength: 1.0 },
		{ source: 'ent_aydin', target: 'ent_turkey', relationship: 'leads', strength: 1.0 },
		{ source: 'ent_aydin', target: 'ent_istanbul', relationship: 'located_in', strength: 0.8 },
		{ source: 'ent_zaporizhzhia', target: 'ent_ukraine', relationship: 'located_in', strength: 1.0 },
		{ source: 'ent_kyiv', target: 'ent_ukraine', relationship: 'capital_of', strength: 1.0 },
		{ source: 'ent_donbas', target: 'ent_ukraine', relationship: 'located_in', strength: 1.0 },
		{ source: 'ent_crimea', target: 'ent_ukraine', relationship: 'located_in', strength: 0.8 },
		{ source: 'ent_crimea', target: 'ent_russia', relationship: 'contrasts', strength: 0.6, evidence: 'Annexed 2014, internationally disputed' },
		{ source: 'ent_odesa', target: 'ent_ukraine', relationship: 'located_in', strength: 1.0 },
		{ source: 'ent_brussels', target: 'ent_belgium', relationship: 'located_in', strength: 1.0 },
		{ source: 'ent_eu', target: 'ent_brussels', relationship: 'located_in', strength: 0.9 },
		{ source: 'ent_nato', target: 'ent_brussels', relationship: 'located_in', strength: 0.9 },
		{ source: 'ent_istanbul', target: 'ent_turkey', relationship: 'located_in', strength: 1.0 },
		{ source: 'ent_russia', target: 'ent_zaporizhzhia', relationship: 'contrasts', strength: 0.8, evidence: 'Occupying force since March 2022' },
		{ source: 'ent_iaea', target: 'ent_un', relationship: 'member_of', strength: 0.8 },
		{ source: 'ent_hungary', target: 'ent_eu', relationship: 'member_of', strength: 0.9 },
	]}),

	// ─── Research ───────────────────────────────────────────────────────
	researchEntity: (_name: string, _context?: any) => new Promise(resolve => setTimeout(() => resolve({
		status: 'complete',
		profile: {
			full_name: 'Marco Antonio Ferri',
			nationality: 'Argentine',
			date_of_birth: '1960-01-01',
			current_role: 'Director General of the IAEA',
			organization: 'International Atomic Energy Agency',
			bio: 'Argentine diplomat serving as Director General of the International Atomic Energy Agency since December 2019. Previously served as Argentine Ambassador to Austria and as IAEA Chief of Cabinet. Known for leading high-stakes nuclear inspections in conflict zones including Ukraine and Iran.',
			career_history: [
				{ role: 'IAEA Director General', period: '2019–present', source: 'Wikipedia', verified: true },
				{ role: 'Argentine Ambassador to Austria', period: '2013–2019', source: 'Wikipedia', verified: true },
				{ role: 'IAEA Chief of Cabinet', period: '2010–2013', source: 'Wikipedia', verified: true },
				{ role: 'Argentine Undersecretary for Foreign Policy', period: '2007–2010', source: 'MFA Argentina', verified: true },
				{ role: 'President, NSG (Nuclear Suppliers Group)', period: '2007–2008', source: 'Wikipedia', verified: true },
			],
			education: [
				{ institution: 'Universidad del Salvador', degree: 'Law', source: 'Wikipedia' },
				{ institution: 'Graduate Institute Geneva', degree: 'International Relations', source: 'IAEA bio' },
			],
			social_handles: {
				twitter: '@example',
			},
			images: [
				{ url: 'https://example.org/portrait.jpg', caption: 'Official IAEA portrait', source: 'iaea.org' },
			],
			links: [
				{ label: 'IAEA Official Page', url: 'https://example.org/director' },
				{ label: 'Wikipedia', url: 'https://example.org/wiki/Marco_Ferri' },
				{ label: 'Twitter/X', url: 'https://example.org/social' },
				{ label: 'UN Profile', url: 'https://example.org/letter.pdf' },
			],
			key_associations: [
				{ entity: 'IAEA', relationship: 'leads', type: 'organization', since: '2019', in_topic: true },
				{ entity: 'United Nations', relationship: 'affiliated', type: 'organization', since: '2010', in_topic: false },
				{ entity: 'Nuclear Suppliers Group', relationship: 'former president', type: 'organization', since: '2007', in_topic: false },
				{ entity: 'Zaporizhzhia NPP', relationship: 'led inspections', type: 'location', since: '2022', in_topic: true },
				{ entity: 'Iran JCPOA', relationship: 'key negotiator', type: 'topic', since: '2020', in_topic: false },
				{ entity: 'Mikhail Sorokin', relationship: 'Russian counterpart', type: 'person', since: '2020', in_topic: false },
				{ entity: 'Andriy Petrenko', relationship: 'met with regarding NPP', type: 'person', since: '2022', in_topic: true },
			],
			notable_actions: [
				{ action: 'Led 5th inspection visit to Zaporizhzhia NPP', date: '2026-03-18', source: 'IAEA press', type: 'observation' },
				{ action: 'Condemned drone strikes on Zaporizhzhia as serious escalation', date: '2026-03-22', source: 'Reuters', type: 'fact' },
				{ action: 'Briefed UN Security Council on nuclear plant safety', date: '2026-03-15', source: 'UN records', type: 'fact' },
				{ action: 'Negotiated IAEA monitoring access under Iran JCPOA', date: '2020-01-15', source: 'Wikipedia', type: 'fact' },
				{ action: 'Called for permanent IAEA presence at Zaporizhzhia', date: '2026-03-20', source: 'Al Jazeera', type: 'observation' },
			],
			conflicts: [
				{ field: 'Stance on Iran inspections', positions: [
					{ stance: 'Maintained IAEA neutrality and impartiality throughout Iran negotiations', sources: ['Wikipedia', 'IAEA official statements'] },
					{ stance: 'Criticized for being too lenient on Iran inspection access', sources: ['Reuters 2023', 'Wall Street Journal'] },
				]},
			],
			dates_mentioned: [
				{ date: '2019-12-02', event: 'Assumed office as IAEA Director General' },
				{ date: '2022-03-04', event: 'First visit to Zaporizhzhia NPP after Russian seizure' },
				{ date: '2026-03-22', event: 'Condemned drone strikes on Zaporizhzhia' },
			],
			knowledge_nodes_created: [
				{ node_id: 'node_ferri_profile', title: 'Marco Antonio Ferri', epistemic_type: 'fact', source_authority: 'verified' },
				{ node_id: 'node_ferri_iaea', title: 'IAEA Director General since 2019', epistemic_type: 'fact', source_authority: 'verified' },
				{ node_id: 'node_ferri_inspections', title: 'Led Zaporizhzhia NPP inspections', epistemic_type: 'observation', source_authority: 'verified' },
				{ node_id: 'node_ferri_iran', title: 'Iran JCPOA monitoring negotiator', epistemic_type: 'fact', source_authority: 'verified' },
				{ node_id: 'node_ferri_neutral', title: 'Maintained IAEA neutrality on Iran', epistemic_type: 'claim', source_authority: 'verified' },
				{ node_id: 'node_ferri_lenient', title: 'Criticized as too lenient on Iran', epistemic_type: 'claim', source_authority: 'disputed' },
			],
			signal_ids: ['sig_research_ferri_001', 'sig_research_ferri_002'],
		},
	}), 1500)),

	aiProviders: () => delay({ providers: [
		{ name: 'ollama', type: 'local', description: 'Local models via Ollama. Data never leaves server.' },
		{ name: 'openai', type: 'cloud', description: 'OpenAI (GPT-4o, o3-mini)' },
		{ name: 'anthropic', type: 'cloud', description: 'Anthropic (Claude Opus, Sonnet, Haiku)' },
		{ name: 'gemini', type: 'cloud', description: 'Google Gemini' },
		{ name: 'grok', type: 'cloud', description: 'xAI Grok' },
		{ name: 'groq', type: 'cloud', description: 'Groq (fast inference)' },
	]}),

	contacts: () => delay({ contacts: [
		{ contact_id: 'contact_001', contact_type: 'person', primary_name: 'Sarah Chen', aliases: ['S. Chen'],
		  email: 'sarah.chen@example.com', phone: '+1-555-0142', role_title: 'VP of Engineering',
		  company_id: 'contact_004', status: 'active', threat_level: 'none', tags: ['employee', 'tech'],
		  notes: 'Primary contact for TechCorp security integration project.',
		  created_at: '2026-02-15T09:00:00Z', updated_at: '2026-03-22T14:00:00Z' },
		{ contact_id: 'contact_002', contact_type: 'person', primary_name: 'Marcus Rivera', aliases: ['M. Rivera', 'Marc'],
		  email: 'mrivera@example.com', phone: '+1-555-0198', role_title: 'Private Investigator',
		  company_id: null, status: 'active', threat_level: 'none', tags: ['contractor', 'investigator'],
		  notes: 'Licensed PI, subcontracted for vehicle theft case #47.',
		  created_at: '2026-01-10T10:00:00Z', updated_at: '2026-03-20T16:00:00Z' },
		{ contact_id: 'contact_003', contact_type: 'person', primary_name: 'Alexei Volkov', aliases: ['A. Volkov', 'Lex'],
		  email: 'a.volkov@example.com', phone: null, role_title: null,
		  company_id: null, status: 'watchlist', threat_level: 'high', tags: ['poi', 'cyber'],
		  social_handles: { twitter: '@alexei_v', telegram: '@lex_ops', github: 'alexei-v' },
		  notes: 'Person of interest — network intrusion analysis case #63. Multiple aliases confirmed.',
		  created_at: '2026-03-22T08:30:00Z', updated_at: '2026-03-22T15:00:00Z' },
		{ contact_id: 'contact_004', contact_type: 'company', primary_name: 'TechCorp Industries', aliases: ['TechCorp', 'TCI'],
		  email: 'info@example.com', phone: '+1-800-555-TECH', role_title: null,
		  company_id: null, status: 'active', threat_level: 'none', tags: ['client', 'enterprise'],
		  notes: 'Major client — 3-year security monitoring contract. HQ in Arlington, VA.',
		  enrichment: { sec: { name: 'TechCorp Industries Inc', cik: '0001234567', sic_description: 'Computer Programming', state: 'VA' },
		    gleif: { lei: '529900T8BM49AURSDO55', jurisdiction: 'US-VA', status: 'ACTIVE', country: 'US' }},
		  created_at: '2026-01-05T08:00:00Z', updated_at: '2026-03-18T11:00:00Z' },
		{ contact_id: 'contact_005', contact_type: 'company', primary_name: 'Meridian Security Group', aliases: ['MSG', 'Meridian'],
		  email: 'ops@example.org', phone: '+1-555-0300', role_title: null,
		  company_id: null, status: 'active', threat_level: 'none', tags: ['partner', 'security'],
		  notes: 'Physical security partner — provides on-site guards and ALPR monitoring.',
		  created_at: '2026-02-01T09:00:00Z', updated_at: '2026-03-15T10:00:00Z' },
		{ contact_id: 'contact_006', contact_type: 'person', primary_name: 'Diana Okafor', aliases: [],
		  email: 'diana.okafor@example.org', phone: '+1-555-0312', role_title: 'Operations Director',
		  company_id: 'contact_005', status: 'active', threat_level: 'none', tags: ['partner', 'security'],
		  notes: 'Main POC at Meridian Security. Coordinates ALPR camera deployments.',
		  created_at: '2026-02-05T10:00:00Z', updated_at: '2026-03-10T14:00:00Z' },
		{ contact_id: 'contact_007', contact_type: 'address', primary_name: '1847 Industrial Blvd, Arlington VA 22201', aliases: ['TechCorp HQ'],
		  email: null, phone: null, role_title: null,
		  company_id: 'contact_004', status: 'active', threat_level: 'none', tags: ['location', 'hq'],
		  notes: 'TechCorp main office. 3 floors, badge access, CCTV monitored.',
		  created_at: '2026-01-05T08:00:00Z', updated_at: '2026-01-05T08:00:00Z' },
		{ contact_id: 'contact_ukraine', contact_type: 'country', primary_name: 'Ukraine', aliases: ['Україна'],
		  email: null, phone: null, role_title: null,
		  company_id: null, status: 'watchlist', threat_level: 'high', tags: ['conflict', 'eastern-europe'],
		  notes: 'Active conflict zone. Monitoring military operations, humanitarian impact, and diplomatic efforts.',
		  metadata: { capital: 'Kyiv', region: 'Eastern Europe', population: '44 million', latitude: 48.3794, longitude: 31.1656 },
		  created_at: '2026-02-01T09:00:00Z', updated_at: '2026-03-22T16:00:00Z' },
		{ contact_id: 'contact_russia', contact_type: 'country', primary_name: 'Russia', aliases: ['Russian Federation', 'Россия'],
		  email: null, phone: null, role_title: null,
		  company_id: null, status: 'watchlist', threat_level: 'critical', tags: ['conflict', 'sanctions'],
		  notes: 'Occupying force at Zaporizhzhia NPP since March 2022. Subject of 14 EU sanctions packages.',
		  metadata: { capital: 'Moscow', region: 'Eastern Europe/Northern Asia', population: '144 million', latitude: 55.7558, longitude: 37.6173 },
		  created_at: '2026-02-01T09:00:00Z', updated_at: '2026-03-22T14:00:00Z' },
		{ contact_id: 'contact_turkey', contact_type: 'country', primary_name: 'Turkey', aliases: ['Türkiye', 'Republic of Turkey'],
		  email: null, phone: null, role_title: null,
		  company_id: null, status: 'active', threat_level: 'none', tags: ['mediator', 'nato-member'],
		  notes: 'Mediating grain corridor negotiations between Ukraine and Russia. NATO member state.',
		  metadata: { capital: 'Ankara', region: 'Eurasia', population: '85 million', latitude: 39.9334, longitude: 32.8597 },
		  created_at: '2026-02-10T09:00:00Z', updated_at: '2026-03-21T18:00:00Z' },
	], count: 10 }),

	contact: (id: string) => delay({
		...(id === 'contact_001' ? {
			contact_id: 'contact_001', contact_type: 'person', primary_name: 'Sarah Chen', aliases: ['S. Chen'],
			email: 'sarah.chen@example.com', phone: '+1-555-0142', role_title: 'VP of Engineering',
			company_id: 'contact_004', company_name: 'TechCorp Industries', status: 'active', threat_level: 'none',
			tags: ['employee', 'tech'], address: '4521 Fairfax Dr, Arlington VA 22203',
			social_handles: { linkedin: 'sarah-chen-techcorp', twitter: '@sarahchen_eng' },
			notes: 'Primary contact for TechCorp security integration project.',
			enrichment: {},
			created_at: '2026-02-15T09:00:00Z', updated_at: '2026-03-22T14:00:00Z',
		} : {
			contact_id: id, contact_type: 'person', primary_name: 'Unknown Contact',
			status: 'active', threat_level: 'none', tags: [], enrichment: {},
			created_at: '2026-03-01T00:00:00Z', updated_at: '2026-03-01T00:00:00Z',
		}),
	}),

	contactDocuments: (_id: string) => delay({ documents: [
		{ doc_id: 'doc_001', title: 'Background Check Report', doc_type: 'report',
		  content: 'Comprehensive background check completed 2026-02-20. No criminal history. Credit score: Excellent. Previous employment verified at 3 companies.',
		  created_by: 'Sam', created_at: '2026-02-20T10:00:00Z' },
		{ doc_id: 'doc_002', title: 'Meeting Notes — Security Integration Kickoff', doc_type: 'note',
		  content: '## Kickoff Meeting 2026-02-18\n\n- Discussed API integration timeline\n- Sarah will provide endpoint specs by March 1\n- Agreed on bi-weekly check-ins\n- NDA signed and filed',
		  created_by: 'Sam', created_at: '2026-02-18T15:00:00Z' },
		{ doc_id: 'doc_003', title: 'Business Card Scan', doc_type: 'photo',
		  file_url: '/uploads/contacts/contact_001/card.jpg', mime_type: 'image/jpeg', file_size: 245000,
		  created_by: 'Sam', created_at: '2026-02-15T09:30:00Z' },
	]}),

	contactCases: (_id: string) => delay({ cases: [
		{ case_id: 'case_51', title: 'Q3 Security Audit Review', status: 'open', priority: 'medium',
		  relationship: 'witness', linked_at: '2026-03-18T10:00:00Z' },
	]}),

	contactSignals: (_id: string) => delay({ signals: [
		{ signal_id: 'sig_a3f7_004', signal_type: 'signal.communications.email', source_domain: 'email_imap',
		  severity: 'informational', timestamp: '2026-03-22T14:31:45Z', observable_value: 'sarah.chen@example.com' },
		{ signal_id: 'sig_a3f7_008', signal_type: 'signal.personal.location', source_domain: 'phone_app',
		  severity: 'informational', timestamp: '2026-03-21T09:15:00Z', observable_value: 'Sarah Chen' },
	], count: 2 }),

	contactRelationships: (_id: string) => delay({ relationships: [
		{ rid: 1, target_id: 'contact_004', target_name: 'TechCorp Industries', target_type: 'company',
		  relationship: 'employee', description: 'VP of Engineering' },
		{ rid: 2, target_id: 'contact_006', target_name: 'Diana Okafor', target_type: 'person',
		  relationship: 'associate', description: 'Works with on ALPR integration' },
	]}),

	contactAssets: (_id: string) => delay({ assets: [
		{ asset_id: 'asset_001', display_name: '2019 BMW X5 — ABC-1234', asset_category: 'vehicle',
		  status: 'active', estimated_value: 62000, relationship: 'registered_to', linked_at: '2026-03-10T09:00:00Z' },
	]}),

	assets: () => delay({ assets: [
		{ asset_id: 'asset_001', asset_category: 'vehicle', display_name: '2019 BMW X5 — ABC-1234',
		  description: 'Black BMW X5, suspected in stolen vehicle case. Plate confirmed via ALPR.',
		  status: 'active', estimated_value: 62000, currency: 'USD', serial_number: '1HGBH41JXMN109186',
		  observables: ['license_plate:ABC-1234', 'custom:vin:1HGBH41JXMN109186'],
		  tags: ['vehicle', 'case-47'], thumbnail_url: null,
		  properties: [
			{ key: 'license_plate', value: 'ABC-1234', source: 'sig_alpr_001', confidence: 0.98, discovered_at: '2026-03-22T14:00:00Z' },
			{ key: 'make', value: 'BMW', source: 'sig_doc_registration', confidence: 1.0, discovered_at: '2026-03-22T15:00:00Z' },
			{ key: 'model', value: 'X5', source: 'sig_doc_registration', confidence: 1.0, discovered_at: '2026-03-22T15:00:00Z' },
			{ key: 'year', value: '2019', source: 'sig_doc_registration', confidence: 1.0, discovered_at: '2026-03-22T15:00:00Z' },
			{ key: 'color', value: 'Black', source: 'sig_img_003', confidence: 0.85, discovered_at: '2026-03-23T09:00:00Z' },
			{ key: 'vin', value: '1HGBH41JXMN109186', source: 'sig_doc_registration', confidence: 1.0, discovered_at: '2026-03-22T15:00:00Z' },
			{ key: 'registered_owner', value: 'Sarah Chen', source: 'sig_doc_registration', confidence: 1.0, discovered_at: '2026-03-22T15:00:00Z' },
		  ],
		  created_at: '2026-03-10T09:00:00Z', updated_at: '2026-03-23T09:00:00Z' },

		{ asset_id: 'asset_002', asset_category: 'real_estate', display_name: '1847 Industrial Blvd, Arlington VA',
		  description: 'TechCorp main office building. 3 floors, badge access, CCTV monitored.',
		  status: 'active', estimated_value: 4200000, currency: 'USD', serial_number: 'PARCEL-22201-0847',
		  observables: ['custom:parcel:22201-0847'],
		  tags: ['property', 'client-site'], thumbnail_url: null,
		  properties: [
			{ key: 'address', value: '1847 Industrial Blvd, Arlington VA 22201', source: 'manual', confidence: 1.0, discovered_at: '2026-01-05T08:00:00Z' },
			{ key: 'parcel_id', value: '22201-0847', source: 'manual', confidence: 1.0, discovered_at: '2026-01-05T08:00:00Z' },
			{ key: 'sq_ft', value: '24,500', source: 'manual', confidence: 1.0, discovered_at: '2026-01-05T08:00:00Z' },
			{ key: 'floors', value: '3', source: 'manual', confidence: 1.0, discovered_at: '2026-01-05T08:00:00Z' },
			{ key: 'security', value: 'Badge access + CCTV', source: 'manual', confidence: 1.0, discovered_at: '2026-02-01T10:00:00Z' },
			{ key: 'owner_entity', value: 'TechCorp Industries Inc', source: 'sig_doc_deed', confidence: 1.0, discovered_at: '2026-01-10T09:00:00Z' },
		  ],
		  latitude: 38.8795, longitude: -77.1013,
		  created_at: '2026-01-05T08:00:00Z', updated_at: '2026-02-01T10:00:00Z' },

		{ asset_id: 'asset_003', asset_category: 'electronics', display_name: 'MacBook Pro 16" — SN: C02ZR1YZMD6T',
		  description: 'Company-issued laptop recovered from suspect workstation.',
		  status: 'seized', estimated_value: 3200, currency: 'USD', serial_number: 'C02ZR1YZMD6T',
		  observables: ['custom:serial:C02ZR1YZMD6T', 'mac_address:A4:83:E7:2B:90:FF'],
		  tags: ['electronics', 'evidence', 'case-63'], thumbnail_url: null,
		  properties: [
			{ key: 'manufacturer', value: 'Apple', source: 'manual', confidence: 1.0, discovered_at: '2026-03-22T10:00:00Z' },
			{ key: 'model', value: 'MacBook Pro 16-inch 2023', source: 'manual', confidence: 1.0, discovered_at: '2026-03-22T10:00:00Z' },
			{ key: 'serial_number', value: 'C02ZR1YZMD6T', source: 'manual', confidence: 1.0, discovered_at: '2026-03-22T10:00:00Z' },
			{ key: 'mac_address', value: 'A4:83:E7:2B:90:FF', source: 'sig_net_scan_014', confidence: 1.0, discovered_at: '2026-03-22T11:30:00Z' },
			{ key: 'last_user', value: 'a.volkov', source: 'sig_device_login', confidence: 0.95, discovered_at: '2026-03-22T12:00:00Z' },
			{ key: 'disk_encrypted', value: 'Yes (FileVault)', source: 'sig_device_scan', confidence: 1.0, discovered_at: '2026-03-22T12:15:00Z' },
		  ],
		  created_at: '2026-03-22T10:00:00Z', updated_at: '2026-03-22T12:15:00Z' },

		{ asset_id: 'asset_004', asset_category: 'crypto', display_name: 'BTC Wallet — bc1q...7kf3',
		  description: 'Bitcoin wallet linked to suspicious transactions. Monitoring active.',
		  status: 'active', estimated_value: 147000, currency: 'USD', serial_number: 'bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh7kf3',
		  observables: ['custom:btc:bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh7kf3'],
		  tags: ['crypto', 'financial', 'watchlist'], thumbnail_url: null,
		  properties: [
			{ key: 'chain', value: 'Bitcoin', source: 'manual', confidence: 1.0, discovered_at: '2026-03-20T08:00:00Z' },
			{ key: 'wallet_address', value: 'bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh7kf3', source: 'manual', confidence: 1.0, discovered_at: '2026-03-20T08:00:00Z' },
			{ key: 'balance_btc', value: '1.847', source: 'sig_chain_query', confidence: 1.0, discovered_at: '2026-03-22T06:00:00Z' },
			{ key: 'total_received', value: '14.293 BTC', source: 'sig_chain_query', confidence: 1.0, discovered_at: '2026-03-22T06:00:00Z' },
			{ key: 'tx_count', value: '47', source: 'sig_chain_query', confidence: 1.0, discovered_at: '2026-03-22T06:00:00Z' },
			{ key: 'first_seen', value: '2025-11-03', source: 'sig_chain_query', confidence: 1.0, discovered_at: '2026-03-20T08:15:00Z' },
		  ],
		  created_at: '2026-03-20T08:00:00Z', updated_at: '2026-03-22T06:00:00Z' },

		{ asset_id: 'asset_005', asset_category: 'jewelry', display_name: 'Rolex Submariner — Ref. 126610LN',
		  description: 'Luxury watch reported in insurance claim. Serial confirmed via pawnshop database.',
		  status: 'missing', estimated_value: 14500, currency: 'USD', serial_number: 'RSM-82947361',
		  observables: ['custom:rolex_serial:RSM-82947361'],
		  tags: ['jewelry', 'insurance', 'missing'], thumbnail_url: null,
		  properties: [
			{ key: 'brand', value: 'Rolex', source: 'manual', confidence: 1.0, discovered_at: '2026-03-15T09:00:00Z' },
			{ key: 'model', value: 'Submariner Date', source: 'manual', confidence: 1.0, discovered_at: '2026-03-15T09:00:00Z' },
			{ key: 'reference', value: '126610LN', source: 'manual', confidence: 1.0, discovered_at: '2026-03-15T09:00:00Z' },
			{ key: 'serial', value: 'RSM-82947361', source: 'sig_doc_insurance', confidence: 1.0, discovered_at: '2026-03-15T09:30:00Z' },
			{ key: 'dial_color', value: 'Black', source: 'manual', confidence: 1.0, discovered_at: '2026-03-15T09:00:00Z' },
			{ key: 'purchase_date', value: '2024-06-12', source: 'sig_doc_receipt', confidence: 1.0, discovered_at: '2026-03-15T10:00:00Z' },
			{ key: 'last_known_location', value: 'Arlington VA residence', source: 'manual', confidence: 0.7, discovered_at: '2026-03-14T22:00:00Z' },
		  ],
		  created_at: '2026-03-15T09:00:00Z', updated_at: '2026-03-15T10:00:00Z' },
	], count: 5 }),

	asset: (id: string) => delay({
		...(id === 'asset_001' ? {
			asset_id: 'asset_001', asset_category: 'vehicle', display_name: '2019 BMW X5 — ABC-1234',
			description: 'Black BMW X5, suspected in stolen vehicle case. Plate confirmed via ALPR.',
			status: 'active', estimated_value: 62000, currency: 'USD', serial_number: '1HGBH41JXMN109186',
			observables: ['license_plate:ABC-1234', 'custom:vin:1HGBH41JXMN109186'],
			tags: ['vehicle', 'case-47'], thumbnail_url: null,
			properties: [
				{ key: 'license_plate', value: 'ABC-1234', source: 'sig_alpr_001', confidence: 0.98, discovered_at: '2026-03-22T14:00:00Z' },
				{ key: 'make', value: 'BMW', source: 'sig_doc_registration', confidence: 1.0, discovered_at: '2026-03-22T15:00:00Z' },
				{ key: 'model', value: 'X5', source: 'sig_doc_registration', confidence: 1.0, discovered_at: '2026-03-22T15:00:00Z' },
				{ key: 'year', value: '2019', source: 'sig_doc_registration', confidence: 1.0, discovered_at: '2026-03-22T15:00:00Z' },
				{ key: 'color', value: 'Black', source: 'sig_img_003', confidence: 0.85, discovered_at: '2026-03-23T09:00:00Z' },
				{ key: 'vin', value: '1HGBH41JXMN109186', source: 'sig_doc_registration', confidence: 1.0, discovered_at: '2026-03-22T15:00:00Z' },
				{ key: 'registered_owner', value: 'Sarah Chen', source: 'sig_doc_registration', confidence: 1.0, discovered_at: '2026-03-22T15:00:00Z' },
			],
			created_at: '2026-03-10T09:00:00Z', updated_at: '2026-03-23T09:00:00Z',
		} : {
			asset_id: id, asset_category: 'other', display_name: 'Unknown Asset',
			status: 'active', estimated_value: null, properties: [], observables: [], tags: [],
			created_at: '2026-03-01T00:00:00Z', updated_at: '2026-03-01T00:00:00Z',
		}),
	}),

	assetDocuments: (_id: string) => delay({ documents: [
		{ doc_id: 'adoc_001', title: 'Vehicle Registration', doc_type: 'registration',
		  content: 'Virginia DMV registration document. Owner: Sarah Chen. Plate: ABC-1234. Exp: 2027-03.',
		  created_by: 'Sam', created_at: '2026-03-10T09:30:00Z' },
		{ doc_id: 'adoc_002', title: 'ALPR Capture — 5th & Main', doc_type: 'photo',
		  file_url: '/uploads/assets/asset_001/alpr_capture.jpg', mime_type: 'image/jpeg', file_size: 1847000,
		  created_by: 'System', created_at: '2026-03-22T14:00:00Z' },
		{ doc_id: 'adoc_003', title: 'Insurance Policy Summary', doc_type: 'insurance',
		  content: 'State Farm policy #SF-2847361. Comprehensive coverage. Deductible: $500. Insured value: $58,000.',
		  created_by: 'Sam', created_at: '2026-03-12T11:00:00Z' },
	]}),

	assetContacts: (_id: string) => delay({ contacts: [
		{ contact_id: 'contact_001', primary_name: 'Sarah Chen', contact_type: 'person',
		  relationship: 'registered_to', linked_at: '2026-03-10T09:00:00Z' },
		{ contact_id: 'contact_004', primary_name: 'TechCorp Industries', contact_type: 'company',
		  relationship: 'insurer', linked_at: '2026-03-12T11:00:00Z' },
	]}),

	assetCases: (_id: string) => delay({ cases: [
		{ case_id: 'case_47', title: 'Stolen Vehicle Investigation', status: 'investigating', priority: 'high',
		  relationship: 'subject', linked_at: '2026-03-10T09:00:00Z' },
	]}),

	caseContacts: (_id: string) => delay({ contacts: [
		{ contact_id: 'contact_002', primary_name: 'Marcus Rivera', contact_type: 'person',
		  relationship: 'witness', linked_at: '2026-03-12T09:00:00Z',
		  metadata: { latitude: 38.8985, longitude: -77.0370 } },
		{ contact_id: 'contact_001', primary_name: 'Sarah Chen', contact_type: 'person',
		  relationship: 'subject', linked_at: '2026-03-10T09:30:00Z',
		  metadata: { latitude: 38.8900, longitude: -77.0300 } },
	]}),

	caseAssets: (_id: string) => delay({ assets: [
		{ asset_id: 'asset_001', display_name: '2019 BMW X5 — ABC-1234', asset_category: 'vehicle',
		  status: 'active', estimated_value: 62000, relationship: 'subject', linked_at: '2026-03-10T09:00:00Z',
		  latitude: 38.8995, longitude: -77.0365 },
	]}),

	caseDocuments: (_id: string) => delay({ documents: [
		{ doc_id: 'cdoc_001', title: 'Initial Incident Report', doc_type: 'report', classification: 'unclassified',
		  content: '## Incident Report — Case #47\n\n**Date:** 2026-03-10\n**Reporting Officer:** Det. Rodriguez\n\nVehicle reported stolen from parking garage at 1847 Industrial Blvd. Owner (Sarah Chen) last saw vehicle at 18:00 on 2026-03-09. Garage CCTV reviewed — footage shows unknown male entering vehicle at 02:47.\n\n### Evidence Collected\n- Garage CCTV footage (3 clips)\n- ALPR hit at 5th & Main (14:00, 2026-03-10)\n- Witness statement from parking attendant',
		  tags: ['initial', 'report'], created_by: 'Det. Rodriguez', created_at: '2026-03-10T10:00:00Z' },
		{ doc_id: 'cdoc_002', title: 'ALPR Capture — 5th & Main', doc_type: 'photo', classification: 'unclassified',
		  file_url: '/uploads/cases/case_47/alpr_5th_main.jpg', mime_type: 'image/jpeg', file_size: 2847000,
		  tags: ['alpr', 'evidence'], created_by: 'System', created_at: '2026-03-10T14:00:00Z' },
		{ doc_id: 'cdoc_003', title: 'Witness Statement — Parking Attendant', doc_type: 'transcript', classification: 'sensitive',
		  content: 'Statement taken from James Porter, night attendant at Industrial Blvd garage.\n\n"I saw a guy in a dark hoodie hanging around the 3rd floor around 2:30 AM. Didn\'t think much of it. About 15 minutes later I heard an engine start and a black SUV drove out fast. No ticket scanned at the exit gate."',
		  tags: ['witness', 'statement'], created_by: 'Det. Rodriguez', created_at: '2026-03-11T09:00:00Z' },
		{ doc_id: 'cdoc_004', title: 'Analyst Notes — Vehicle Movement Pattern', doc_type: 'summary', classification: 'unclassified',
		  content: 'After correlating ALPR hits and traffic camera data:\n\n1. Vehicle exited garage → headed south on Industrial Blvd\n2. ALPR hit at 5th & Main (14:00)\n3. No further ALPR hits in 48h scan radius\n4. Possible route toward I-66 westbound\n\nRecommendation: Expand ALPR search to Fairfax County network.',
		  tags: ['analysis', 'movement'], created_by: 'Sam', created_at: '2026-03-12T15:00:00Z' },
		{ doc_id: 'cdoc_005', title: 'Search Warrant — Vehicle GPS Data', doc_type: 'warrant', classification: 'restricted',
		  content: 'Warrant #VA-2026-0847 granted by Judge Martinez.\nScope: GPS and telematics data from BMW ConnectedDrive for VIN 1HGBH41JXMN109186.\nValid: 2026-03-13 through 2026-04-13.',
		  tags: ['legal', 'warrant'], created_by: 'Det. Rodriguez', created_at: '2026-03-13T11:00:00Z' },
	]}),
};

// ─── API Client ──────────────────────────────────────────────────────────────

// Always use /api — Caddy proxies to backend in prod, Vite proxies in dev
const BACKEND_URL = import.meta.env?.PUBLIC_API_URL || '/api';

// Auth context is shared with client.ts via ./auth.ts so both layers see the
// same token, tenant id, and identity id.
import { setAuthContext as _setAuthContextShared, getAuthHeaders as _getAuthHeaders } from './auth';

export const setAuthContext = _setAuthContextShared;

export function getHeaders(): Record<string, string> {
	return { 'Content-Type': 'application/json', ..._getAuthHeaders() };
}

/**
 * Fetch from the real backend. Falls back to mock data in development
 * if the backend is unreachable. In production, throws on failure.
 */
async function fetchOrMock<T>(path: string, mockFn: () => Promise<T>, options?: RequestInit): Promise<T> {
	try {
		const res = await fetch(`${BACKEND_URL}${path}`, {
			...options,
			headers: { ...getHeaders(), ...options?.headers as any },
		});
		if (res.ok) return await res.json();
		// In dev, fall back to mock data for realistic previews
		if (import.meta.env?.DEV) return mockFn();
		// In production, return empty/default data instead of crashing
		// This handles fresh installs and missing endpoints gracefully
		try { return await res.json(); } catch { return {} as T; }
	} catch (err) {
		if (import.meta.env?.DEV) return mockFn();
		return {} as T;
	}
}

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
	const res = await fetch(`${BACKEND_URL}${path}`, {
		...options,
		headers: { ...getHeaders(), ...options?.headers as any },
	});
	return await res.json();
}

export const api = {
	// ─── Read ────────────────────────────────────────────────────────────
	dashboard: () => fetchOrMock('/dashboard/summary', mock.dashboard),
	alerts: () => fetchOrMock('/alerts', mock.alerts),
	cases: () => fetchOrMock('/cases', mock.cases),
	signals: () => fetchOrMock('/signals', mock.signals),
	signal: (id: string) => apiFetch(`/signals/${id}`),
	lduPEntries: () => fetchOrMock('/signals/ldu-p', mock.lduPEntries),
	lduPEntry: (id: string) => apiFetch(`/signals/ldu-p/${id}`),
	promoteLduP: (
		id: string,
		signalType: string,
		opts: {
			resolvedBy?: string;
			writeMapping?: boolean;
			mappingOverwrite?: boolean;
			mappingNodeType?: string;
			mappingSubtype?: string;
		} = {},
	) =>
		apiFetch(`/signals/ldu-p/${id}/promote`, {
			method: 'POST',
			body: JSON.stringify({
				signal_type: signalType,
				resolved_by: opts.resolvedBy ?? 'operator',
				write_mapping: opts.writeMapping ?? false,
				mapping_overwrite: opts.mappingOverwrite ?? false,
				mapping_node_type: opts.mappingNodeType ?? 'event',
				mapping_subtype: opts.mappingSubtype,
			}),
		}),
	rejectLduP: (id: string, reason = '', resolvedBy = 'operator') =>
		apiFetch(`/signals/ldu-p/${id}/reject`, { method: 'PATCH', body: JSON.stringify({ reason, resolved_by: resolvedBy }) }),
	editLduP: (
		id: string,
		body: {
			proposed_signal_type?: string;
			extracted_structure?: any;
			proposed_mapping?: any;
		},
	) => apiFetch(`/signals/ldu-p/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
	gallery: () => fetchOrMock('/gallery', mock.gallery),
	organized: () => fetchOrMock('/organize/domains', mock.organized),
	projects: () => fetchOrMock('/projects', mock.projects),
	profile: () => fetchOrMock('/profile', mock.profile),
	aiProviders: () => fetchOrMock('/ai/providers', mock.aiProviders),
	aiConfig: () => fetchOrMock('/ai/config', async () => ({ configured: false, provider: null, model: null })),
	rules: () => fetchOrMock('/detection/rules', async () => ({ rules: [
		{ id: 'rule_social_keyword', name: 'Social Media Keyword Watch', description: 'Alert when any social post contains keywords of interest',
		  status: 'active', severity: 'medium', category: 'social_monitoring', rule_type: 'match',
		  conditions: [
			{ field: 'signal_type', operator: 'equals', value: 'signal.content.social_post' },
			{ field: 'payload.body', operator: 'regex', value: '(classified|confidential|leak|breach)' },
		  ], trigger_count: 12, last_triggered: '2026-03-22T14:30:00Z', created_at: '2026-03-01T09:00:00Z' },
		{ id: 'rule_social_poi', name: 'POI Social Activity', description: 'Alert when a watched person posts on any monitored platform',
		  status: 'active', severity: 'high', category: 'social_monitoring', rule_type: 'observable_match',
		  conditions: [
			{ field: 'signal_type', operator: 'equals', value: 'signal.content.social_post' },
			{ field: 'observables.username', operator: 'contains', value: 'alexei_v' },
		  ], trigger_count: 3, last_triggered: '2026-03-21T18:00:00Z', created_at: '2026-03-22T08:30:00Z' },
		{ id: 'rule_social_geofence', name: 'Geotagged Post Near HQ', description: 'Alert when a social post is geotagged within 5km of monitored locations',
		  status: 'active', severity: 'low', category: 'geofence', rule_type: 'geofence',
		  conditions: [
			{ field: 'signal_type', operator: 'equals', value: 'signal.content.social_post' },
			{ field: 'latitude', operator: 'gt', value: '38.87' },
			{ field: 'latitude', operator: 'lt', value: '38.90' },
		  ], trigger_count: 7, last_triggered: '2026-03-22T12:00:00Z', created_at: '2026-03-05T10:00:00Z' },
		{ id: 'rule_social_hashtag', name: 'Hashtag Monitor', description: 'Alert on specific hashtags appearing in social feeds',
		  status: 'active', severity: 'medium', category: 'social_monitoring', rule_type: 'match',
		  conditions: [
			{ field: 'signal_type', operator: 'equals', value: 'signal.content.social_post' },
			{ field: 'observables.hashtag', operator: 'regex', value: '(protest|rally|demonstration|shutdown)' },
		  ], trigger_count: 22, last_triggered: '2026-03-22T16:00:00Z', created_at: '2026-03-02T09:00:00Z' },
		{ id: 'rule_social_volume', name: 'Social Volume Spike', description: 'Alert when social post ingestion rate exceeds threshold',
		  status: 'disabled', severity: 'low', category: 'threshold', rule_type: 'threshold',
		  conditions: [
			{ field: 'signal_type', operator: 'equals', value: 'signal.content.social_post' },
			{ field: '_count_1h', operator: 'gt', value: '50' },
		  ], trigger_count: 0, last_triggered: null, created_at: '2026-03-10T09:00:00Z' },
		{ id: 'rule_auth_failure', name: 'Brute Force Detection', description: 'Alert on 5+ auth failures from same IP in 3 minutes',
		  status: 'active', severity: 'high', category: 'credential_access', rule_type: 'threshold',
		  conditions: [
			{ field: 'signal_type', operator: 'equals', value: 'signal.security.authentication_failure' },
			{ field: '_count_3m', operator: 'gt', value: '5' },
		  ], trigger_count: 8, last_triggered: '2026-03-22T14:32:00Z', created_at: '2026-02-15T09:00:00Z' },
		{ id: 'rule_plate_watch', name: 'Watched License Plate', description: 'Alert when a watched plate is captured by ALPR',
		  status: 'active', severity: 'high', category: 'observable_match', rule_type: 'observable_match',
		  conditions: [
			{ field: 'observables.license_plate', operator: 'equals', value: 'ABC-1234' },
		  ], trigger_count: 2, last_triggered: '2026-03-22T14:32:07Z', created_at: '2026-03-10T09:00:00Z' },
	], count: 7 })),

	// ─── Geo / Map ──────────────────────────────────────────────────────
	geoSignals: (params?: any) => {
		const qs = params ? '?' + new URLSearchParams(
			Object.fromEntries(Object.entries(params).filter(([_, v]) => v != null && v !== '').map(([k, v]) => [k, String(v)]))
		).toString() : '';
		return fetchOrMock(`/signals/geo${qs}`, mock.geoSignals);
	},
	tracks: () => fetchOrMock('/tracks', mock.tracks),
	geofences: () => fetchOrMock('/geofences', mock.geofences),
	createGeofence: (body: any) =>
		apiFetch('/geofences', { method: 'POST', body: JSON.stringify(body) }),
	analyzeDevice: (deviceId: string, params?: any) => {
		const qs = params ? '?' + new URLSearchParams(
			Object.fromEntries(Object.entries(params).filter(([_, v]) => v != null && v !== '').map(([k, v]) => [k, String(v)]))
		).toString() : '';
		return apiFetch(`/tracks/${encodeURIComponent(deviceId)}/analyze${qs}`);
	},
	geoNodes: (params?: any) => {
		const qs = params ? '?' + new URLSearchParams(
			Object.fromEntries(Object.entries(params).filter(([_, v]) => v != null && v !== '').map(([k, v]) => [k, String(v)]))
		).toString() : '';
		return fetchOrMock(`/knowledge/geo${qs}`, mock.geoNodes);
	},
	createGeoNode: (body: any) =>
		apiFetch('/knowledge/geo', { method: 'POST', body: JSON.stringify(body) }),
	addNodeImage: (nodeId: string, body: any) =>
		apiFetch(`/knowledge/geo/${nodeId}/images`, { method: 'POST', body: JSON.stringify(body) }),
	updateGeoNode: (nodeId: string, body: any) =>
		apiFetch(`/knowledge/geo/${nodeId}`, { method: 'PATCH', body: JSON.stringify(body) }),

	// ─── Annotations / Drawing / Import ─────────────────────────────────
	annotations: (params?: any) => {
		const qs = params ? '?' + new URLSearchParams(
			Object.fromEntries(Object.entries(params).filter(([_, v]) => v != null && v !== '').map(([k, v]) => [k, String(v)]))
		).toString() : '';
		return fetchOrMock(`/map/annotations${qs}`, mock.annotations);
	},
	createAnnotation: (body: any) =>
		apiFetch('/map/annotations', { method: 'POST', body: JSON.stringify(body) }),
	deleteAnnotation: (id: string) =>
		apiFetch(`/map/annotations/${id}`, { method: 'DELETE' }),
	importGeoFile: (body: any) =>
		apiFetch('/map/import', { method: 'POST', body: JSON.stringify(body) }),

	// ─── Alert Actions ──────────────────────────────────────────────────
	updateAlert: (alertId: string, body: { status: string; suppression_reason?: string; assigned_to?: string }) =>
		apiFetch(`/alerts/${alertId}`, { method: 'PATCH', body: JSON.stringify(body) }),

	escalateAlert: (alertId: string) =>
		apiFetch(`/alerts/${alertId}/escalate`, { method: 'POST' }),

	// ─── Case Actions ───────────────────────────────────────────────────
	createCase: (body: { title: string; priority?: string; description?: string; tags?: string[] }) =>
		apiFetch('/cases', { method: 'POST', body: JSON.stringify(body) }),

	getCase: (caseId: string) => fetchOrMock(`/cases/${caseId}`, async () => {
		const allCases: Record<string, any> = {
			'case_47': { case_id: 'case_47', title: 'Stolen Vehicle Investigation', status: 'investigating', priority: 'high',
				project_id: 'proj_sec_audit', owner: 'Det. Rodriguez', description: 'Black BMW X5 (plate ABC-1234) reported stolen from parking garage.',
				tags: ['vehicle', 'theft'], created_at: '2026-03-10T09:00:00Z', updated_at: '2026-03-22T14:35:00Z' },
			'case_51': { case_id: 'case_51', title: 'Q3 Security Audit Review', status: 'open', priority: 'medium',
				project_id: 'proj_sec_audit', owner: 'Marcus', description: 'Quarterly security audit review and compliance check.',
				tags: ['security', 'audit'], created_at: '2026-03-18T10:00:00Z', updated_at: '2026-03-22T11:00:00Z' },
			'case_63': { case_id: 'case_63', title: 'Network Intrusion Analysis', status: 'open', priority: 'high',
				project_id: 'proj_sec_audit', owner: null, description: 'Investigating unauthorized network access from multiple IPs.',
				tags: ['security', 'network'], created_at: '2026-03-22T08:00:00Z', updated_at: '2026-03-22T08:00:00Z' },
			'case_38': { case_id: 'case_38', title: 'Phishing Campaign Tracking', status: 'resolved', priority: 'low',
				project_id: 'proj_sec_audit', owner: 'Sarah', description: 'Tracking and analyzing phishing emails targeting org.',
				tags: ['phishing'], created_at: '2026-03-01T09:00:00Z', updated_at: '2026-03-15T16:00:00Z' },
			'case_safety1': { case_id: 'case_safety1', title: 'School Zone Monitoring', status: 'open', priority: 'medium',
				project_id: 'proj_family', owner: 'Sam', description: 'Monitoring family member locations near school zones.',
				tags: ['family', 'geofence'], created_at: '2026-02-01T09:00:00Z', updated_at: '2026-03-20T10:00:00Z' },
		};
		return allCases[caseId] || { case_id: caseId, title: 'Unknown Case', status: 'open', priority: 'P3', project_id: null };
	}),

	updateCaseStatus: (caseId: string, status: string) =>
		apiFetch(`/cases/${caseId}`, { method: 'PATCH', body: JSON.stringify({ status }) }),
	updateCase: (caseId: string, body: any) =>
		apiFetch(`/cases/${caseId}`, { method: 'PATCH', body: JSON.stringify(body) }),
	deleteCase: (caseId: string) =>
		apiFetch(`/cases/${caseId}`, { method: 'DELETE' }),

	caseTimeline: (caseId: string) => fetchOrMock(`/cases/${caseId}/timeline`, async () => ({ events: [
		{ event_type: 'case_created', actor_id: 'Det. Rodriguez', commit_time: '2026-03-10T09:00:00Z',
		  new_value: 'Case opened — stolen vehicle report filed by Sarah Chen' },
		{ event_type: 'observable_added', actor_id: 'Det. Rodriguez', commit_time: '2026-03-10T09:30:00Z',
		  new_value: 'Added plate ABC-1234 to watchlist', title: 'Plate Watch', signal_id: 'sig_a3f7_005' },
		{ event_type: 'alert_linked', actor_id: 'System', commit_time: '2026-03-10T14:00:05Z',
		  new_value: 'ALPR hit: ABC-1234 at 5th & Main', title: 'ALPR Hit',
		  latitude: 38.8995, longitude: -77.0365, signal_id: 'sig_a3f7_005' },
		{ event_type: 'document_added', actor_id: 'Det. Rodriguez', commit_time: '2026-03-10T10:00:00Z',
		  new_value: 'Filed initial incident report' },
		{ event_type: 'witness_statement', actor_id: 'Det. Rodriguez', commit_time: '2026-03-11T09:00:00Z',
		  new_value: 'Statement from James Porter (night attendant) — saw suspect at 02:30' },
		{ event_type: 'contact_linked', actor_id: 'Det. Rodriguez', commit_time: '2026-03-12T09:00:00Z',
		  new_value: 'Linked Marcus Rivera as witness' },
		{ event_type: 'analysis_note', actor_id: 'Sam', commit_time: '2026-03-12T15:00:00Z',
		  new_value: 'Vehicle movement analysis — possible route toward I-66 westbound' },
		{ event_type: 'warrant_issued', actor_id: 'Det. Rodriguez', commit_time: '2026-03-13T11:00:00Z',
		  new_value: 'Search warrant #VA-2026-0847 granted for BMW ConnectedDrive GPS data' },
		{ event_type: 'status_change', actor_id: 'Det. Rodriguez', commit_time: '2026-03-22T14:35:00Z',
		  new_value: 'Status changed to investigating' },
	] })),

	addObservable: (caseId: string, body: { observable_type: string; observable_value: string }) =>
		apiFetch(`/cases/${caseId}/observables`, { method: 'POST', body: JSON.stringify(body) }),

	// ─── Detection Rules ────────────────────────────────────────────────
	createRule: (body: { name: string; rule_type: string; severity: string; condition: any }) =>
		apiFetch('/detection/rules', { method: 'POST', body: JSON.stringify(body) }),

	// ─── Rule Presets (Alert Library) ───────────────────────────────────
	presets: (category?: string) => {
		const qs = category ? `?category=${encodeURIComponent(category)}` : '';
		return fetchOrMock(`/detection/presets${qs}`, mock.presets);
	},
	presetCategories: () => fetchOrMock('/detection/presets/categories', async () => ({ categories: [] })),
	financialSummary: (days = 30) => fetchOrMock(`/financial/summary?days=${days}`, mock.financialSummary),
	financialAccounts: () => fetchOrMock('/financial/accounts', mock.financialAccounts),
	netWorth: () => fetchOrMock('/financial/net-worth', mock.netWorth),
	financialRecurring: () => fetchOrMock('/financial/recurring', mock.financialRecurring),
	detectRecurring: () => apiFetch('/financial/recurring/detect', { method: 'POST' }),
	createFinancialAccount: (body: any) => apiFetch('/financial/accounts', { method: 'POST', body: JSON.stringify(body) }),
	updateFinancialAccount: (id: string, body: any) => apiFetch(`/financial/accounts/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
	activatePreset: (presetId: string) =>
		apiFetch(`/detection/presets/${presetId}/activate`, { method: 'POST' }),
	deactivatePreset: (presetId: string) =>
		apiFetch(`/detection/presets/${presetId}/deactivate`, { method: 'POST' }),

	// ─── Observable Search ──────────────────────────────────────────────
	searchObservables: (type: string, value: string) =>
		apiFetch(`/search/observables?observable_type=${encodeURIComponent(type)}&value=${encodeURIComponent(value)}`),

	// ─── Knowledge Graph ────────────────────────────────────────────────
	getProject: (projectId: string) => fetchOrMock(`/projects/${projectId}`, async () => {
		const projects: Record<string, any> = {
			'proj_sec_audit': { project_id: 'proj_sec_audit', name: 'Security Audit Q3', status: 'active',
				description: 'Comprehensive Q3 security review covering vehicle theft, network intrusions, phishing campaigns, and compliance.',
				node_count: 23, created_at: '2026-03-01T09:00:00Z' },
			'proj_architecture': { project_id: 'proj_architecture', name: 'Architecture Research', status: 'active',
				description: 'Archaeological site analysis — Gobekli Tepe, Stonehenge, Machu Picchu, and megalithic alignments.',
				node_count: 47, created_at: '2026-02-15T10:00:00Z' },
			'proj_family': { project_id: 'proj_family', name: 'Family Safety', status: 'active',
				description: 'Family member location monitoring, school zone geofencing, and personal safety alerts.',
				node_count: 8, created_at: '2026-01-20T08:00:00Z' },
		};
		return projects[projectId] || { project_id: projectId, name: 'Unknown Project', status: 'active', node_count: 0, created_at: '2026-01-01T00:00:00Z' };
	}),
	projectGraph: (projectId: string) => apiFetch(`/projects/${projectId}/graph`),
	createNode: (body: { project_id: string; title: string; description?: string; epistemic_type: string }) =>
		apiFetch('/knowledge/nodes', { method: 'POST', body: JSON.stringify(body) }),

	// ─── Ingestion ──────────────────────────────────────────────────────
	ingest: (rawBytes: string, deviceId: string) =>
		fetch(`${BACKEND_URL}/signals/ingest`, {
			method: 'POST',
			headers: { ...HEADERS, 'X-Device-ID': deviceId, 'Content-Type': 'application/octet-stream' },
			body: rawBytes,
		}).then(r => r.json()),

	// ─── Device Pairing ─────────────────────────────────────────────────
	generatePairing: (body: { display_name: string; device_type?: string; gps_interval_s?: number; upload_photos?: boolean }) =>
		apiFetch('/devices/pair/generate', { method: 'POST', body: JSON.stringify(body) }),
	listDevices: () => apiFetch('/devices'),
	getDevice: (deviceId: string) => apiFetch(`/devices/${deviceId}`),
	updateDevice: (deviceId: string, config: any) =>
		apiFetch(`/devices/${deviceId}`, { method: 'PATCH', body: JSON.stringify(config) }),
	revokeDevice: (deviceId: string) =>
		apiFetch(`/devices/${deviceId}`, { method: 'DELETE' }),
	sendCommand: (deviceId: string, type: string, params?: any) =>
		apiFetch(`/devices/${deviceId}/commands`, { method: 'POST', body: JSON.stringify({ type, params: params || {} }) }),
	deviceCommands: (deviceId: string) => apiFetch(`/devices/${deviceId}/commands`),

	// ─── AI Chat & Config ───────────────────────────────────────────────
	chat: (message: string) =>
		apiFetch('/ai/chat', { method: 'POST', body: JSON.stringify({ message }) }),
	connectWizard: (message: string, conversationHistory?: any[]) =>
		apiFetch('/ai/connect-wizard', {
			method: 'POST',
			body: JSON.stringify({ message, conversation_history: conversationHistory }),
		}),
	saveAiConfig: (body: { provider: string; model?: string; api_key?: string }) =>
		apiFetch('/ai/config', { method: 'POST', body: JSON.stringify(body) }),
	testAiConnection: () =>
		apiFetch('/ai/config/test', { method: 'POST' }),

	// ─── Card Templates ─────────────────────────────────────────────────
	templates: () => fetchOrMock('/card-templates', async () => ({ templates: [] })),
	updateTemplate: (signalType: string, body: any) =>
		apiFetch(`/card-templates/${encodeURIComponent(signalType)}`, { method: 'PATCH', body: JSON.stringify(body) }),
	regenerateTemplate: (signalType: string) =>
		apiFetch(`/card-templates/${encodeURIComponent(signalType)}/regenerate`, { method: 'POST' }),

	// ─── Workers & Processing Queue ─────────────────────────────────────
	workers: () => fetchOrMock('/workers', async () => ({ workers: [] })),
	workerAction: (workerId: string, action: string) =>
		apiFetch(`/workers/${workerId}/${action}`, { method: 'POST' }),
	workerTick: (workerId: string) =>
		apiFetch(`/workers/${workerId}/tick`, { method: 'POST' }),
	processingQueue: (params?: any) => {
		const qs = params ? '?' + new URLSearchParams(params).toString() : '';
		return fetchOrMock(`/processing/queue${qs}`, async () => ({ items: [], count: 0 }));
	},
	processingSummary: () => fetchOrMock('/processing/queue/summary', async () => ({})),
	validateChunk: (body: any) =>
		apiFetch('/processing/validate', { method: 'POST', body: JSON.stringify(body) }),

	// ─── Connectors ─────────────────────────────────────────────────────
	connectors: () => fetchOrMock('/connectors', async () => ({ connectors: [] })),
	createConnector: (body: any) =>
		apiFetch('/connectors', { method: 'POST', body: JSON.stringify(body) }),
	triggerConnector: (connectorId: string) =>
		apiFetch(`/connectors/${connectorId}/fetch`, { method: 'POST' }),

	// ─── Connector Recipes ──────────────────────────────────────────────
	// ─── Device Feed ────────────────────────────────────────────────────
	deviceFeed: () => fetchOrMock('/devices/feed', async () => ({ devices: [] })),
	typeHistory: (deviceId: string, signalType: string, limit = 20) =>
		apiFetch(`/devices/feed/${encodeURIComponent(deviceId)}/${encodeURIComponent(signalType)}?limit=${limit}`),

	listRecipes: () => apiFetch('/recipes'),
	getRecipe: (recipeId: string) => apiFetch(`/recipes/${recipeId}`),
	enableRecipe: (recipeId: string, params: any) =>
		apiFetch(`/recipes/${recipeId}/enable`, { method: 'POST', body: JSON.stringify({ params }) }),

	// ─── Live Cache (hot data for browse/map) ───────────────────────────
	liveQuery: (source: string, params?: { lat?: number; lon?: number; radius_km?: number; limit?: number }) => {
		const qs = params ? '?' + Object.entries(params).filter(([_, v]) => v != null).map(([k, v]) => `${k}=${v}`).join('&') : '';
		return fetchOrMock(`/live/${source}${qs}`, async () => ({ source, count: 0, items: [] }));
	},
	liveGeo: (source: string, params?: { lat?: number; lon?: number; radius_km?: number; limit?: number }) => {
		const qs = params ? '?' + Object.entries(params).filter(([_, v]) => v != null).map(([k, v]) => `${k}=${v}`).join('&') : '';
		return fetchOrMock(`/live/${source}/geo${qs}`, async () => ({ type: 'FeatureCollection', features: [] }));
	},
	liveViewport: (source: string, bounds: { north: number; south: number; east: number; west: number }) =>
		fetchOrMock(`/live/${source}/viewport?north=${bounds.north}&south=${bounds.south}&east=${bounds.east}&west=${bounds.west}`,
			async () => ({ source, count: 0, items: [] })),
	liveTrail: (source: string, key: string, hours: number = 4) =>
		fetchOrMock(`/live/${source}/trail/${encodeURIComponent(key)}?hours=${hours}`, async () => ({ positions: [], count: 0, track: null })),
	liveTrails: (source: string, hours: number = 1) =>
		fetchOrMock(`/live/${source}/trails?hours=${hours}`, async () => ({ entities: [], count: 0 })),

	// ─── Case Observables (watchlist) ───────────────────────────────────
	addCaseObservable: (caseId: string, data: { observable_type: string; observable_value: string; notes?: string }) =>
		apiFetch(`/cases/${caseId}/observables`, { method: 'POST', body: JSON.stringify(data) }),
	caseObservables: (caseId: string) =>
		fetchOrMock(`/cases/${caseId}/observables`, async () => ({ observables: [
			{ observable_type: 'license_plate', observable_value: 'ABC-1234', added_by: 'manual', added_at: '2026-03-10T09:30:00Z' },
			{ observable_type: 'ip_address', observable_value: '127.0.0.1', added_by: 'entity_resolver', added_at: '2026-03-10T14:35:00Z' },
			{ observable_type: 'email_address', observable_value: 'sarah@example.com', added_by: 'manual', added_at: '2026-03-10T09:30:00Z' },
			{ observable_type: 'name', observable_value: 'James Porter', added_by: 'graph_builder', added_at: '2026-03-12T09:00:00Z' },
			{ observable_type: 'hostname', observable_value: 'fw-edge-01', added_by: 'entity_resolver', added_at: '2026-03-22T14:32:00Z' },
		] })),
	caseSignals: (caseId: string) =>
		fetchOrMock(`/cases/${caseId}/signals`, async () => ({ signals: [] })),
	caseContacts: (caseId: string) =>
		fetchOrMock(`/cases/${caseId}/contacts`, () => mock.caseContacts(caseId)),
	linkCaseContact: (caseId: string, body: { contact_id: string; relationship: string }) =>
		apiFetch(`/cases/${caseId}/contacts`, { method: 'POST', body: JSON.stringify(body) }),
	caseAssets: (caseId: string) =>
		fetchOrMock(`/cases/${caseId}/assets`, () => mock.caseAssets(caseId)),
	linkCaseAsset: (caseId: string, body: { asset_id: string; relationship: string }) =>
		apiFetch(`/cases/${caseId}/assets`, { method: 'POST', body: JSON.stringify(body) }),
	caseDocuments: (caseId: string) =>
		fetchOrMock(`/cases/${caseId}/documents`, () => mock.caseDocuments(caseId)),
	addCaseDocument: (caseId: string, body: any) =>
		apiFetch(`/cases/${caseId}/documents`, { method: 'POST', body: JSON.stringify(body) }),
	deleteCaseDocument: (caseId: string, docId: string) =>
		apiFetch(`/cases/${caseId}/documents/${docId}`, { method: 'DELETE' }),

	// ─── Knowledge Nodes ────────────────────────────────────────────────
	getNode: (nodeId: string) => apiFetch(`/knowledge/nodes/${nodeId}`),
	nodeAttachments: (nodeId: string) =>
		fetchOrMock(`/knowledge/nodes/${nodeId}/attachments`, async () => ({ attachments: [] })),
	addNodeAttachment: (nodeId: string, data: any) =>
		apiFetch(`/knowledge/nodes/${nodeId}/attachments`, { method: 'POST', body: JSON.stringify(data) }),
	nodeCases: (nodeId: string) =>
		fetchOrMock(`/knowledge/nodes/${nodeId}/cases`, async () => ({ cases: [] })),
	linkNodeToCase: (nodeId: string, caseId: string) =>
		apiFetch(`/knowledge/nodes/${nodeId}/link-case`, { method: 'POST', body: JSON.stringify({ case_id: caseId }) }),
	nearbySignals: (nodeId: string, radiusM: number = 500) =>
		fetchOrMock(`/knowledge/nodes/${nodeId}/nearby?radius_m=${radiusM}`, async () => ({ signals: [], count: 0 })),
	caseNodes: (caseId: string) =>
		fetchOrMock(`/cases/${caseId}/nodes`, async () => ({ nodes: [
			{ node_id: 'kn_001', title: 'Sarah Chen', epistemic_type: 'person',
			  description: 'Vehicle owner, reported theft on 2026-03-10',
			  concepts: ['victim', 'vehicle-owner'], latitude: 38.8900, longitude: -77.0300 },
			{ node_id: 'kn_002', title: '2019 BMW X5', epistemic_type: 'asset',
			  description: 'Stolen vehicle — plate ABC-1234, VIN 1HGBH41JXMN109186',
			  concepts: ['vehicle', 'stolen', 'bmw'], latitude: 38.8995, longitude: -77.0365 },
			{ node_id: 'kn_003', title: 'James Porter', epistemic_type: 'person',
			  description: 'Night parking attendant — witnessed suspect at 02:30',
			  concepts: ['witness', 'attendant'] },
			{ node_id: 'kn_004', title: '1847 Industrial Blvd Garage', epistemic_type: 'location',
			  description: 'Theft location — 3rd floor parking structure',
			  concepts: ['crime-scene', 'garage'], latitude: 38.8970, longitude: -77.0385 },
			{ node_id: 'kn_005', title: '5th & Main Intersection', epistemic_type: 'location',
			  description: 'ALPR capture point — vehicle spotted 14:00 on 2026-03-10',
			  concepts: ['alpr', 'sighting'], latitude: 38.8995, longitude: -77.0365 },
		] })),
	resolveEntity: (type: string, value: string, caseId?: string) =>
		apiFetch('/knowledge/resolve', { method: 'POST', body: JSON.stringify({ type, value, case_id: caseId }) }),

	// ─── Research ───────────────────────────────────────────────────────
	researchEntity: (name: string, context?: { entity_type?: string; topic_id?: string; existing_properties?: any[] }) =>
		fetchOrMock('/entities/research/entity', () => mock.researchEntity(name, context), {
			method: 'POST', body: JSON.stringify({ name, ...context }),
		}),
	promoteEntityToContact: (topicId: string, entityId: string, researchData: any) =>
		apiFetch(`/watch-topics/${topicId}/entities/${entityId}/promote`, {
			method: 'POST', body: JSON.stringify(researchData),
		}),

	// ─── Watch Topics ───────────────────────────────────────────────────
	watchTopics: () => fetchOrMock('/watch-topics', mock.watchTopics),
	watchTopic: (id: string) => fetchOrMock(`/watch-topics/${id}`, () => mock.watchTopic(id)),
	createWatchTopic: (body: any) =>
		apiFetch('/watch-topics', { method: 'POST', body: JSON.stringify(body) }),
	updateWatchTopic: (id: string, body: any) =>
		apiFetch(`/watch-topics/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
	deleteWatchTopic: (id: string) =>
		apiFetch(`/watch-topics/${id}`, { method: 'DELETE' }),
	watchTopicClusters: (id: string) => fetchOrMock(`/watch-topics/${id}/clusters`, () => mock.watchTopicClusters(id)),
	watchTopicArticles: (id: string, clusterId?: string) => {
		const qs = clusterId ? `?cluster_id=${clusterId}` : '';
		return fetchOrMock(`/watch-topics/${id}/articles${qs}`, () => mock.watchTopicArticles(id, clusterId));
	},
	watchTopicEntities: (id: string) => fetchOrMock(`/watch-topics/${id}/entities`, () => mock.watchTopicEntities(id)),
	watchTopicTimeline: (id: string) =>
		fetchOrMock(`/watch-topics/${id}/timeline`, () => mock.watchTopicTimeline(id)),
	watchTopicGraph: (id: string, focalId?: string) => {
		const qs = focalId ? `?focal=${focalId}` : '';
		return fetchOrMock(`/watch-topics/${id}/graph${qs}`, () => mock.watchTopicGraph(id, focalId));
	},
	promoteTopicToProject: (id: string, body: any) =>
		apiFetch(`/watch-topics/${id}/promote/project`, { method: 'POST', body: JSON.stringify(body) }),
	promoteTopicToCase: (id: string, body: any) =>
		apiFetch(`/watch-topics/${id}/promote/case`, { method: 'POST', body: JSON.stringify(body) }),
	topicLinkedProjects: (id: string) =>
		fetchOrMock(`/watch-topics/${id}/projects`, async () => ({ projects: [
			{ project_id: 'proj_sec_audit', name: 'Security Audit Q3', status: 'active', auto_sync: true, linked_at: '2026-03-15T10:00:00Z' },
		]})),
	linkTopicToProject: (topicId: string, body: { project_id: string }) =>
		apiFetch(`/watch-topics/${topicId}/projects`, { method: 'POST', body: JSON.stringify(body) }),
	unlinkTopicFromProject: (topicId: string, projectId: string) =>
		apiFetch(`/watch-topics/${topicId}/projects/${projectId}`, { method: 'DELETE' }),
	toggleTopicProjectSync: (topicId: string, projectId: string, autoSync: boolean) =>
		apiFetch(`/watch-topics/${topicId}/projects/${projectId}`, { method: 'PATCH', body: JSON.stringify({ auto_sync: autoSync }) }),
	topicLinkedCases: (id: string) =>
		fetchOrMock(`/watch-topics/${id}/cases`, async () => ({ cases: [
			{ case_id: 'case_47', title: 'Stolen Vehicle Investigation', status: 'investigating', auto_sync: true, linked_at: '2026-03-10T09:00:00Z' },
			{ case_id: 'case_63', title: 'Network Intrusion Analysis', status: 'open', auto_sync: false, linked_at: '2026-03-22T08:00:00Z' },
		]})),
	linkTopicToCase: (topicId: string, body: { case_id: string }) =>
		apiFetch(`/watch-topics/${topicId}/cases`, { method: 'POST', body: JSON.stringify(body) }),
	unlinkTopicFromCase: (topicId: string, caseId: string) =>
		apiFetch(`/watch-topics/${topicId}/cases/${caseId}`, { method: 'DELETE' }),
	toggleTopicCaseSync: (topicId: string, caseId: string, autoSync: boolean) =>
		apiFetch(`/watch-topics/${topicId}/cases/${caseId}`, { method: 'PATCH', body: JSON.stringify({ auto_sync: autoSync }) }),
	// Reverse lookups: which topics feed into a project/case
	projectTopics: (projectId: string) =>
		fetchOrMock(`/projects/${projectId}/topics`, async () => ({ topics: [
			{ topic_id: 'topic_ukraine', title: 'Ukraine Conflict', status: 'active', article_count: 234, auto_sync: true },
		]})),
	caseTopics: (caseId: string) =>
		fetchOrMock(`/cases/${caseId}/topics`, async () => ({ topics: [
			{ topic_id: 'topic_ukraine', title: 'Ukraine Conflict', status: 'active', article_count: 234, auto_sync: true },
		]})),
	projectCases: (projectId: string) => {
		const allCases: Record<string, any[]> = {
			'proj_sec_audit': [
				{ case_id: 'case_47', title: 'Stolen Vehicle Investigation', status: 'investigating', priority: 'high',
				  project_id: 'proj_sec_audit', owner: 'Det. Rodriguez', tags: ['vehicle', 'theft'], created_at: '2026-03-10T09:00:00Z', updated_at: '2026-03-22T14:35:00Z' },
				{ case_id: 'case_63', title: 'Network Intrusion Analysis', status: 'open', priority: 'high',
				  project_id: 'proj_sec_audit', owner: null, tags: ['security', 'network'], created_at: '2026-03-22T08:00:00Z', updated_at: '2026-03-22T08:00:00Z' },
				{ case_id: 'case_51', title: 'Q3 Security Audit Review', status: 'open', priority: 'medium',
				  project_id: 'proj_sec_audit', owner: 'Marcus', tags: ['security', 'audit'], created_at: '2026-03-18T10:00:00Z', updated_at: '2026-03-22T11:00:00Z' },
				{ case_id: 'case_38', title: 'Phishing Campaign Tracking', status: 'resolved', priority: 'low',
				  project_id: 'proj_sec_audit', owner: 'Sarah', tags: ['phishing'], created_at: '2026-03-01T09:00:00Z', updated_at: '2026-03-15T16:00:00Z' },
			],
			'proj_family': [
				{ case_id: 'case_safety1', title: 'School Zone Monitoring', status: 'open', priority: 'medium',
				  project_id: 'proj_family', owner: 'Sam', tags: ['family', 'geofence'], created_at: '2026-02-01T09:00:00Z', updated_at: '2026-03-20T10:00:00Z' },
			],
		};
		return fetchOrMock(`/projects/${projectId}/cases`, async () => ({ cases: allCases[projectId] || [] }));
	},
	createProjectCase: (projectId: string, body: any) =>
		apiFetch(`/projects/${projectId}/cases`, { method: 'POST', body: JSON.stringify(body) }),

	// ─── Assets ─────────────────────────────────────────────────────────
	assets: () => fetchOrMock('/assets', mock.assets),
	asset: (id: string) => fetchOrMock(`/assets/${id}`, () => mock.asset(id)),
	createAsset: (body: any) =>
		apiFetch('/assets', { method: 'POST', body: JSON.stringify(body) }),
	updateAsset: (id: string, body: any) =>
		apiFetch(`/assets/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
	deleteAsset: (id: string) =>
		apiFetch(`/assets/${id}`, { method: 'DELETE' }),
	addAssetProperty: (id: string, body: { key: string; value: string }) =>
		apiFetch(`/assets/${id}/properties`, { method: 'POST', body: JSON.stringify(body) }),
	assetDocuments: (id: string) => fetchOrMock(`/assets/${id}/documents`, () => mock.assetDocuments(id)),
	addAssetDocument: (id: string, body: any) =>
		apiFetch(`/assets/${id}/documents`, { method: 'POST', body: JSON.stringify(body) }),
	deleteAssetDocument: (assetId: string, docId: string) =>
		apiFetch(`/assets/${assetId}/documents/${docId}`, { method: 'DELETE' }),
	assetContacts: (id: string) => fetchOrMock(`/assets/${id}/contacts`, () => mock.assetContacts(id)),
	linkAssetContact: (assetId: string, body: { contact_id: string; relationship: string }) =>
		apiFetch(`/assets/${assetId}/contacts`, { method: 'POST', body: JSON.stringify(body) }),
	assetCases: (id: string) => fetchOrMock(`/assets/${id}/cases`, () => mock.assetCases(id)),
	linkAssetCase: (assetId: string, body: { case_id: string; relationship: string }) =>
		apiFetch(`/assets/${assetId}/cases`, { method: 'POST', body: JSON.stringify(body) }),

	// ─── Contacts ───────────────────────────────────────────────────────
	contacts: () => fetchOrMock('/contacts', mock.contacts),
	contact: (id: string) => fetchOrMock(`/contacts/${id}`, () => mock.contact(id)),
	createContact: (body: any) =>
		apiFetch('/contacts', { method: 'POST', body: JSON.stringify(body) }),
	updateContact: (id: string, body: any) =>
		apiFetch(`/contacts/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
	deleteContact: (id: string) =>
		apiFetch(`/contacts/${id}`, { method: 'DELETE' }),
	contactDocuments: (id: string) => fetchOrMock(`/contacts/${id}/documents`, () => mock.contactDocuments(id)),
	addContactDocument: (id: string, body: any) =>
		apiFetch(`/contacts/${id}/documents`, { method: 'POST', body: JSON.stringify(body) }),
	deleteContactDocument: (contactId: string, docId: string) =>
		apiFetch(`/contacts/${contactId}/documents/${docId}`, { method: 'DELETE' }),
	contactCases: (id: string) => fetchOrMock(`/contacts/${id}/cases`, () => mock.contactCases(id)),
	linkContactCase: (contactId: string, body: { case_id: string; relationship: string }) =>
		apiFetch(`/contacts/${contactId}/cases`, { method: 'POST', body: JSON.stringify(body) }),
	contactSignals: (id: string) => fetchOrMock(`/contacts/${id}/signals`, () => mock.contactSignals(id)),
	contactRelationships: (id: string) => fetchOrMock(`/contacts/${id}/relationships`, () => mock.contactRelationships(id)),
	addContactRelationship: (id: string, body: { target_id: string; relationship: string; description?: string }) =>
		apiFetch(`/contacts/${id}/relationships`, { method: 'POST', body: JSON.stringify(body) }),
	contactAssets: (id: string) => fetchOrMock(`/contacts/${id}/assets`, () => mock.contactAssets(id)),

	// ─── Graph Explorer ────────────────────────────────────────────────
	graphWorkspaces: () => fetchOrMock('/graph/workspaces', async () => ({
		workspaces: [
			// Watch Topics
			{ id: 'topic_ukraine', type: 'watch', title: 'Ukraine Conflict', node_count: 22, edge_count: 26, status: 'active', color: '#3b82f6',
			  description: 'OSINT monitoring — military, diplomatic, humanitarian',
			  updated_at: '2026-04-06T14:00:00Z', signal_count: 234,
			  top_entities: ['Petrenko', 'NATO', 'Zaporizhzhia NPP'],
			  tags: ['geopolitics', 'military', 'nuclear'] },
			{ id: 'topic_quantum', type: 'watch', title: 'Quantum Computing', node_count: 12, edge_count: 8, status: 'active', color: '#06b6d4',
			  description: 'Research tracking — IBM, Google, error correction',
			  updated_at: '2026-04-05T10:00:00Z', signal_count: 67,
			  top_entities: ['IBM Quantum', 'Google AI', 'Condor'],
			  tags: ['quantum', 'research', 'computing'] },
			{ id: 'topic_artist', type: 'watch', title: 'Nova Lane', node_count: 8, edge_count: 4, status: 'active', color: '#ec4899',
			  description: 'World Tour, media monitoring',
			  updated_at: '2026-04-06T08:00:00Z', signal_count: 412,
			  top_entities: ['Nova Lane', 'World Tour'],
			  tags: ['entertainment', 'music', 'touring'] },
			// Projects
			{ id: 'proj_sec_audit', type: 'project', title: 'Security Audit Q3', node_count: 8, edge_count: 8, status: 'active', color: '#a855f7',
			  description: 'Vehicle theft, network intrusions, phishing campaigns',
			  updated_at: '2026-04-06T11:30:00Z', case_count: 4,
			  top_entities: ['Sarah Chen', 'BMW X5', 'Phishing Cluster'],
			  tags: ['security', 'theft', 'phishing'] },
			{ id: 'proj_architecture', type: 'project', title: 'Architecture Research', node_count: 4, edge_count: 4, status: 'active', color: '#8b5cf6',
			  description: 'Megalithic site analysis — alignments and dating',
			  updated_at: '2026-04-04T09:00:00Z', case_count: 0,
			  top_entities: ['Gobekli Tepe', 'Stonehenge', 'Pillar alignments'],
			  tags: ['archaeology', 'megalithic', 'astronomy'] },
			{ id: 'proj_family', type: 'project', title: 'Family Safety', node_count: 3, edge_count: 2, status: 'active', color: '#22c55e',
			  description: 'Geofencing, school zone, perimeter monitoring',
			  updated_at: '2026-04-06T07:15:00Z', case_count: 1,
			  top_entities: ['School Zone', 'Home Perimeter', 'Suspicious Van'],
			  tags: ['family', 'geofence', 'safety'] },
			// Cases
			{ id: 'case_47', type: 'case', title: 'Stolen Vehicle Investigation', node_count: 5, edge_count: 5, status: 'investigating', color: '#f59e0b',
			  description: 'BMW X5 theft — garage CCTV, ALPR hits, witness statement',
			  updated_at: '2026-04-06T14:35:00Z', priority: 'high',
			  top_entities: ['Sarah Chen', 'BMW X5', 'Unknown suspect'],
			  tags: ['vehicle', 'theft', 'ALPR'] },
			{ id: 'case_63', type: 'case', title: 'Network Intrusion Analysis', node_count: 3, edge_count: 3, status: 'open', color: '#ef4444',
			  description: 'Unauthorized access, lateral movement, C2 exfiltration',
			  updated_at: '2026-04-06T08:00:00Z', priority: 'high',
			  top_entities: ['WS-DEV-042', 'C2 callback', 'Lateral movement'],
			  tags: ['intrusion', 'malware', 'C2'] },
		],
		// Cross-workspace links (shown as edges between workspace bubbles)
		links: [
			{ source: 'topic_ukraine', target: 'proj_sec_audit', relationship: 'feeds', strength: 0.8, label: 'Topic syncs to project' },
			{ source: 'proj_sec_audit', target: 'case_47', relationship: 'contains', strength: 0.95, label: 'Case within project' },
			{ source: 'proj_sec_audit', target: 'case_63', relationship: 'contains', strength: 0.90, label: 'Case within project' },
			{ source: 'proj_family', target: 'case_47', relationship: 'associated_with', strength: 0.35, label: 'Suspect vehicle matches school zone report', cross_workspace: true },
		],
	})),
	graphNodes: (workspaceId: string, workspaceType: string) => fetchOrMock(
		`/graph/nodes?workspace=${workspaceId}&type=${workspaceType}`,
		async () => {
			const nodesByWorkspace: Record<string, any[]> = {
				'proj_sec_audit': [
					{ id: 'kn_001', title: 'Sarah Chen', epistemic_type: 'observation', entity_type: 'person', source_authority: 'verified', mention_count: 12, workspace_id: 'proj_sec_audit', description: 'Vehicle owner, reported theft on 2026-03-10', confidence: 0.92, connections: 5, latitude: 38.8900, longitude: -77.0300 },
					{ id: 'kn_002', title: '2019 BMW X5', epistemic_type: 'fact', entity_type: 'asset', source_authority: 'verified', mention_count: 18, workspace_id: 'proj_sec_audit', description: 'Stolen vehicle — plate ABC-1234, VIN 1HGBH41JXMN109186', confidence: 0.98, connections: 4 },
					{ id: 'kn_003', title: 'James Porter', epistemic_type: 'observation', entity_type: 'person', source_authority: 'verified', mention_count: 3, workspace_id: 'proj_sec_audit', description: 'Night parking attendant — witnessed suspect at 02:30', confidence: 0.75, connections: 2 },
					{ id: 'kn_004', title: '1847 Industrial Blvd', epistemic_type: 'fact', entity_type: 'location', source_authority: 'verified', mention_count: 8, workspace_id: 'proj_sec_audit', description: 'Theft location — 3rd floor parking structure', confidence: 0.99, connections: 3, latitude: 38.8970, longitude: -77.0385 },
					{ id: 'kn_005', title: '5th & Main ALPR', epistemic_type: 'evidence', entity_type: 'location', source_authority: 'verified', mention_count: 5, workspace_id: 'proj_sec_audit', description: 'ALPR capture — vehicle spotted 14:00', confidence: 0.95, connections: 2, latitude: 38.8995, longitude: -77.0365 },
					{ id: 'kn_006', title: 'Phishing Cluster', epistemic_type: 'claim', entity_type: 'organization', source_authority: 'disputed', mention_count: 14, workspace_id: 'proj_sec_audit', description: 'Domains linked to credential harvesting', confidence: 0.68, connections: 6 },
					{ id: 'kn_007', title: 'mail-secure-login.com', epistemic_type: 'evidence', source_authority: 'verified', mention_count: 7, workspace_id: 'proj_sec_audit', description: 'Phishing domain, resolves to 185.x.x.x', confidence: 0.97, connections: 3 },
					{ id: 'kn_008', title: 'Unknown suspect', epistemic_type: 'hypothesis', entity_type: 'person', source_authority: 'unverified', mention_count: 4, workspace_id: 'proj_sec_audit', description: 'CCTV suspect — male, dark clothing, 5\'10"', confidence: 0.35, connections: 3 },
				],
				'proj_architecture': [
					{ id: 'kn_arch_01', title: 'Gobekli Tepe', epistemic_type: 'fact', entity_type: 'location', source_authority: 'verified', mention_count: 22, workspace_id: 'proj_architecture', description: 'Pre-pottery Neolithic site, ~9500 BCE', confidence: 0.95, connections: 4, latitude: 37.2231, longitude: 38.9225 },
					{ id: 'kn_arch_02', title: 'Pillar alignments', epistemic_type: 'hypothesis', source_authority: 'disputed', mention_count: 9, workspace_id: 'proj_architecture', description: 'T-shaped pillars may encode astronomical alignments', confidence: 0.45, connections: 3 },
					{ id: 'kn_arch_03', title: 'Stonehenge comparison', epistemic_type: 'claim', source_authority: 'verified', mention_count: 11, workspace_id: 'proj_architecture', description: 'Shared solstice alignment methodology', confidence: 0.60, connections: 3 },
					{ id: 'kn_arch_04', title: 'Dating controversy', epistemic_type: 'question', source_authority: 'disputed', mention_count: 6, workspace_id: 'proj_architecture', description: 'C14 dates contested — possible earlier occupation layer', confidence: 0.30, connections: 2 },
				],
				'proj_family': [
					{ id: 'kn_fam_01', title: 'School Zone Geofence', epistemic_type: 'fact', entity_type: 'location', source_authority: 'verified', mention_count: 15, workspace_id: 'proj_family', description: 'Lincoln Elementary — alerts on boundary exit', confidence: 0.99, connections: 2, latitude: 38.9100, longitude: -77.0200 },
					{ id: 'kn_fam_02', title: 'Home Perimeter', epistemic_type: 'fact', entity_type: 'location', source_authority: 'verified', mention_count: 20, workspace_id: 'proj_family', description: 'Ring camera + motion sensor coverage', confidence: 0.99, connections: 3, latitude: 38.9050, longitude: -77.0250 },
					{ id: 'kn_fam_03', title: 'Suspicious Van', epistemic_type: 'claim', entity_type: 'asset', source_authority: 'unverified', mention_count: 2, workspace_id: 'proj_family', description: 'White van seen near school at pickup, no plates', confidence: 0.40, connections: 2 },
				],
				'topic_ukraine': [
					{ id: 'ent_petrenko', title: 'Andriy Petrenko', entity_type: 'person', source_authority: 'verified', mention_count: 87, workspace_id: 'topic_ukraine', description: 'President of Ukraine', confidence: 0.99, connections: 4 },
					{ id: 'ent_iaea', title: 'IAEA', entity_type: 'organization', source_authority: 'verified', mention_count: 34, workspace_id: 'topic_ukraine', description: 'International Atomic Energy Agency', confidence: 0.98, connections: 3 },
					{ id: 'ent_ukraine', title: 'Ukraine', entity_type: 'country', source_authority: 'verified', mention_count: 234, workspace_id: 'topic_ukraine', description: 'Primary subject of monitoring', confidence: 0.99, connections: 8, latitude: 48.3794, longitude: 31.1656 },
					{ id: 'ent_russia', title: 'Russia', entity_type: 'country', source_authority: 'verified', mention_count: 189, workspace_id: 'topic_ukraine', description: 'Belligerent state', confidence: 0.99, connections: 5, latitude: 61.524, longitude: 105.3188 },
					{ id: 'ent_nato', title: 'NATO', entity_type: 'organization', source_authority: 'verified', mention_count: 45, workspace_id: 'topic_ukraine', description: 'North Atlantic Treaty Organization', confidence: 0.99, connections: 4 },
					{ id: 'ent_zaporizhzhia', title: 'Zaporizhzhia NPP', entity_type: 'location', source_authority: 'verified', mention_count: 56, workspace_id: 'topic_ukraine', description: 'Nuclear power plant under occupation', confidence: 0.98, connections: 4, latitude: 47.5069, longitude: 34.5853 },
					{ id: 'ent_crimea', title: 'Crimea', entity_type: 'region', source_authority: 'disputed', mention_count: 28, workspace_id: 'topic_ukraine', description: 'Annexed 2014, internationally disputed', confidence: 0.85, connections: 3, latitude: 44.9521, longitude: 34.1024 },
				],
				'topic_quantum': [
					{ id: 'ent_ibm_q', title: 'IBM Quantum', entity_type: 'organization', source_authority: 'verified', mention_count: 23, workspace_id: 'topic_quantum', description: '1121-qubit Condor processor', confidence: 0.95, connections: 3 },
					{ id: 'ent_google_q', title: 'Google Quantum AI', entity_type: 'organization', source_authority: 'verified', mention_count: 18, workspace_id: 'topic_quantum', description: 'Error correction milestone', confidence: 0.92, connections: 2 },
					{ id: 'ent_condor', title: 'Condor Processor', epistemic_type: 'fact', source_authority: 'verified', mention_count: 15, workspace_id: 'topic_quantum', description: '1121-qubit chip, 3x error rate improvement', confidence: 0.90, connections: 2 },
				],
				'topic_artist': [
					{ id: 'ent_artist', title: 'Nova Lane', entity_type: 'person', source_authority: 'verified', mention_count: 412, workspace_id: 'topic_artist', description: 'Artist, World Tour', confidence: 0.99, connections: 3 },
					{ id: 'ent_world_tour', title: 'World Tour', entity_type: 'organization', source_authority: 'verified', mention_count: 280, workspace_id: 'topic_artist', description: '$2.1B highest-grossing tour in history', confidence: 0.98, connections: 2 },
				],
				'case_47': [
					{ id: 'kn_001', title: 'Sarah Chen', epistemic_type: 'observation', entity_type: 'person', source_authority: 'verified', mention_count: 12, workspace_id: 'case_47', description: 'Vehicle owner', confidence: 0.92, connections: 3 },
					{ id: 'kn_002', title: '2019 BMW X5', epistemic_type: 'fact', entity_type: 'asset', source_authority: 'verified', mention_count: 18, workspace_id: 'case_47', description: 'Stolen vehicle — plate ABC-1234', confidence: 0.98, connections: 4 },
					{ id: 'kn_004', title: '1847 Industrial Blvd', epistemic_type: 'fact', entity_type: 'location', source_authority: 'verified', mention_count: 8, workspace_id: 'case_47', description: 'Theft location', confidence: 0.99, connections: 3, latitude: 38.8970, longitude: -77.0385 },
					{ id: 'kn_008', title: 'Unknown suspect', epistemic_type: 'hypothesis', entity_type: 'person', source_authority: 'unverified', mention_count: 4, workspace_id: 'case_47', description: 'CCTV suspect', confidence: 0.35, connections: 2 },
					{ id: 'kn_005', title: '5th & Main ALPR', epistemic_type: 'evidence', entity_type: 'location', source_authority: 'verified', mention_count: 5, workspace_id: 'case_47', description: 'ALPR capture point', confidence: 0.95, connections: 2, latitude: 38.8995, longitude: -77.0365 },
				],
				'case_63': [
					{ id: 'kn_net_01', title: 'Compromised endpoint', epistemic_type: 'fact', entity_type: 'asset', source_authority: 'verified', mention_count: 8, workspace_id: 'case_63', description: 'WS-DEV-042 — initial access point', confidence: 0.92, connections: 3 },
					{ id: 'kn_net_02', title: 'Lateral movement', epistemic_type: 'evidence', source_authority: 'verified', mention_count: 5, workspace_id: 'case_63', description: 'SMB spread to 3 hosts within 2h', confidence: 0.88, connections: 2 },
					{ id: 'kn_net_03', title: 'C2 callback', epistemic_type: 'evidence', source_authority: 'verified', mention_count: 6, workspace_id: 'case_63', description: 'Beacon to 45.x.x.x every 60s', confidence: 0.95, connections: 2 },
				],
			};
			const edgesByWorkspace: Record<string, any[]> = {
				'proj_sec_audit': [
					{ source: 'kn_001', target: 'kn_002', relationship: 'owns', strength: 0.95 },
					{ source: 'kn_002', target: 'kn_004', relationship: 'located_in', strength: 0.90 },
					{ source: 'kn_003', target: 'kn_004', relationship: 'located_in', strength: 0.85 },
					{ source: 'kn_003', target: 'kn_008', relationship: 'witnessed', strength: 0.60 },
					{ source: 'kn_008', target: 'kn_002', relationship: 'questions', strength: 0.40 },
					{ source: 'kn_005', target: 'kn_002', relationship: 'evidence', strength: 0.95 },
					{ source: 'kn_006', target: 'kn_007', relationship: 'supports', strength: 0.85 },
					{ source: 'kn_007', target: 'kn_006', relationship: 'member_of', strength: 0.80 },
				],
				'proj_architecture': [
					{ source: 'kn_arch_01', target: 'kn_arch_02', relationship: 'supports', strength: 0.70 },
					{ source: 'kn_arch_01', target: 'kn_arch_03', relationship: 'references', strength: 0.60 },
					{ source: 'kn_arch_04', target: 'kn_arch_01', relationship: 'questions', strength: 0.50 },
					{ source: 'kn_arch_03', target: 'kn_arch_02', relationship: 'supports', strength: 0.55 },
				],
				'proj_family': [
					{ source: 'kn_fam_01', target: 'kn_fam_02', relationship: 'references', strength: 0.60 },
					{ source: 'kn_fam_03', target: 'kn_fam_01', relationship: 'located_in', strength: 0.55 },
				],
				'topic_ukraine': [
					{ source: 'ent_petrenko', target: 'ent_ukraine', relationship: 'leads', strength: 1.0 },
					{ source: 'ent_petrenko', target: 'ent_nato', relationship: 'member_of', strength: 0.9 },
					{ source: 'ent_iaea', target: 'ent_zaporizhzhia', relationship: 'inspected', strength: 0.95 },
					{ source: 'ent_zaporizhzhia', target: 'ent_ukraine', relationship: 'located_in', strength: 1.0 },
					{ source: 'ent_crimea', target: 'ent_ukraine', relationship: 'located_in', strength: 0.8 },
					{ source: 'ent_crimea', target: 'ent_russia', relationship: 'contrasts', strength: 0.6, evidence: 'Annexed 2014, internationally disputed' },
					{ source: 'ent_russia', target: 'ent_zaporizhzhia', relationship: 'contrasts', strength: 0.8, evidence: 'Occupying force since 2022' },
					{ source: 'ent_nato', target: 'ent_ukraine', relationship: 'supports', strength: 0.85 },
				],
				'topic_quantum': [
					{ source: 'ent_ibm_q', target: 'ent_condor', relationship: 'created', strength: 0.95 },
					{ source: 'ent_google_q', target: 'ent_ibm_q', relationship: 'contrasts', strength: 0.50, evidence: 'Competing approaches to error correction' },
				],
				'topic_artist': [
					{ source: 'ent_artist', target: 'ent_world_tour', relationship: 'leads', strength: 1.0 },
				],
				'case_47': [
					{ source: 'kn_001', target: 'kn_002', relationship: 'owns', strength: 0.95 },
					{ source: 'kn_002', target: 'kn_004', relationship: 'located_in', strength: 0.90 },
					{ source: 'kn_008', target: 'kn_002', relationship: 'questions', strength: 0.40 },
					{ source: 'kn_005', target: 'kn_002', relationship: 'evidence', strength: 0.95 },
					{ source: 'kn_008', target: 'kn_004', relationship: 'located_in', strength: 0.55 },
				],
				'case_63': [
					{ source: 'kn_net_01', target: 'kn_net_02', relationship: 'led_to', strength: 0.90 },
					{ source: 'kn_net_02', target: 'kn_net_03', relationship: 'supports', strength: 0.85 },
					{ source: 'kn_net_01', target: 'kn_net_03', relationship: 'supports', strength: 0.70 },
				],
			};
			const nodes = nodesByWorkspace[workspaceId] || [];
			const edges = edgesByWorkspace[workspaceId] || [];
			return { nodes, edges };
		}
	),

	// ─── Cameras (local DB, fast spatial query) ─────────────────────────
	cameras: (bounds: { north: number; south: number; east: number; west: number; zoom?: number }, limit = 5000) =>
		fetchOrMock(`/cameras?north=${bounds.north}&south=${bounds.south}&east=${bounds.east}&west=${bounds.west}&zoom=${bounds.zoom ?? 12}&limit=${limit}`,
			async () => ({ count: 0, cameras: [] })),
	cameraStats: () => fetchOrMock('/cameras/stats', async () => ({ total: 0, providers: [] })),
	syncCameras: () => apiFetch('/cameras/sync', { method: 'POST' }),
};
