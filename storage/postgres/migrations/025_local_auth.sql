-- Local password authentication for hardware nodes.
-- Adds password_hash and email to identities table.
-- Allows auth without Logto (fully offline capable).

ALTER TABLE identities ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);
ALTER TABLE identities ADD COLUMN IF NOT EXISTS email VARCHAR(255);

-- Email uniqueness (within tenant)
CREATE UNIQUE INDEX IF NOT EXISTS idx_identities_email_tenant
    ON identities(email, tenant_id) WHERE email IS NOT NULL;
