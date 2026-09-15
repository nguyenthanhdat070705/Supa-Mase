-- REVIEW TEMPLATE ONLY: do not execute unchanged or before the captain merges.
-- Synthetic schema/table names are deliberate. Replace them only with the exact
-- audited business relations. No production data modification is needed here.
-- Run as trusted operator; do not hand this role's password or an admin key to a child.
-- This creates a NEW role; do not rotate/reconfigure a shared production reader.

BEGIN;
CREATE ROLE sm_analytics LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
    NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 4;
-- No password is embedded. Provision a random password through the operator's
-- secret facility, or psql \password sm_analytics after commit, with logging review.
-- NOSUPERUSER/NOINHERIT do NOT subtract PUBLIC privileges or disable SET ROLE.
-- Runtime refuses any memberships, ownership, CREATE or effective write rights.
GRANT CONNECT ON DATABASE postgres TO sm_analytics;
GRANT USAGE ON SCHEMA business_example TO sm_analytics;
GRANT SELECT (id, region, total) ON business_example.orders TO sm_analytics;

-- Existing RLS must already be enabled and reviewed. Do not silently enable RLS
-- here: changing RLS state can alter existing production application behavior.
-- Use USING (true) ONLY where this reader is approved for every business row.
-- A SELECT policy is scoped to this role; it changes no other role's policies.
CREATE POLICY sm_analytics_select ON business_example.orders
    FOR SELECT TO sm_analytics USING (true);

-- These defaults are defense in depth. They are user-settable and cannot alone
-- enforce read-only access; the trusted broker starts READ ONLY on every call.
ALTER ROLE sm_analytics SET default_transaction_read_only = on;
ALTER ROLE sm_analytics SET statement_timeout = '15s';
ALTER ROLE sm_analytics SET lock_timeout = '1s';
ALTER ROLE sm_analytics SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE sm_analytics SET search_path = pg_catalog;
COMMIT;

-- Do NOT GRANT role memberships, ownership, ALL TABLES, sequence USAGE/UPDATE,
-- schema/database CREATE, table/column writes, MAINTAIN, or RPC execution.
-- Do NOT ALTER DEFAULT PRIVILEGES for existing application object creators here.
-- Do NOT REVOKE from PUBLIC broadly: it can break production and direct REVOKE
-- from sm_analytics cannot subtract an inherited PUBLIC privilege.
-- If PUBLIC rights cause preflight to fail, stop enrollment and have the owner
-- review a narrowly scoped remediation or isolated sanitized export database.
-- After provisioning: audit-reader.sql + runtime role preflight under the reader;
-- inspect RLS/view/function/type/index dependencies, then fingerprint enrollment.
