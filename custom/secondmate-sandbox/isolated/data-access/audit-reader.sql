-- OPERATOR REVIEW TEMPLATE ONLY. No live execution performed.
-- Replace every 'sm_analytics' with the exact separately created reader role.
-- Catalog reads only; do not print function/view/policy source or role passwords.
-- Run under a trusted operator connection, not a service/admin token in the agent.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '30s';
SET LOCAL search_path = pg_catalog;

SELECT current_database(), current_setting('server_version_num') AS server_version_num;

-- Role existence and dangerous role attributes. No rolpassword/rolconfig output.
SELECT oid, rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
       rolreplication, rolbypassrls, rolinherit, rolconnlimit
FROM pg_catalog.pg_roles
WHERE rolname = 'sm_analytics';

-- No memberships is the simplest invariant. This includes indirect membership.
SELECT r.rolname AS reachable_role, r.rolsuper, r.rolcreatedb, r.rolcreaterole,
       r.rolreplication, r.rolbypassrls
FROM pg_catalog.pg_roles r
WHERE r.rolname <> 'sm_analytics'
  AND pg_catalog.pg_has_role('sm_analytics', r.oid, 'MEMBER');

-- Effective database rights include PUBLIC; a direct REVOKE cannot subtract them.
SELECT d.datname,
       pg_catalog.has_database_privilege('sm_analytics', d.oid, 'CONNECT') AS can_connect,
       pg_catalog.has_database_privilege('sm_analytics', d.oid, 'CREATE') AS can_create,
       pg_catalog.has_database_privilege('sm_analytics', d.oid, 'TEMPORARY') AS can_temp,
       pg_catalog.pg_get_userbyid(d.datdba) AS owner
FROM pg_catalog.pg_database d WHERE d.datname = current_database();

SELECT n.oid, n.nspname,
       pg_catalog.pg_get_userbyid(n.nspowner) AS owner,
       pg_catalog.has_schema_privilege('sm_analytics', n.oid, 'USAGE') AS can_use,
       pg_catalog.has_schema_privilege('sm_analytics', n.oid, 'CREATE') AS can_create
FROM pg_catalog.pg_namespace n
ORDER BY n.nspname;

-- Table grants alone miss column-specific INSERT/UPDATE/REFERENCES rights.
SELECT n.nspname, c.relname, c.relkind,
       pg_catalog.pg_get_userbyid(c.relowner) AS owner,
       pg_catalog.has_table_privilege('sm_analytics', c.oid, 'SELECT') AS can_select,
       pg_catalog.has_table_privilege('sm_analytics', c.oid,
          'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS table_write_or_control,
       pg_catalog.has_any_column_privilege('sm_analytics', c.oid,
          'INSERT,UPDATE,REFERENCES') AS column_write_or_control,
       CASE WHEN current_setting('server_version_num')::int >= 170000
            THEN pg_catalog.has_table_privilege('sm_analytics',c.oid,'MAINTAIN')
            ELSE false END AS can_maintain,
       c.relrowsecurity, c.relforcerowsecurity, c.reloptions
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','p','v','m','f')
  AND n.nspname NOT IN ('pg_catalog','information_schema')
ORDER BY n.nspname, c.relname;

SELECT n.nspname, c.relname,
       pg_catalog.has_sequence_privilege('sm_analytics', c.oid, 'USAGE,UPDATE') AS can_advance_or_set,
       pg_catalog.has_sequence_privilege('sm_analytics', c.oid, 'SELECT') AS can_read
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'S'
ORDER BY n.nspname, c.relname;

-- Report all callable non-catalog/SECURITY DEFINER routines and selected dangerous
-- catalog families. Absence here is not a complete executable-code purity proof.
-- Keep routines even without schema USAGE: a stored view can reference them.
SELECT p.oid, n.nspname, p.proname,
       pg_catalog.pg_get_function_identity_arguments(p.oid) AS identity_arguments,
       pg_catalog.pg_get_userbyid(p.proowner) AS owner,
       p.prokind, p.prosecdef, p.provolatile,
       pg_catalog.has_schema_privilege('sm_analytics', n.oid, 'USAGE') AS direct_schema_usage,
       pg_catalog.has_function_privilege('sm_analytics', p.oid, 'EXECUTE') AS effective_execute,
       EXISTS (
         SELECT 1 FROM pg_catalog.aclexplode(
           COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))) a
         WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE'
       ) AS public_execute,
       EXISTS (SELECT 1 FROM unnest(p.proconfig) x WHERE x LIKE 'search_path=%') AS fixes_search_path
