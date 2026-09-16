# Structured business-data reads

This is a parent-side read/export component for isolated children. The child sends structured JSON to the authenticated control service. Only the parent process can open the dedicated PostgreSQL login. The source includes no live table inventory, project IDs, database passwords, or business records.

This directory is source for review. Provisioning and deployment happen only after the captain's manual merge and operator review. No production SQL is executed by installing these files.

## Integration

Parent control imports `data_service.py`, constructs `ReadOnlyService(private_config)`, and routes:

| Endpoint | Method | Call |
| --- | --- | --- |
| `/v1/data/schema` | POST | `service.schema(authenticated_child_id, payload)` |
| `/v1/data/query` | POST | `service.execute(authenticated_child_id, payload)` |

The HTTP service derives `child_id` from the authenticated child token. A parent request may select only one of that parent's registered children and receives exactly that child's scope. Never use a caller-provided child ID without that check. Catch `DataAccessError` and expose only its `code` and `status`; do not serialize exception chains or database messages. The module performs no caller authentication itself.

`config.example.json` documents the private configuration shape. Each child maps to one broker connection and an explicit list of relations/columns. Unknown children, tables, columns, operations and request fields fail closed. No `sql`, `rpc`, DSN, role, connection setting, DDL or DML field exists. The example deliberately has an invalid fingerprint and cannot be activated unchanged.

Install `requirements.txt` into the trusted parent Python environment (Python 3.10+). Stable `psycopg` and `psycopg-binary` are pinned to **3.3.5**, with their platform dependencies pinned. Use this driver's `prepare_threshold=None` with Supabase transaction pooling. `_connect` enforces TLS `verify-full`; provide an appropriate CA via `sslrootcert` in the private DSN when required. Verify actual pooler TLS and role routing before deployment. This source does not claim a live connection test.

The DSN file must be a regular mode-0600 file owned by root or the parent service account, under an operator-controlled directory. Its contents are a libpq connection string for the dedicated reader login. Never mount this file, the manifest, a Supabase service-role key, or the parent filesystem into a child. The configuration and module paths must not be child-writable. Protect tokens and parent transport with the existing isolated control network; HTTPS is required when that network is not a trusted local boundary.

## Operator enrollment

1. Review the exact business tables and columns, including multilingual names and actual business meaning. Exclude credential, configuration and bot-state tables by default. A name regex is a secondary check and cannot identify every secret. English and Vietnamese password names are normalized for accents, spaces and camel case. Explicit `sensitive_table_approved` or `sensitive_columns_approved` overrides belong only to the operator's documented audit, never a child request.
2. Use `provision-reader.sql` as a **template**, replacing synthetic identifiers with reviewed ones. It creates a separate login, exact column SELECT grants and SELECT RLS policy scoped to that role. Do not rotate an existing shared reader. Do not change existing production RLS state automatically. Grant `USING (true)` only where all rows are approved; otherwise retain a scoped predicate and mark visibility `policy_filtered`.
3. Run `audit-reader.sql` through a trusted operator connection. It only reads catalogs in a READ ONLY transaction and returns metadata/hashes rather than function bodies or credential options. Review effective PUBLIC privileges, every role membership, database/schema CREATE, table and column writes, PG17 MAINTAIN, sequence writes, ownership and callable routines. Runtime refuses all role memberships, admin/bypass flags, ownership/write rights, and any callable non-system SECURITY DEFINER routine. PUBLIC TEMP is permitted because the child has no SQL surface.
4. Review indirect executable dependencies before enrolling a relation: RLS predicates, views/nested views, partition and inheritance trees, casts/operators, built-in type identity, expression indexes and their support functions. Ordinary base tables with built-in scalar columns and simple SELECT policies are the initial target. Foreign tables and foreign/restricted descendants are refused. Views require explicit `allow_view: true` and manual dependency review. Materialized views, system/auth/vault/storage schemas and custom/array types are unsupported initially.
5. Prepare a private candidate config with approved names, columns, types, primary key, relation kind, RLS review and row-visibility decision. Run `python fingerprint_manifest.py --config /private/candidate.json --output /private/fingerprinted-candidate.json` under the parent account. It uses only the dedicated reader, performs runtime privilege preflight and READ ONLY catalog queries, and writes a new mode-0600 file. The generator clears `audit_ref`, so the output remains invalid until the operator finishes the dependency audit and supplies its audit reference. A fingerprint is drift detection, **not evidence that a dependency is harmless**.
6. Activate the reviewed manifest through the parent's configuration. Test role identity, schema discovery, bounded synthetic reads and scoped data access. Never probe writes against application tables. Stop/drain reads during schema/function/privilege migrations; review and regenerate fingerprints afterward. Catalog fingerprints are not a lock against a trusted administrator changing code during an in-flight request.

Do not apply broad REVOKEs to PUBLIC or alter existing creators' default privileges merely to make preflight pass. Privileges are additive: a direct REVOKE from this reader does not remove PUBLIC execution rights. If a required production privilege conflicts, stop enrollment and have the owner review a scoped remediation or use a separate sanitized data store. Read-only transactions prevent local persistent changes; they do not make arbitrary functions pure or prevent network effects. This is why the child has a fixed structured API and operators must audit indirect dependencies.

## Query examples

Schema discovery, scoped to the authenticated child:

```json
{}
```

Rows with parameterized filters and a bounded page:

```json
{"schema":"business_example","table":"orders","columns":["id","region","total"],"filters":[{"column":"region","op":"eq","value":"north"}],"limit":500}
```

For the next page, reuse the same columns/filters and copy the returned `next_after` into `after`:

