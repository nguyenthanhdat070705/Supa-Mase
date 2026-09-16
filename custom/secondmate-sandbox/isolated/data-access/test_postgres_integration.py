"""Opt-in destructive tests ONLY in an explicitly named disposable local database.

FM_TEST_ADMIN_DSN must connect to localhost/socket and dbname fm_readonly_fixture_*.
Never point this fixture at Supabase or an application database.
"""
import copy
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from data_service import (DataAccessError, ReadOnlyService, EXECUTABLE_STAMP_SQL,
                          catalog_fingerprint, inspect_table)


@unittest.skipUnless(os.environ.get('FM_TEST_ADMIN_DSN'), 'local disposable PostgreSQL fixture not enabled')
class DisposablePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        from psycopg.rows import dict_row
        from psycopg import sql
        cls.psycopg,cls.sql,cls.dict_row=psycopg,sql,staticmethod(dict_row)
        cls.dsn=os.environ['FM_TEST_ADMIN_DSN']
        opts=conninfo_to_dict(cls.dsn)
        host=opts.get('host','')
        if (not opts.get('dbname','').startswith('fm_readonly_fixture_')
                or host not in ('localhost','127.0.0.1','::1') and not host.startswith('/')
                or 'hostaddr' in opts and opts['hostaddr'] not in ('127.0.0.1','::1')):
            raise RuntimeError('Refusing fixture: requires explicit local host/socket and disposable database name.')
        cls.admin=psycopg.connect(cls.dsn,autocommit=True,row_factory=dict_row)
        fact=cls.admin.execute('SELECT current_database() AS db, pg_catalog.host(inet_server_addr()) AS ip').fetchone()
        if not fact['db'].startswith('fm_readonly_fixture_') or fact['ip'] not in (None,'127.0.0.1','::1'):
            cls.admin.close()
            raise RuntimeError('Refusing fixture: connected server identity is not an isolated local database.')
        suffix=uuid.uuid4().hex[:12]
        cls.schema='fixture_'+suffix
        cls.reader='fixture_reader_'+suffix
        cls.password='disposable-test-only-'+suffix
        ident=sql.Identifier
        cls.admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}').format(ident(cls.reader),sql.Literal(cls.password)))
        cls.admin.execute(sql.SQL('CREATE SCHEMA {}').format(ident(cls.schema)))
        cls.admin.execute(sql.SQL('CREATE TABLE {}.orders (id bigint PRIMARY KEY, region text, total numeric)').format(ident(cls.schema)))
        cls.admin.execute(sql.SQL("INSERT INTO {}.orders VALUES (1,'north',12.30),(2,'north',7.70),(3,'south',5.00)").format(ident(cls.schema)))
        cls.admin.execute(sql.SQL('ALTER TABLE {}.orders ENABLE ROW LEVEL SECURITY').format(ident(cls.schema)))
        cls.admin.execute(sql.SQL('CREATE POLICY fixture_reader_select ON {}.orders FOR SELECT TO {} USING (true)').format(ident(cls.schema),ident(cls.reader)))
        cls.admin.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(ident(cls.schema),ident(cls.reader)))
        cls.admin.execute(sql.SQL('GRANT SELECT ON {}.orders TO {}').format(ident(cls.schema),ident(cls.reader)))
        cls.reader_opts={**opts,'user':cls.reader,'password':cls.password}
        # PG15+ fresh database has no PUBLIC CREATE on public. Older/custom fixture
        # databases must be initialized appropriately by their test operator.

    @classmethod
    def tearDownClass(cls):
        cls.admin.execute(cls.sql.SQL('DROP SCHEMA {} CASCADE').format(cls.sql.Identifier(cls.schema)))
        cls.admin.execute(cls.sql.SQL('DROP ROLE {}').format(cls.sql.Identifier(cls.reader)))
        cls.admin.close()

    def connect_reader(self,ignored=None):
        return self.psycopg.connect(**self.reader_opts,autocommit=True,prepare_threshold=None,row_factory=self.dict_row)

    def service(self):
        with self.connect_reader() as conn:
            conn.execute('BEGIN READ ONLY')
            stamp=conn.execute(EXECUTABLE_STAMP_SQL).fetchone()['executable_stamp']
            metadata=inspect_table(conn.cursor(),self.schema,'orders',stamp)
            conn.rollback()
        cfg={'version':1,'connections':{'fixture':{
            'dsn_file':str(Path(tempfile.gettempdir())/'unused-fixture.dsn'),
            'expected_role':self.reader,'database':self.reader_opts['dbname'],'require_recovery':False}},
            'children':{'fixture-child':{'connection':'fixture','tables':[{
                'schema':self.schema,'table':'orders','columns':{'id':'int8','region':'text','total':'numeric'},
                'primary_key':['id'],'relation_kind':'r','rls_reviewed':True,'row_visibility':'all_authorized_rows_audited',
                'audit_ref':'synthetic-local-fixture','catalog_fingerprint':catalog_fingerprint(metadata)}]}},
            'limits':{'max_rows':10,'max_bytes':10000}}
        return ReadOnlyService(cfg,connection_factory=self.connect_reader)

    def test_rows_aggregates_keyset_and_metadata_real_database(self):
        service=self.service()
        req={'schema':self.schema,'table':'orders','limit':2}
        page=service.execute('fixture-child',req)
        self.assertEqual([x['id'] for x in page['rows']],[1,2])
        self.assertEqual(page['rows'][0]['total'],'12.30')
        self.assertTrue(page['read_only'])
        page2=service.execute('fixture-child',{**req,'after':page['next_after']})
        self.assertEqual([x['id'] for x in page2['rows']],[3])
        self.assertFalse(page2['has_more'])
        grouped=service.execute('fixture-child',{'schema':self.schema,'table':'orders','operation':'aggregate',
            'group_by':['region'],'aggregates':[{'fn':'sum','column':'total','as':'sales'}]})
        self.assertEqual(grouped['rows'][0],{'region':'north','sales':'20.00'})
        self.assertEqual(service.schema('fixture-child')['tables'][0]['primary_key'],['id'])

    def test_role_denies_dml_and_ddl_even_in_read_write_transaction(self):
        s=self.sql.Identifier(self.schema)
        statements=[self.sql.SQL('INSERT INTO {}.orders VALUES (4,\'blocked\',0)').format(s),
                    self.sql.SQL('UPDATE {}.orders SET total=0').format(s),
                    self.sql.SQL('DELETE FROM {}.orders').format(s),
                    self.sql.SQL('TRUNCATE {}.orders').format(s),
                    self.sql.SQL('ALTER TABLE {}.orders ADD COLUMN blocked int').format(s),
                    self.sql.SQL('CREATE TABLE {}.blocked(id int)').format(s),
                    self.sql.SQL('DROP TABLE {}.orders').format(s)]
        for query in statements:
            with self.subTest(query=query.as_string()),self.connect_reader() as conn:
                conn.execute('BEGIN READ WRITE')
                with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                    conn.execute(query)
                conn.rollback()

    def test_public_definer_trap_and_broker_preflight(self):
        s=self.sql.Identifier(self.schema)
        # This synthetic writer proves why SELECT grants alone are insufficient.
        self.admin.execute(self.sql.SQL('CREATE FUNCTION {}.synthetic_writer() RETURNS integer LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS {}').format(
            s,self.sql.Literal('UPDATE '+s.as_string()+'.orders SET total=total+1 WHERE id=1 RETURNING 1')))
        try:
            with self.connect_reader() as conn:
                conn.execute('BEGIN READ WRITE')
                self.assertEqual(conn.execute(self.sql.SQL('SELECT {}.synthetic_writer() AS n').format(s)).fetchone()['n'],1)
                conn.rollback()  # Never leave even this synthetic business row modified.
            with self.connect_reader() as conn:
                conn.execute('BEGIN READ ONLY')
                with self.assertRaises(self.psycopg.errors.ReadOnlySqlTransaction):
                    conn.execute(self.sql.SQL('SELECT {}.synthetic_writer()').format(s))
                conn.rollback()
            service=self.service()
            with self.assertRaises(DataAccessError) as caught:
                service.execute('fixture-child',{'schema':self.schema,'table':'orders'})
            self.assertEqual(caught.exception.code,'unsafe_database_role')
        finally:
            self.admin.execute(self.sql.SQL('DROP FUNCTION {}.synthetic_writer()').format(s))

    def test_schema_drift_fails_closed(self):
        service=self.service()
        s=self.sql.Identifier(self.schema)
        self.admin.execute(self.sql.SQL('ALTER TABLE {}.orders ADD COLUMN synthetic_extra text').format(s))
        try:
            with self.assertRaises(DataAccessError) as caught:
                service.execute('fixture-child',{'schema':self.schema,'table':'orders'})
            self.assertEqual(caught.exception.code,'catalog_changed')
        finally:
            self.admin.execute(self.sql.SQL('ALTER TABLE {}.orders DROP COLUMN synthetic_extra').format(s))


if __name__=='__main__':
    unittest.main()