FROM pg_catalog.pg_proc p
JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE pg_catalog.has_function_privilege('sm_analytics', p.oid, 'EXECUTE')
  AND (n.nspname <> 'pg_catalog' OR p.prosecdef OR
       p.proname ~ '^(set_config|nextval|setval|lo_|pg_(advisory|cancel|terminate|read|write|ls|reload|rotate|promote)|dblink|http|net)')
ORDER BY n.nspname, p.proname, p.oid;

-- RLS metadata only. Do not export raw expressions: literals may contain secrets.
SELECT n.nspname, c.relname, pol.polname, pol.polcmd, pol.polpermissive,
       pol.polroles,
       md5(COALESCE(pol.polqual::text, '')) AS using_fingerprint,
       md5(COALESCE(pol.polwithcheck::text, '')) AS check_fingerprint
FROM pg_catalog.pg_policy pol
JOIN pg_catalog.pg_class c ON c.oid = pol.polrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
ORDER BY n.nspname, c.relname, pol.polname;

-- Direct function dependencies of view rewrite rules and RLS policies.
-- Inspect nested views, types/casts/operators and procedural dynamic SQL separately.
SELECT n.nspname AS view_schema, c.relname AS view_name, p.oid AS function_oid,
       pn.nspname AS function_schema, p.proname, p.prosecdef, p.provolatile
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_rewrite rw ON rw.ev_class = c.oid
JOIN pg_catalog.pg_depend d ON d.classid = 'pg_catalog.pg_rewrite'::regclass
                           AND d.objid = rw.oid
                           AND d.refclassid = 'pg_catalog.pg_proc'::regclass
JOIN pg_catalog.pg_proc p ON p.oid = d.refobjid
JOIN pg_catalog.pg_namespace pn ON pn.oid = p.pronamespace
WHERE c.relkind = 'v'
ORDER BY n.nspname, c.relname, p.oid;

SELECT n.nspname, c.relname, pol.polname, p.oid AS function_oid,
       pn.nspname AS function_schema, p.proname, p.prosecdef, p.provolatile
FROM pg_catalog.pg_policy pol
JOIN pg_catalog.pg_class c ON c.oid = pol.polrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_depend d ON d.classid = 'pg_catalog.pg_policy'::regclass
                           AND d.objid = pol.oid
                           AND d.refclassid = 'pg_catalog.pg_proc'::regclass
JOIN pg_catalog.pg_proc p ON p.oid = d.refobjid
JOIN pg_catalog.pg_namespace pn ON pn.oid = p.pronamespace
ORDER BY n.nspname, c.relname, pol.polname;

-- Default ACLs are per actual object creator; schema-specific rules do not erase
-- built-in defaults/global grants. Do not apply broad REVOKEs to production roles.
SELECT pg_catalog.pg_get_userbyid(d.defaclrole) AS creator,
       n.nspname, d.defaclobjtype,
       CASE WHEN a.grantee = 0 THEN 'PUBLIC'
            ELSE pg_catalog.pg_get_userbyid(a.grantee) END AS grantee,
       a.privilege_type, a.is_grantable
FROM pg_catalog.pg_default_acl d
LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.defaclnamespace
CROSS JOIN LATERAL pg_catalog.aclexplode(d.defaclacl) a
WHERE a.grantee = 0 OR a.grantee = 'sm_analytics'::regrole
ORDER BY creator, n.nspname, d.defaclobjtype;

-- Inventory extension/version and foreign-table existence only: do not select
-- foreign server/user-mapping options (they may contain connection credentials).
SELECT e.extname, e.extversion, n.nspname
FROM pg_catalog.pg_extension e
JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
ORDER BY e.extname;

SELECT n.nspname, c.relname, f.ftserver AS foreign_server_oid
FROM pg_catalog.pg_foreign_table f
JOIN pg_catalog.pg_class c ON c.oid = f.ftrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
ORDER BY n.nspname, c.relname;

-- Additional version-aware/operator checks still required:
-- pg_auth_members SET/INHERIT options on PG16+; MAINTAIN rights on PG17+;
-- all owned objects and grant options; unsafe predefined memberships;
-- custom column types, cast/operator/aggregate/index support functions;
-- nested view/partition dependencies; accessible large objects and schema drift.
ROLLBACK;
