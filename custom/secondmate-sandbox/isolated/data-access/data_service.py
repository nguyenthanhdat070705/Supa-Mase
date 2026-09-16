"""Trusted parent-side, structured PostgreSQL reads. No child SQL or DSN surface.

Configuration is operator-owned; callers must authenticate child_id before calling.
Pure compiler/tests use only the standard library. psycopg is imported on connect.
"""
from __future__ import annotations

import decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import unicodedata
import uuid


class DataAccessError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def refuse(code, message, status=400):
    raise DataAccessError(code, message, status)


SAFE_TYPES = frozenset(('bool', 'int2', 'int4', 'int8', 'numeric', 'float4', 'float8',
                       'text', 'varchar', 'bpchar', 'date', 'timestamp', 'timestamptz',
                       'time', 'timetz', 'interval', 'uuid', 'json', 'jsonb', 'bytea'))
NUMERIC_TYPES = frozenset(('int2', 'int4', 'int8', 'numeric', 'float4', 'float8'))
RESTRICTED_SCHEMAS = frozenset(('auth', 'vault', 'storage', 'realtime', 'extensions',
                              'graphql', 'graphql_public', 'supabase_functions', 'information_schema'))
SECRET_COLUMN = re.compile(r'(^|_)(password|passwd|secret|secrets|token|tokens|api_key|private_key|credential|credentials|mat_khau|matkhau)(_|$)', re.I)
SENSITIVE_TABLE = re.compile(r'(^|_)(auth|credential|credentials|signin|sign_in|login|password|config|configuration|systemconfig|settings|state)(_|$)', re.I)


def normalized_name(value):
    value = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', value)
    value = ''.join(c for c in unicodedata.normalize('NFKD', value) if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '_', value.casefold().replace('đ', 'd')).strip('_')
DEFAULT_LIMITS = {'max_rows': 1000, 'max_bytes': 2_000_000, 'max_request_bytes': 32_768,
                  'statement_timeout_ms': 15_000, 'lock_timeout_ms': 1000,
                  'idle_timeout_ms': 15_000, 'max_offset': 1_000_000,
                  'max_filters': 20, 'max_in_values': 100, 'max_concurrent': 2}


def identifier(value):
    if (not isinstance(value, str) or not value or len(value.encode('utf-8')) > 63
            or any(ord(c) < 32 for c in value)):
        refuse('invalid_identifier', 'Identifier is invalid.')
    return '"' + value.replace('"', '""') + '"'


def exact_keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        refuse('invalid_request', 'Unknown, missing, or unsupported request fields.')


