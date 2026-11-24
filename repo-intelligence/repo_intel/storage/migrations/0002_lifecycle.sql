-- M3 lifecycle (GI-018 to GI-021): snapshot status and retraction, tracked refs,
-- general entity lineage, parity incidents, audit records, and the narrow
-- maintenance functions through which the application role deletes rows.

ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS retraction jsonb;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS published_at timestamptz NOT NULL DEFAULT clock_timestamp();
CREATE INDEX IF NOT EXISTS snapshots_repository ON snapshots (tenant_id, repository_id);
CREATE INDEX IF NOT EXISTS snapshot_shards_digest ON snapshot_shards (tenant_id, shard_digest);
CREATE INDEX IF NOT EXISTS evidence_blob ON evidence (tenant_id, blob_sha256);

-- A tracked ref points at its newest head and at the snapshot last published for it.
-- The ref view is `current` when they agree and `stale` otherwise (spec §4.4).
CREATE TABLE IF NOT EXISTS tracked_refs (
  tenant_id text NOT NULL, repository_id text NOT NULL, ref text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('branch', 'tag', 'pull_request')),
  head_sha text, head_seen_at timestamptz,
  rkp_hash text, published_sha text, published_at timestamptz,
  closed_at timestamptz, compile_after timestamptz, requested_at timestamptz,
  PRIMARY KEY (tenant_id, repository_id, ref));

-- Lineage for every entity kind (spec §4.2). A row exists where an entity's
-- lineage differs from its own ID; anything without a row is its own lineage.
CREATE TABLE IF NOT EXISTS entity_lineage (
  tenant_id text NOT NULL, repository_id text NOT NULL, rkp_hash text NOT NULL,
  entity_id text NOT NULL, lineage_id text NOT NULL, previous_id text, basis text NOT NULL,
  PRIMARY KEY (tenant_id, rkp_hash, entity_id));
CREATE INDEX IF NOT EXISTS entity_lineage_lineage ON entity_lineage (tenant_id, lineage_id);
CREATE INDEX IF NOT EXISTS entity_lineage_entity ON entity_lineage (tenant_id, entity_id);
CREATE INDEX IF NOT EXISTS entity_lineage_repository ON entity_lineage (tenant_id, repository_id);
INSERT INTO entity_lineage (tenant_id, repository_id, rkp_hash, entity_id, lineage_id, basis)
  SELECT b.tenant_id, s.repository_id, b.rkp_hash, b.entity_id, b.lineage_id, 'shared_evidence'
  FROM business_lineage b JOIN snapshots s USING (tenant_id, rkp_hash)
  WHERE b.entity_id <> b.lineage_id
  ON CONFLICT DO NOTHING;
DROP TABLE IF EXISTS business_lineage;

CREATE TABLE IF NOT EXISTS parity_incidents (
  tenant_id text NOT NULL, incident_id text NOT NULL, repository_id text NOT NULL,
  incremental_rkp_hash text NOT NULL, cold_rkp_hash text NOT NULL, differing jsonb NOT NULL,
  opened_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, incident_id));

-- Audit records hold hashes and counts, never repository content (spec §12).
CREATE TABLE IF NOT EXISTS audit_events (
  tenant_id text NOT NULL, event_id text NOT NULL, at timestamptz NOT NULL DEFAULT clock_timestamp(),
  actor text NOT NULL, action text NOT NULL, subject_hash text NOT NULL, details jsonb NOT NULL,
  PRIMARY KEY (tenant_id, event_id));

-- Maintenance functions. The application role keeps SELECT/INSERT on content
-- tables and cannot delete arbitrary rows; it calls these instead. Each acts
-- only on the tenant bound to the session and refuses to run without one.

CREATE OR REPLACE FUNCTION rkp_bound_tenant() RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE t text := current_setting('rkp.tenant_id', true);
BEGIN
  IF t IS NULL OR t = '' THEN RAISE EXCEPTION 'no tenant is bound to this session'; END IF;
  RETURN t;
END $$;

