-- Baseline: the schema as it stood before numbered migrations (M0-M2).
-- Every statement is idempotent so that a database created by the old
-- `ensure_schema` is adopted without change.

CREATE TABLE IF NOT EXISTS snapshots (
  tenant_id text NOT NULL, rkp_hash text NOT NULL, repository_id text NOT NULL, commit_sha text NOT NULL,
  status text NOT NULL, manifest jsonb NOT NULL, PRIMARY KEY (tenant_id, rkp_hash));
CREATE INDEX IF NOT EXISTS snapshots_commit ON snapshots (tenant_id, repository_id, commit_sha);
CREATE TABLE IF NOT EXISTS snapshot_shards (
  tenant_id text NOT NULL, rkp_hash text NOT NULL, unit text NOT NULL, shard_digest text NOT NULL,
  PRIMARY KEY (tenant_id, rkp_hash, unit));
CREATE TABLE IF NOT EXISTS shards (tenant_id text NOT NULL, shard_digest text NOT NULL, PRIMARY KEY (tenant_id, shard_digest));
CREATE TABLE IF NOT EXISTS entities (
  tenant_id text NOT NULL, shard_digest text NOT NULL, entity_id text NOT NULL, kind text NOT NULL, name text NOT NULL,
  row jsonb NOT NULL, PRIMARY KEY (tenant_id, shard_digest, entity_id));
CREATE TABLE IF NOT EXISTS assertions (
  tenant_id text NOT NULL, shard_digest text NOT NULL, assertion_id text NOT NULL, subject_id text NOT NULL,
  predicate text NOT NULL, object_id text NOT NULL, row jsonb NOT NULL, PRIMARY KEY (tenant_id, shard_digest, assertion_id));
CREATE INDEX IF NOT EXISTS assertions_subject ON assertions (tenant_id, subject_id, predicate);
CREATE INDEX IF NOT EXISTS assertions_object ON assertions (tenant_id, object_id, predicate);
CREATE TABLE IF NOT EXISTS evidence (
  tenant_id text NOT NULL, shard_digest text NOT NULL, evidence_id text NOT NULL, blob_sha256 text NOT NULL,
  row jsonb NOT NULL, PRIMARY KEY (tenant_id, shard_digest, evidence_id));

CREATE TABLE IF NOT EXISTS business_maps (
  tenant_id text NOT NULL, map_id text NOT NULL, version int NOT NULL,
  PRIMARY KEY (tenant_id, map_id));
CREATE TABLE IF NOT EXISTS business_map_stages (
  tenant_id text NOT NULL, map_id text NOT NULL, stage_id text NOT NULL, position int NOT NULL,
  name text NOT NULL, PRIMARY KEY (tenant_id, map_id, stage_id),
  FOREIGN KEY (tenant_id, map_id) REFERENCES business_maps (tenant_id, map_id));
CREATE TABLE IF NOT EXISTS business_map_capabilities (
  tenant_id text NOT NULL, map_id text NOT NULL, stage_id text NOT NULL, capability_id text NOT NULL,
  position int NOT NULL, name text NOT NULL, description text NOT NULL,
  PRIMARY KEY (tenant_id, map_id, capability_id),
  FOREIGN KEY (tenant_id, map_id, stage_id)
    REFERENCES business_map_stages (tenant_id, map_id, stage_id));
CREATE TABLE IF NOT EXISTS business_map_placements (
  tenant_id text NOT NULL, map_id text NOT NULL, lineage_id text NOT NULL, capability_id text,
  row jsonb NOT NULL, PRIMARY KEY (tenant_id, map_id, lineage_id),
  FOREIGN KEY (tenant_id, map_id) REFERENCES business_maps (tenant_id, map_id),
  FOREIGN KEY (tenant_id, map_id, capability_id)
    REFERENCES business_map_capabilities (tenant_id, map_id, capability_id));
CREATE TABLE IF NOT EXISTS business_map_revisions (
  tenant_id text NOT NULL, map_id text NOT NULL, version int NOT NULL, author text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(), document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, map_id, version),
  FOREIGN KEY (tenant_id, map_id) REFERENCES business_maps (tenant_id, map_id));

CREATE TABLE IF NOT EXISTS repository_registry (
  tenant_id text NOT NULL, repository_id text NOT NULL, document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, repository_id));
CREATE TABLE IF NOT EXISTS connection_checks (
  tenant_id text NOT NULL, connection_id text NOT NULL, document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, connection_id));
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  tenant_id text NOT NULL, connection_id text NOT NULL, delivery_id text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, connection_id, delivery_id));

CREATE TABLE IF NOT EXISTS workspace_settings (
  tenant_id text PRIMARY KEY, version int NOT NULL, document jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS workspace_settings_revisions (
  tenant_id text NOT NULL, version int NOT NULL, document jsonb NOT NULL,
  author text NOT NULL, updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, version));

CREATE TABLE IF NOT EXISTS business_lineage (
  tenant_id text NOT NULL, rkp_hash text NOT NULL, entity_id text NOT NULL, lineage_id text NOT NULL,
  PRIMARY KEY (tenant_id, rkp_hash, entity_id));