```json
{"schema":"business_example","table":"orders","columns":["id","region","total"],"filters":[{"column":"region","op":"eq","value":"north"}],"limit":500,"after":{"id":12345}}
```

Keyset paging uses the verified database primary key, ascending order, and all key columns must be selected. It avoids rescanning increasingly large OFFSET ranges. Do not combine `after` with custom order or offset. A table without a usable enrolled primary key can use bounded offset/order queries; duplicate ordering values may cause unstable pages.

Fixed aggregation:

```json
{"operation":"aggregate","schema":"business_example","table":"orders","group_by":["region"],"aggregates":[{"fn":"sum","column":"total","as":"sales"},{"fn":"count","as":"orders"}],"order_by":[{"column":"sales","direction":"desc"}],"limit":100}
```

Filter operations: `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, `not_in`, `is_null`, `not_null`, `like`, `ilike`. Filters are ANDed. Null checks have no value; use `is_null`/`not_null` instead of equality to null. Values are bounded scalar JSON values, not expressions. Aggregates: `count`, `sum`, `avg`, `min`, `max`, with optional `distinct`. SUM/AVG require numeric columns; arbitrary expressions, joins, subqueries, HAVING, routines and SQL strings are unavailable.

Decimals and integers outside JavaScript's exact integer range are returned as strings to preserve precision. `has_more`, `next_after` and `next_offset` describe pagination. Each response declares its RLS visibility and one-request snapshot. **Different pages may observe different committed data.** This API does not produce a globally consistent multi-page database dump. Plan a separately reviewed snapshot export if that is required.

## Bounds and failure behavior

Each call opens a new connection with prepared statements disabled, begins `REPEATABLE READ READ ONLY`, fixes `search_path=pg_catalog`, applies statement/lock/idle timeouts, verifies the role and audited catalog fingerprint, executes a parameterized SELECT, then rolls back and closes. There is no commit path. Global per-service concurrency is bounded. Default limits are 1,000 rows, 2 MB response, 32 KB request and 15 seconds per statement; the compiler and configuration enforce hard upper bounds.

The data query uses a server-side cursor and checks a serialized row's byte size on the server before transporting it. Oversized rows/results return a sanitized error and no partial result. Limits bound output and statement duration, not total database CPU or bytes scanned: a GROUP BY over millions of records may still be expensive. Indexes, narrower predicates, workload monitoring and an operator-approved replica may be appropriate later. No replica or paid resource is created here.

The executable fingerprint is conservative and database-wide. Routine bodies/config/ACL, extension versions, policies, rewrite rules, casts/operators, type I/O and index support definitions contribute only hashes. Unrelated migrations can block reads until reviewed. Fingerprints do not detect changes outside the database, dynamically selected external targets, or configuration that is not included; dependency audit and controlled maintenance remain required.

## Child CLI

`data_client.py` reads the same `SM_INSTANCE_FILE` (default `/opt/secondmate/instance.json`) as the runtime, containing `parent_url` and `control_token_file`. It sends the child token only to the configured parent and refuses HTTP redirects. It has no PostgreSQL driver or DSN access.

```sh
python data_client.py schema
python data_client.py query --request /workspace/query.json
```

The CLI prints only successful JSON results or a generic sanitized failure. The caller chooses where to store exports; business exports must remain in approved private storage and outside the source repository.

## Tests

Offline, no driver/server/credentials required:

```sh
python -m unittest test_data_service test_data_client -v
```

Opt-in disposable PostgreSQL fixture (PG17 is the target): initialize a **local, isolated test database** named `fm_readonly_fixture_<suffix>`, then inject its test-only admin DSN as `FM_TEST_ADMIN_DSN`. Do not use a production credential. The harness rejects remote hosts and other database names, confirms the connected server identity, creates only a random synthetic schema/login in that disposable DB, and cleans those exact objects. A fresh PG15+ database's default PUBLIC schema privileges are expected. The operator creates/destroys the disposable database; the test does not manage a remote server.

```sh
python -m unittest test_postgres_integration -v
```

The fixture tests actual rows/aggregates/keyset/RLS, read-write transaction DML/DDL refusal, READ ONLY blocking a synthetic PUBLIC SECURITY DEFINER writer, broker preflight rejecting that callable writer, and schema drift. Write probes target only synthetic fixture rows and are rolled back. Without the environment variable these integration tests are explicitly skipped. Offline fakes validate request rejection, identity/preflight failures, injection escaping, multilingual sensitive names, foreign descendants, drift, precision, size bounds, rollback and sanitized errors; they do not replace a real PostgreSQL run.

## Primary references

- [PostgreSQL transaction access modes](https://www.postgresql.org/docs/current/sql-set-transaction.html) and [standby limitations, including external effects](https://www.postgresql.org/docs/current/hot-standby.html).
- [PostgreSQL grants](https://www.postgresql.org/docs/current/sql-grant.html), [function security](https://www.postgresql.org/docs/current/sql-createfunction.html), and [view security](https://www.postgresql.org/docs/current/sql-createview.html).
- [Supabase roles](https://supabase.com/docs/guides/database/postgres/roles), [database functions](https://supabase.com/docs/guides/database/functions), and [read replicas](https://supabase.com/docs/guides/platform/read-replicas).
- [Supabase change: new tables no longer automatically exposed](https://supabase.com/changelog/45329-breaking-change-tables-not-exposed-to-data-and-graphql-api-automatically). API exposure is independent of this dedicated SQL login's grants.
- [Psycopg install](https://www.psycopg.org/psycopg3/docs/basic/install.html), [transaction behavior](https://www.psycopg.org/psycopg3/docs/basic/transactions.html), and [pinned release metadata](https://pypi.org/pypi/psycopg/3.3.5/json).