-- Remove shard rows that no snapshot of the tenant links to any more.
CREATE OR REPLACE FUNCTION rkp_sweep_shards() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE t text := rkp_bound_tenant(); n integer;
BEGIN
  DELETE FROM entities x WHERE x.tenant_id = t
    AND NOT EXISTS (SELECT 1 FROM snapshot_shards s WHERE s.tenant_id = t AND s.shard_digest = x.shard_digest);
  DELETE FROM assertions x WHERE x.tenant_id = t
    AND NOT EXISTS (SELECT 1 FROM snapshot_shards s WHERE s.tenant_id = t AND s.shard_digest = x.shard_digest);
  DELETE FROM evidence x WHERE x.tenant_id = t
    AND NOT EXISTS (SELECT 1 FROM snapshot_shards s WHERE s.tenant_id = t AND s.shard_digest = x.shard_digest);
  DELETE FROM shards x WHERE x.tenant_id = t
    AND NOT EXISTS (SELECT 1 FROM snapshot_shards s WHERE s.tenant_id = t AND s.shard_digest = x.shard_digest);
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- Drop whole snapshots (retention, deletion) and any shard rows left orphaned.
CREATE OR REPLACE FUNCTION rkp_drop_snapshots(p_hashes text[]) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE t text := rkp_bound_tenant(); n integer;
BEGIN
  DELETE FROM snapshot_shards WHERE tenant_id = t AND rkp_hash = ANY(p_hashes);
  DELETE FROM entity_lineage WHERE tenant_id = t AND rkp_hash = ANY(p_hashes);
  DELETE FROM snapshots WHERE tenant_id = t AND rkp_hash = ANY(p_hashes);
  GET DIAGNOSTICS n = ROW_COUNT;
  PERFORM rkp_sweep_shards();
  RETURN n;
END $$;

-- Tombstone a snapshot: keep the row and its manifest (hashes only), record why
-- and what replaces it, and unlink its content so shard rows only it used go.
CREATE OR REPLACE FUNCTION rkp_retract_snapshot(p_hash text, p_retraction jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE t text := rkp_bound_tenant(); found boolean;
BEGIN
  UPDATE snapshots SET status = 'retracted', retraction = p_retraction
    WHERE tenant_id = t AND rkp_hash = p_hash;
  found := FOUND;
  DELETE FROM snapshot_shards WHERE tenant_id = t AND rkp_hash = p_hash;
  PERFORM rkp_sweep_shards();
  RETURN found;
END $$;

-- Delete everything the projection, registry, lineage, refs and business map
-- hold for one repository. Placements are matched by lineage ID and by the
-- snapshot they were made from; map and settings history is redacted the same way.
CREATE OR REPLACE FUNCTION rkp_purge_repository(p_repository text, p_lineage text[]) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  t text := rkp_bound_tenant();
  hashes text[] := ARRAY(SELECT rkp_hash FROM snapshots WHERE tenant_id = t AND repository_id = p_repository);
  lineage text[] := p_lineage || ARRAY(SELECT DISTINCT lineage_id FROM entity_lineage WHERE tenant_id = t AND repository_id = p_repository);
  placements integer;
BEGIN
  PERFORM rkp_drop_snapshots(hashes);
  DELETE FROM entity_lineage WHERE tenant_id = t AND repository_id = p_repository;
  DELETE FROM tracked_refs WHERE tenant_id = t AND repository_id = p_repository;
  DELETE FROM parity_incidents WHERE tenant_id = t AND repository_id = p_repository;
  DELETE FROM repository_registry WHERE tenant_id = t AND repository_id = p_repository;
  DELETE FROM business_map_placements WHERE tenant_id = t
    AND (lineage_id = ANY(lineage) OR row->>'source_rkp_hash' = ANY(hashes));
  GET DIAGNOSTICS placements = ROW_COUNT;
  UPDATE business_map_revisions SET document = jsonb_set(document, '{placements}', COALESCE((
      SELECT jsonb_agg(p) FROM jsonb_array_elements(document->'placements') p
      WHERE NOT (p->>'lineage_id' = ANY(lineage) OR COALESCE(p->>'source_rkp_hash', '') = ANY(hashes))), '[]'::jsonb))
    WHERE tenant_id = t AND jsonb_typeof(document->'placements') = 'array';
  UPDATE workspace_settings SET document = document #- ARRAY['repositories', p_repository]
    WHERE tenant_id = t;
  UPDATE workspace_settings_revisions SET document = document #- ARRAY['repositories', p_repository]
    WHERE tenant_id = t;
  RETURN jsonb_build_object('snapshots', cardinality(hashes), 'placements', placements);
END $$;

-- Delete every row the tenant owns, except its audit records.
CREATE OR REPLACE FUNCTION rkp_purge_tenant() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE t text := rkp_bound_tenant(); tbl text; total integer := 0; n integer;
BEGIN
  FOREACH tbl IN ARRAY ARRAY['snapshot_shards', 'entities', 'assertions', 'evidence', 'shards', 'snapshots',
      'entity_lineage', 'tracked_refs', 'parity_incidents', 'repository_registry', 'connection_checks',
      'webhook_deliveries', 'business_map_placements', 'business_map_capabilities', 'business_map_stages',
      'business_map_revisions', 'business_maps', 'workspace_settings_revisions', 'workspace_settings'] LOOP
    EXECUTE format('DELETE FROM %I WHERE tenant_id = $1', tbl) USING t;
    GET DIAGNOSTICS n = ROW_COUNT;
    total := total + n;
  END LOOP;
  RETURN total;
END $$;