def bounded_int(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        refuse('invalid_request', label + ' is outside its allowed bounds.')
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def catalog_fingerprint(metadata):
    """Use only the metadata returned by inspect_table; never function source."""
    return hashlib.sha256(canonical(metadata).encode()).hexdigest()


ROLE_PREFLIGHT_SQL = """/* fm-data:role-preflight */
SELECT current_user AS role, session_user AS session_role, current_database() AS database,
       current_setting('transaction_read_only') AS read_only,
       pg_catalog.pg_is_in_recovery() AS in_recovery,
       current_timestamp::text AS snapshot_time,
       pg_catalog.pg_current_snapshot()::text AS snapshot,
       current_setting('server_version_num')::int AS server_version_num,
       r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication, r.rolbypassrls,
       EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid) AS membership,
       pg_catalog.has_database_privilege(r.oid, current_database(), 'CREATE') AS database_create,
       EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n
               WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
                 AND (n.nspowner = r.oid OR pg_catalog.has_schema_privilege(r.oid,n.oid,'CREATE'))) AS schema_control,
       EXISTS (SELECT 1 FROM pg_catalog.pg_class c
               JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
                 AND c.relkind IN ('r','p','v','m','f')
                 AND (c.relowner = r.oid OR
                      pg_catalog.has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR
                      CASE WHEN current_setting('server_version_num')::int >= 170000
                           THEN pg_catalog.has_table_privilege(r.oid,c.oid,'MAINTAIN') ELSE false END OR
                      pg_catalog.has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))) AS table_write,
       EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.relkind='S'
                 AND (c.relowner=r.oid OR pg_catalog.has_sequence_privilege(r.oid,c.oid,'USAGE,UPDATE'))) AS sequence_write,
       EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.proowner=r.oid) AS owns_routine,
       EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
               WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND p.prosecdef
                 AND pg_catalog.has_function_privilege(r.oid,p.oid,'EXECUTE')) AS executable_definer
FROM pg_catalog.pg_roles r WHERE r.rolname=current_user
"""

# A conservative database-wide executable stamp catches changed routine bodies,
# extension versions, operators/casts, and indexes without disclosing their source.
# Procedural dynamic dependencies still require manual audit before enrollment.
EXECUTABLE_STAMP_SQL = """/* fm-data:executable-stamp */
SELECT pg_catalog.md5(COALESCE((
  SELECT string_agg(p.oid::text || ':' || p.proowner::text || ':' || p.prosecdef::text || ':' ||
       p.provolatile::text || ':' || p.prolang::text || ':' || COALESCE(p.proacl::text,'') || ':' ||
       COALESCE(p.proconfig::text,'') || ':' ||
       pg_catalog.md5(p.prosrc || COALESCE(p.probin,'')), E'\\n' ORDER BY p.oid)
  FROM pg_catalog.pg_proc p), '') || '|' || COALESCE((
  SELECT string_agg(e.extname || ':' || e.extversion, ',' ORDER BY e.extname)
  FROM pg_catalog.pg_extension e), '') || '|' || COALESCE((
  SELECT string_agg(o.oid::text || ':' || o.oprcode::text, ',' ORDER BY o.oid)
  FROM pg_catalog.pg_operator o), '') || '|' || COALESCE((
  SELECT string_agg(c.oid::text || ':' || c.castfunc::text || ':' || c.castmethod::text, ',' ORDER BY c.oid)
  FROM pg_catalog.pg_cast c), '') || '|' || COALESCE((
  SELECT string_agg(i.indexrelid::text || ':' || i.indclass::text || ':' ||
     COALESCE(i.indexprs::text,'') || ':' || COALESCE(i.indpred::text,''), ',' ORDER BY i.indexrelid)
  FROM pg_catalog.pg_index i), '') || '|' || COALESCE((
  SELECT string_agg(w.oid::text || ':' || w.ev_action::text, ',' ORDER BY w.oid)
  FROM pg_catalog.pg_rewrite w), '') || '|' || COALESCE((
  SELECT string_agg(p.oid::text || ':' || p.polroles::text || ':' ||
       COALESCE(p.polqual::text,'') || ':' || COALESCE(p.polwithcheck::text,''), ',' ORDER BY p.oid)
  FROM pg_catalog.pg_policy p), '') || '|' || COALESCE((
  SELECT string_agg(t.oid::text || ':' || t.typinput::text || ':' || t.typoutput::text || ':' ||
       t.typreceive::text || ':' || t.typsend::text, ',' ORDER BY t.oid)
  FROM pg_catalog.pg_type t), '') || '|' || COALESCE((
  SELECT string_agg(a.oid::text || ':' || a.amproc::text, ',' ORDER BY a.oid)
  FROM pg_catalog.pg_amproc a), '') || '|' || COALESCE((
  SELECT string_agg(a.oid::text || ':' || a.amopopr::text, ',' ORDER BY a.oid)
  FROM pg_catalog.pg_amop a), '')) AS executable_stamp
"""

TABLE_METADATA_SQL = """/* fm-data:table-metadata */
SELECT c.oid::bigint AS oid, c.relkind AS relation_kind,
       c.relrowsecurity AS rls_enabled, c.relforcerowsecurity AS force_rls,
       c.relowner::bigint AS owner_oid,
       COALESCE((SELECT jsonb_agg(jsonb_build_object(
           'name', a.attname, 'type', t.typname, 'type_schema', tn.nspname,
           'not_null', a.attnotnull, 'position', a.attnum) ORDER BY a.attnum)
         FROM pg_catalog.pg_attribute a
         JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
         JOIN pg_catalog.pg_namespace tn ON tn.oid=t.typnamespace
         WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped), '[]'::jsonb) AS columns,
       COALESCE((SELECT jsonb_agg(jsonb_build_object(
           'name', p.polname, 'command', p.polcmd, 'permissive', p.polpermissive,
           'roles', p.polroles::text, 'using_hash', md5(COALESCE(p.polqual::text,'')),
           'check_hash', md5(COALESCE(p.polwithcheck::text,''))) ORDER BY p.polname)
         FROM pg_catalog.pg_policy p WHERE p.polrelid=c.oid), '[]'::jsonb) AS policies,
       COALESCE((SELECT jsonb_agg(a.attname ORDER BY k.position)
         FROM pg_catalog.pg_index ix
         CROSS JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY k(attnum,position)
         JOIN pg_catalog.pg_attribute a ON a.attrelid=ix.indrelid AND a.attnum=k.attnum
         WHERE ix.indrelid=c.oid AND ix.indisprimary AND k.position<=ix.indnkeyatts), '[]'::jsonb) AS primary_key,
       COALESCE((WITH RECURSIVE descendants(oid) AS (
           SELECT i.inhrelid FROM pg_catalog.pg_inherits i WHERE i.inhparent=c.oid
           UNION SELECT i.inhrelid FROM pg_catalog.pg_inherits i JOIN descendants d ON i.inhparent=d.oid)
           SELECT jsonb_agg(jsonb_build_object('child_oid',cc.oid::bigint,'relation_kind',cc.relkind,
                   'schema',nn.nspname) ORDER BY cc.oid)
           FROM descendants d JOIN pg_catalog.pg_class cc ON cc.oid=d.oid
           JOIN pg_catalog.pg_namespace nn ON nn.oid=cc.relnamespace), '[]'::jsonb) AS children,
       COALESCE((SELECT md5(string_agg(w.ev_action::text, ',' ORDER BY w.oid))
           FROM pg_catalog.pg_rewrite w WHERE w.ev_class=c.oid), '') AS rewrite_hash
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname=%s AND c.relname=%s
"""


def inspect_table(cursor, schema, table, executable_stamp):
    """Operator helper; safe catalog metadata, not enrollment/purity approval."""
    identifier(schema)
    identifier(table)
    cursor.execute(TABLE_METADATA_SQL, (schema, table))
    row = cursor.fetchone()
    if not row:
        refuse('catalog_changed', 'An approved relation is absent.', 503)
    result = dict(row)
    for field in ('columns', 'policies', 'children', 'primary_key'):
        if isinstance(result[field], str):
            result[field] = json.loads(result[field])
    result['executable_stamp'] = executable_stamp
    return result


def validate_table(entry):
    exact_keys(entry, {'schema','table','columns','catalog_fingerprint','audit_ref',
                      'relation_kind','rls_reviewed','row_visibility','primary_key',
                      'allow_view','sensitive_columns_approved','sensitive_table_approved'},
               {'schema','table','columns','catalog_fingerprint','audit_ref','relation_kind','rls_reviewed','row_visibility'})
    for field in ('schema', 'table'):
        identifier(entry[field])
    if entry['schema'].startswith('pg_') or entry['schema'] in RESTRICTED_SCHEMAS:
        refuse('invalid_config', 'System, credential and external-service schemas are unsupported.', 503)
    if SENSITIVE_TABLE.search(normalized_name(entry['table'])) and entry.get('sensitive_table_approved') is not True:
        refuse('invalid_config', 'Credential, configuration and state tables require explicit operator approval.', 503)
    if entry['relation_kind'] not in ('r', 'p', 'v'):
        refuse('invalid_config', 'Only audited base tables and explicitly audited views are supported.', 503)
    if entry['relation_kind'] == 'v' and entry.get('allow_view') is not True:
        refuse('invalid_config', 'Views require explicit operator audit enrollment.', 503)
    if (entry['rls_reviewed'] is not True or not isinstance(entry['audit_ref'], str) or not entry['audit_ref'].strip()
            or entry['row_visibility'] not in ('policy_filtered', 'all_authorized_rows_audited')):
        refuse('invalid_config', 'A relation needs documented dependency and row-visibility review.', 503)
    if not isinstance(entry['catalog_fingerprint'], str) or not re.fullmatch('[0-9a-f]{64}', entry['catalog_fingerprint']):
        refuse('invalid_config', 'A reviewed catalog fingerprint is required.', 503)
    columns = entry['columns']
    if not isinstance(columns, dict) or not 1 <= len(columns) <= 300:
        refuse('invalid_config', 'An explicit bounded column manifest is required.', 503)
    sensitive = entry.get('sensitive_columns_approved', [])
    if not isinstance(sensitive, list) or any(x not in columns for x in sensitive):
        refuse('invalid_config', 'Invalid sensitive-column approval list.', 503)
    for name, kind in columns.items():
        identifier(name)
        if kind not in SAFE_TYPES:
            refuse('invalid_config', 'Only reviewed built-in scalar data types are supported.', 503)
        if SECRET_COLUMN.search(normalized_name(name)) and name not in sensitive:
            refuse('invalid_config', 'Sensitive-looking columns require explicit operator approval.', 503)
    primary = entry.get('primary_key', [])
    if not isinstance(primary, list) or any(x not in columns for x in primary) or len(primary) != len(set(primary)):
        refuse('invalid_config', 'Invalid stable ordering configuration.', 503)


class ReadOnlyService:
    def __init__(self, config, connection_factory=None):
        # Deep-copy trusted config so callers cannot mutate enrollment mid-request.
        self.config = json.loads(json.dumps(config))
        exact_keys(self.config, {'version','connections','children','limits'}, {'version','connections','children'})
        if self.config['version'] != 1:
            refuse('invalid_config', 'Unsupported data service config version.', 503)
        self.limits = dict(DEFAULT_LIMITS)
        limits = self.config.get('limits', {})
        exact_keys(limits, DEFAULT_LIMITS)
        for name, value in limits.items():
            bounded_int(value, 1, {'max_rows':10000,'max_bytes':16_000_000,'max_request_bytes':100_000,
                'statement_timeout_ms':120000,'lock_timeout_ms':10000,'idle_timeout_ms':120000,
                'max_offset':100_000_000,'max_filters':100,'max_in_values':1000,'max_concurrent':16}[name], name)
            self.limits[name] = value
        if not isinstance(self.config['connections'], dict) or not isinstance(self.config['children'], dict):
            refuse('invalid_config', 'Connection and child registries must be objects.', 503)
        self.tables = {}
        for child, scope in self.config['children'].items():
            exact_keys(scope, {'connection','tables'}, {'connection','tables'})
            if scope['connection'] not in self.config['connections'] or not isinstance(scope['tables'], list):
                refuse('invalid_config', 'Invalid child connection or relation registry.', 503)
            registry = {}
            for entry in scope['tables']:
                validate_table(entry)
                key = (entry['schema'], entry['table'])
                if key in registry:
                    refuse('invalid_config', 'Duplicate enrolled relation.', 503)
                registry[key] = entry
            self.tables[child] = registry
        for connection in self.config['connections'].values():
            exact_keys(connection, {'dsn_file','expected_role','database','require_recovery'},
                       {'dsn_file','expected_role','database','require_recovery'})
            if (not isinstance(connection['dsn_file'], str) or not Path(connection['dsn_file']).is_absolute()
                    or not isinstance(connection['expected_role'], str) or not connection['expected_role']
                    or not isinstance(connection['database'], str) or not connection['database']
                    or type(connection['require_recovery']) is not bool):
                refuse('invalid_config', 'Invalid trusted connection configuration.', 503)
        self.connection_factory = connection_factory or self._connect
        self.slots = threading.BoundedSemaphore(self.limits['max_concurrent'])

    def _scope(self, child_id):
        if not isinstance(child_id, str) or child_id not in self.tables:
            refuse('child_not_enrolled', 'This child has no approved data scope.', 403)
        return self.config['connections'][self.config['children'][child_id]['connection']]

    @staticmethod
    def _connect(config):
        try:
            import psycopg
            from psycopg.rows import dict_row
            path = Path(config['dsn_file'])
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                refuse('credential_permissions', 'Broker credential file permissions are unsafe.', 503)
            if hasattr(os, 'geteuid') and info.st_uid not in (0, os.geteuid()):
                refuse('credential_permissions', 'Broker credential file owner is unsafe.', 503)
            # sslrootcert may be specified in the operator-owned DSN. No child
            # field reaches connect(). Binary wheels carry libpq; pooling is off.
            return psycopg.connect(path.read_text().strip(), autocommit=True,
                prepare_threshold=None, row_factory=dict_row, connect_timeout=5,
                sslmode='verify-full', application_name='firstmate-readonly-broker')
        except DataAccessError:
            raise
        except Exception:
            refuse('database_unavailable', 'The approved database connection is unavailable.', 503)

    def _request(self, child_id, request):
        self._scope(child_id)
        try:
            if len(canonical(request).encode()) > self.limits['max_request_bytes']:
                refuse('request_too_large', 'Data request exceeds the configured size bound.', 413)
        except (TypeError, ValueError):
            refuse('invalid_request', 'Request must contain finite JSON values.')

    def _table(self, child_id, request):
        key = (request.get('schema'), request.get('table'))
        if not all(isinstance(x, str) for x in key) or key not in self.tables[child_id]:
            refuse('relation_not_enrolled', 'Relation is not approved for this child.', 403)
        return self.tables[child_id][key]

    def _value(self, value):
        if value is None or type(value) in (bool, int):
            return value
        if type(value) is float and math.isfinite(value):
            return value
        if isinstance(value, str) and len(value.encode()) <= 8192:
            return value
        refuse('invalid_filter', 'Filter values must be bounded JSON scalars.')

    def compile(self, child_id, request):
        """Pure structured compiler. Returned SQL is never accepted from a caller."""
        self._request(child_id, request)
        exact_keys(request, {'operation','schema','table','columns','filters','order_by','limit','offset','after','aggregates','group_by'},
                   {'schema','table'})
        entry = self._table(child_id, request)
        operation = request.get('operation', 'rows')
        if operation not in ('rows', 'aggregate'):
            refuse('unsupported_operation', 'Only rows and aggregate operations are available.')
        columns = entry['columns']
        params, clauses = [], []
        filters = request.get('filters', [])
        if not isinstance(filters, list) or len(filters) > self.limits['max_filters']:
            refuse('invalid_filter', 'Too many or malformed filters.')
        binary = {'eq':'=', 'ne':'<>', 'lt':'<', 'lte':'<=', 'gt':'>', 'gte':'>='}
        for item in filters:
            exact_keys(item, {'column','op','value'}, {'column','op'})
            name, op = item['column'], item['op']
            if not isinstance(op,str):
                refuse('invalid_filter', 'Filter operation must be a supported name.')
            if not isinstance(name, str) or name not in columns:
                refuse('column_not_enrolled', 'Filter column is not approved.', 403)
            col = identifier(name)
            cast = '::pg_catalog.' + identifier(columns[name])
            if op in ('is_null','not_null'):
                if 'value' in item:
                    refuse('invalid_filter', 'Null predicates do not accept a value.')
                clauses.append(col + (' IS NULL' if op == 'is_null' else ' IS NOT NULL'))
            elif op in binary or op in ('like','ilike'):
                if 'value' not in item or item['value'] is None:
                    refuse('invalid_filter', 'Use an explicit null predicate for null values.')
                if op in ('like','ilike'):
                    if columns[name] not in ('text','varchar','bpchar') or not isinstance(item['value'], str):
                        refuse('invalid_filter', 'Pattern filters require text columns and values.')
                    operator = '~~' if op == 'like' else '~~*'
                else:
                    operator = binary[op]
                clauses.append(col + ' OPERATOR(pg_catalog.' + operator + ') %s' + cast)
                params.append(self._value(item['value']))
            elif op in ('in','not_in'):
                values = item.get('value')
                if not isinstance(values, list) or not 1 <= len(values) <= self.limits['max_in_values'] or any(x is None for x in values):
                    refuse('invalid_filter', 'Membership filters require a bounded nonempty scalar list without null.')
                # OR of explicitly pg_catalog equality avoids untrusted overloads.
                comparisons = []
                for value in values:
                    params.append(self._value(value))
                    comparisons.append(col + ' OPERATOR(pg_catalog.=) %s' + cast)
                clauses.append(('NOT ' if op == 'not_in' else '') + '(' + ' OR '.join(comparisons) + ')')
            else:
                refuse('invalid_filter', 'Unsupported filter operation.')
        primary = entry.get('primary_key', [])
        after = request.get('after')
        if 'after' in request:
            if (operation != 'rows' or not primary or not isinstance(after,dict) or set(after)!=set(primary)
                    or request.get('offset',0) != 0 or 'order_by' in request or any(after[x] is None for x in primary)):
                refuse('invalid_cursor', 'Keyset paging requires the complete configured key, default ordering and no offset.')
            branches=[]
            for index,key in enumerate(primary):
                pieces=[]
                for prefix in primary[:index]:
                    pieces.append(identifier(prefix)+' OPERATOR(pg_catalog.=) %s::pg_catalog.'+identifier(columns[prefix]))
                    params.append(self._value(after[prefix]))
                pieces.append(identifier(key)+' OPERATOR(pg_catalog.>) %s::pg_catalog.'+identifier(columns[key]))
                params.append(self._value(after[key]))
                branches.append('('+' AND '.join(pieces)+')')
            clauses.append('('+' OR '.join(branches)+')')
        output, expressions = [], []
        if operation == 'rows':
            if 'aggregates' in request or 'group_by' in request:
                refuse('invalid_request', 'Row requests cannot contain aggregation fields.')
            requested = request.get('columns', list(columns))
            if (not isinstance(requested, list) or not requested or any(not isinstance(x,str) or x not in columns for x in requested)
                    or len(requested) != len(set(requested))):
                refuse('column_not_enrolled', 'Select columns must be distinct approved names.', 403)
            output, expressions = requested, [identifier(x) for x in requested]
            groups = []
        else:
            if 'columns' in request:
                refuse('invalid_request', 'Aggregate requests use group_by and aggregates, not columns.')
            groups = request.get('group_by', [])
            if (not isinstance(groups,list) or len(groups)>16 or any(not isinstance(x,str) or x not in columns for x in groups)
                    or len(groups) != len(set(groups))):
                refuse('invalid_request', 'Invalid grouping columns.')
            output, expressions = list(groups), [identifier(x) for x in groups]
            aggs = request.get('aggregates')
            if not isinstance(aggs,list) or not 1 <= len(aggs) <= 16:
                refuse('invalid_request', 'Provide 1..16 structured aggregates.')
            for agg in aggs:
                exact_keys(agg, {'fn','column','as','distinct'}, {'fn','as'})
                fn, name, alias = agg['fn'], agg.get('column'), agg['as']
                if fn not in ('count','sum','avg','min','max'):
                    refuse('invalid_aggregate', 'Unsupported aggregation function.')
                identifier(alias)
                if alias in output:
                    refuse('invalid_aggregate', 'Output aliases must be distinct.')
                distinct = agg.get('distinct',False)
                if type(distinct) is not bool:
                    refuse('invalid_aggregate', 'distinct must be a boolean.')
                if name is None and fn == 'count' and not distinct:
                    arg = '*'
                else:
                    if not isinstance(name,str) or name not in columns:
                        refuse('column_not_enrolled', 'Aggregation column is not approved.',403)
                    if fn in ('sum','avg') and columns[name] not in NUMERIC_TYPES:
                        refuse('invalid_aggregate', 'sum and avg require numeric columns.')
                    if fn in ('min','max') and columns[name] in ('json','jsonb','bytea','bool'):
                        refuse('invalid_aggregate', 'Unsupported min/max column type.')
                    arg = ('DISTINCT ' if distinct else '') + identifier(name)
                expressions.append('pg_catalog.' + fn + '(' + arg + ') AS ' + identifier(alias))
                output.append(alias)
        query = 'SELECT ' + ', '.join(expressions) + ' FROM ' + identifier(entry['schema']) + '.' + identifier(entry['table'])
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        if groups:
            query += ' GROUP BY ' + ', '.join(identifier(x) for x in groups)
        default_order = [{'column':x,'direction':'asc'} for x in (entry.get('primary_key',[]) if operation=='rows' else groups)]
        order = request.get('order_by', default_order)
        if not isinstance(order,list) or len(order)>16:
            refuse('invalid_request', 'Invalid order_by.')
        ordered = []
        for item in order:
            exact_keys(item, {'column','direction','nulls'}, {'column'})
            allowed_order = columns if operation == 'rows' else output
            if not isinstance(item['column'],str) or item['column'] not in allowed_order:
                refuse('column_not_enrolled', 'Ordering column is not available.',403)
            direction, nulls = item.get('direction','asc'), item.get('nulls','last')
            if direction not in ('asc','desc') or nulls not in ('first','last'):
                refuse('invalid_request', 'Invalid ordering direction or null placement.')
            ordered.append(identifier(item['column'])+' '+direction.upper()+' NULLS '+nulls.upper())
        if ordered:
            query += ' ORDER BY ' + ', '.join(ordered)
        limit = bounded_int(request.get('limit',min(100,self.limits['max_rows'])),1,self.limits['max_rows'],'limit')
        offset = bounded_int(request.get('offset',0),0,self.limits['max_offset'],'offset')
        if offset and not ordered:
            refuse('unstable_pagination', 'Pagination requires an explicit stable ordering.')
        query += ' LIMIT %s OFFSET %s'
        params.extend((limit+1,offset))
        keyset_eligible = operation=='rows' and bool(primary) and all(x in output for x in primary) and order==default_order
        if 'after' in request and not keyset_eligible:
            refuse('invalid_cursor','Include every configured key column when using keyset paging.')
        return {'sql':query,'params':params,'entry':entry,'columns':output,'limit':limit,'offset':offset,
                'operation':operation,'keyset_eligible':keyset_eligible}

    def _begin(self, conn, config):
        cursor = conn.cursor()
        cursor.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        cursor.execute("SET LOCAL search_path = pg_catalog")
        for name, key in (('statement_timeout','statement_timeout_ms'),('lock_timeout','lock_timeout_ms'),
                          ('idle_in_transaction_session_timeout','idle_timeout_ms')):
            # Names fixed here, values are validated operator integers, never SQL input.
            cursor.execute("SELECT pg_catalog.set_config(%s,%s,true)",(name,str(self.limits[key])+'ms'))
        cursor.execute(ROLE_PREFLIGHT_SQL)
        role = cursor.fetchone()
        if (not role or role['role'] != config['expected_role'] or role['session_role'] != config['expected_role']
                or role['database'] != config['database'] or role['read_only'] != 'on'
                or any(role[key] for key in ('rolsuper','rolcreatedb','rolcreaterole','rolreplication','rolbypassrls',
                                             'membership','database_create','schema_control','table_write','sequence_write','owns_routine','executable_definer'))
                or config['require_recovery'] and not role['in_recovery']):
            refuse('unsafe_database_role', 'Database role, permissions or transaction state failed preflight.',503)
        cursor.execute(EXECUTABLE_STAMP_SQL)
        stamp = cursor.fetchone()['executable_stamp']
        return cursor, role, stamp

    def _verify(self, cursor, entry, stamp):
        metadata = inspect_table(cursor,entry['schema'],entry['table'],stamp)
        if metadata['relation_kind'] != entry['relation_kind'] or catalog_fingerprint(metadata) != entry['catalog_fingerprint']:
            refuse('catalog_changed', 'Approved schema or executable dependencies changed; operator review is required.',503)
        if any(x['relation_kind'] not in ('r','p') or x['schema'].startswith('pg_') or x['schema'] in RESTRICTED_SCHEMAS
               for x in metadata['children']):
            refuse('unsafe_relation','Foreign or restricted descendant relations are unsupported.',503)
        actual = {x['name']:x for x in metadata['columns']}
        for name, kind in entry['columns'].items():
            if name not in actual or actual[name]['type_schema'] != 'pg_catalog' or actual[name]['type'] != kind:
                refuse('catalog_changed', 'An approved column type changed; operator review is required.',503)
        if entry.get('primary_key') and metadata['primary_key'] != entry['primary_key']:
            refuse('catalog_changed', 'Keyset ordering must match the verified database primary key.',503)
        return metadata

    def _run(self, child_id, task):
        config = self._scope(child_id)
        if not self.slots.acquire(blocking=False):
            refuse('busy','The read service is at its concurrency limit.',429)
        conn = None
        try:
            conn = self.connection_factory(config)
            cursor, role, stamp = self._begin(conn,config)
            return task(conn,cursor,role,stamp)
        except DataAccessError:
            raise
        except Exception:
            raise DataAccessError('read_failed','The bounded read failed; no write or partial result was returned.',503) from None
        finally:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            self.slots.release()

    def schema(self, child_id, request=None):
        request = {} if request is None else request
        self._request(child_id,request)
        exact_keys(request,{'schema','table'})
        if 'table' in request and 'schema' not in request:
            refuse('invalid_request','A table filter also needs its schema.')
        entries = [e for e in self.tables[child_id].values()
                   if ('schema' not in request or e['schema']==request['schema'])
                   and ('table' not in request or e['table']==request['table'])]
        if request and not entries:
            refuse('relation_not_enrolled','No matching relation is approved for this child.',403)
        def task(conn,cursor,role,stamp):
            result=[]
            for entry in entries:
                metadata=self._verify(cursor,entry,stamp)
                result.append({'schema':entry['schema'],'table':entry['table'],
                    'columns':[{'name':name,'type':kind} for name,kind in entry['columns'].items()],
                    'primary_key':entry.get('primary_key',[]),'rls_enabled':metadata['rls_enabled'],
                    'row_visibility':entry['row_visibility']})
            response={'tables':result,'snapshot_time':role['snapshot_time'],'read_only':True,
                      'limits':{k:self.limits[k] for k in ('max_rows','max_bytes','max_offset')}}
            if len(canonical(response).encode())>self.limits['max_bytes']:
                refuse('result_too_large','Schema response exceeds its bound; select one table.',413)
            return response
        return self._run(child_id,task)

    def execute(self, child_id, request):
        compiled=self.compile(child_id,request)
        def task(conn,cursor,role,stamp):
            self._verify(cursor,compiled['entry'],stamp)
            # Bound a single cell/row before it crosses the connection. JSON is
            # built on the server from reviewed built-in types; never call RPCs.
            sql=("/* fm-data:rows */ SELECT CASE WHEN pg_catalog.octet_length(payload)<=%s THEN payload ELSE NULL END AS payload, "
                 "pg_catalog.octet_length(payload) AS payload_bytes FROM (SELECT pg_catalog.row_to_json(__fm_row)::text AS payload FROM ("
                 +compiled['sql']+") AS __fm_row) AS __fm_encoded")
            data_cursor=conn.cursor(name='fm_read_'+uuid.uuid4().hex)
            data_cursor.execute(sql,[self.limits['max_bytes'],*compiled['params']])
            rows=[]
            used=0
            has_more=False
            while True:
                row=data_cursor.fetchone()
                if row is None:
                    break
                if len(rows)==compiled['limit']:
                    has_more=True
                    break
                if row['payload'] is None or row['payload_bytes']>self.limits['max_bytes']:
                    refuse('result_too_large','A result row exceeds the byte bound; narrow columns.',413)
                value=json.loads(row['payload'],parse_float=decimal.Decimal)
                value=_json_values(value)
                used+=len(canonical(value).encode())+1
                if used>self.limits['max_bytes']:
                    refuse('result_too_large','Result exceeds the byte bound; request fewer rows or columns.',413)
                rows.append(value)
            data_cursor.close()
            result={'schema':compiled['entry']['schema'],'table':compiled['entry']['table'],
                    'columns':compiled['columns'],'rows':rows,'row_count':len(rows),'has_more':has_more,
                    'next_offset':compiled['offset']+len(rows) if has_more and 'after' not in request else None,
                    'next_after':{k:rows[-1][k] for k in compiled['entry']['primary_key']}
                       if has_more and rows and compiled['keyset_eligible'] else None,
                    'snapshot_time':role['snapshot_time'],'snapshot':role['snapshot'],
                    'read_only':True,'row_visibility':compiled['entry']['row_visibility'],
                    'consistency':'one_request_snapshot; later pages may see newer committed data'}
            if len(canonical(result).encode())>self.limits['max_bytes']:
                refuse('result_too_large','Result including metadata exceeds its byte bound.',413)
            return result
        return self._run(child_id,task)


def _json_values(value):
    if isinstance(value,decimal.Decimal):
        return str(value)
    if type(value) is int and abs(value)>9_007_199_254_740_991:
        return str(value)
    if isinstance(value,list):
        return [_json_values(x) for x in value]
    if isinstance(value,dict):
        return {k:_json_values(v) for k,v in value.items()}
    return value
