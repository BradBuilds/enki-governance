-- Clear Working Data — preserves auth, system config, AI providers
-- Handles missing tables gracefully (no transaction — each TRUNCATE independent)

DO $$
DECLARE
    t TEXT;
    tables_to_clear TEXT[] := ARRAY[
        'watch_topics',
        'signals', 'signal_views', 'signal_receipts',
        'signal_extractions', 'processing_queue',
        'knowledge_nodes', 'knowledge_edges', 'concept_index',
        'entities', 'entity_identifiers', 'entity_documents', 'entity_images',
        'entity_redirects', 'entity_enrichment_queue', 'entity_resolution_log',
        'cases', 'case_documents', 'case_observables', 'case_signals',
        'case_alerts', 'case_events', 'case_nodes',
        'contacts', 'contact_documents', 'contact_relationships', 'contact_cases',
        'assets', 'asset_contacts', 'asset_cases', 'asset_documents',
        'financial_accounts', 'financial_balance_history', 'financial_recurring',
        'projects', 'project_chat_history',
        'chat_threads', 'thread_messages', 'thread_nodes',
        'collections', 'collection_items',
        'connectors',
        'detection_rules', 'alerts',
        'tracks', 'geofences', 'map_annotations',
        'reports', 'report_schedules',
        'ldu_t_signals', 'ldu_p_entries',
        'extraction_passes', 'learned_mappings', 'research_passes',
        'live_cache', 'live_history',
        'observable_index', 'unknown_signal_types', 'vocabulary_proposals',
        'import_jobs',
        'topics', 'places'
    ];
BEGIN
    FOREACH t IN ARRAY tables_to_clear LOOP
        BEGIN
            EXECUTE format('TRUNCATE %I CASCADE', t);
            RAISE NOTICE 'Cleared: %', t;
        EXCEPTION WHEN undefined_table THEN
            RAISE NOTICE 'Skipped (not found): %', t;
        WHEN OTHERS THEN
            RAISE NOTICE 'Error clearing %: %', t, SQLERRM;
        END;
    END LOOP;
END $$;

-- Show preserved tables
SELECT 'PRESERVED' as status, tablename
FROM pg_tables
WHERE schemaname = 'public'
    AND tablename IN ('tenants', 'identities', 'ai_providers', 'ai_config', 'secrets', 'setup_config', 'connector_recipes')
ORDER BY tablename;
