-- M6 Signal Hub integration, engine side (docs/contracts/100-m6-signals.md §6). Re-runnable.
--
-- * connector.auth_kind gains 'service_jwt' (internal first-party services such as the Signal Hub;
--   provisioned by bootstrap, never registrable through the back-office).
-- * alert_case: one case per Signal Hub alert (POST /api/alerts/{id}/open-case and the automatic
--   draft case of an alert_and_draft_case alert are idempotent through its primary key). A tenant
--   table (ENABLE + FORCE RLS, composite FK to the case; deleting the case deletes the mapping).
-- * connector_event.hub_forwarded_at / hub_forward_attempts: the webhook → hub forward outbox (a
--   forward that failed inline is retried by engine-api's alert watcher).

ALTER TABLE connector DROP CONSTRAINT IF EXISTS connector_auth_kind_chk;
ALTER TABLE connector ADD CONSTRAINT connector_auth_kind_chk
  CHECK (auth_kind IN ('none', 'static_header', 'oauth_client_credentials', 'oauth_platform_merxa', 'service_jwt'));

CREATE TABLE IF NOT EXISTS alert_case (
  tenant      text NOT NULL REFERENCES tenant(slug),
  alert_id    uuid NOT NULL,
  event_id    uuid NOT NULL,
  case_id     text NOT NULL,
  run_id      text,
  auto        boolean NOT NULL DEFAULT false,
  opened_by   text NOT NULL,
  hub_synced  boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, alert_id),
  UNIQUE (tenant, case_id),
  FOREIGN KEY (tenant, case_id) REFERENCES "case" (tenant, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS alert_case_unsynced_idx ON alert_case (tenant, created_at) WHERE NOT hub_synced;

ALTER TABLE alert_case ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_case FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON alert_case;
CREATE POLICY tenant_isolation ON alert_case AS PERMISSIVE FOR ALL
  USING (tenant = current_setting('app.tenant_id', true) OR current_setting('app.system', true) = 'on')
  WITH CHECK (tenant = current_setting('app.tenant_id', true) OR current_setting('app.system', true) = 'on');

ALTER TABLE connector_event ADD COLUMN IF NOT EXISTS hub_forwarded_at timestamptz;
ALTER TABLE connector_event ADD COLUMN IF NOT EXISTS hub_forward_attempts integer NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS connector_event_hub_pending_idx ON connector_event (received_at, id)
  WHERE hub_forwarded_at IS NULL;
