-- Optional Supabase installation step, executed by the application's table owner.
-- The FastAPI server uses its private PostgreSQL connection and owns authorization.
-- These tables are not a direct browser/Data API. No broad auth.uid() policy is added.
BEGIN;
DO $$
DECLARE
    app_table record;
    api_role text;
BEGIN
    FOR app_table IN
        SELECT schemaname, tablename FROM pg_catalog.pg_tables
        WHERE schemaname = 'public'
          AND (left(tablename, 4) = 'ext_' OR tablename = 'project_hierarchy')
    LOOP
        EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', app_table.schemaname, app_table.tablename);
        EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE %I.%I FROM PUBLIC', app_table.schemaname, app_table.tablename);
        FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated']
        LOOP
            IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = api_role) THEN
                EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE %I.%I FROM %I', app_table.schemaname, app_table.tablename, api_role);
            END IF;
        END LOOP;
    END LOOP;
END $$;
COMMIT;
