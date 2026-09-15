import copy
import json
from pathlib import Path
import tempfile
import unittest

from data_service import DataAccessError, ReadOnlyService, catalog_fingerprint


def fixture_config():
    metadata={'oid':123,'relation_kind':'r','rls_enabled':True,'force_rls':False,'owner_oid':10,
        'columns':[{'name':name,'type':kind,'type_schema':'pg_catalog','not_null':name=='id','position':i}
                   for i,(name,kind) in enumerate((('id','int8'),('region','text'),('total','numeric')),1)],
        'policies':[],'children':[],'primary_key':['id'],'rewrite_hash':'','executable_stamp':'fixture_stamp'}
    entry={'schema':'business','table':'orders','columns':{'id':'int8','region':'text','total':'numeric'},
           'catalog_fingerprint':catalog_fingerprint(metadata),'audit_ref':'synthetic-fixture-review',
           'relation_kind':'r','rls_reviewed':True,'row_visibility':'policy_filtered','primary_key':['id']}
    cfg={'version':1,'connections':{'fixture':{'dsn_file':str(Path(tempfile.gettempdir())/'unused-fixture.dsn'),'expected_role':'reader',
        'database':'fixture','require_recovery':False}},'children':{'child-a':{'connection':'fixture','tables':[entry]},
        'child-b':{'connection':'fixture','tables':[]}},'limits':{'max_rows':10,'max_bytes':10000}}
    return cfg, metadata


class FakeCursor:
    def __init__(self,conn,name=None):
        self.conn,self.name,self.rows=conn,name,[]

    def execute(self,sql,params=None):
        self.conn.calls.append((sql,params))
        if 'fm-data:role-preflight' in sql:
            self.rows=[copy.deepcopy(self.conn.role)]
        elif 'fm-data:executable-stamp' in sql:
            self.rows=[{'executable_stamp':self.conn.stamp}]
        elif 'fm-data:table-metadata' in sql:
            self.rows=[copy.deepcopy(self.conn.metadata)]
        elif 'fm-data:rows' in sql:
            if self.conn.data_error:
                raise RuntimeError('FAKE_DSN_PASSWORD_MUST_NOT_LEAK')
            self.rows=copy.deepcopy(self.conn.data)
        else:
            self.rows=[]
        return self

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def close(self):
        pass


class FakeConnection:
    def __init__(self,metadata):
        self.metadata=metadata
        self.stamp=metadata['executable_stamp']
        self.role={'role':'reader','session_role':'reader','database':'fixture','read_only':'on',
                   'in_recovery':False,'snapshot_time':'fixture snapshot','snapshot':'1:2:', 'server_version_num':170006}
        self.role.update({k:False for k in ('rolsuper','rolcreatedb','rolcreaterole','rolreplication','rolbypassrls',
            'membership','database_create','schema_control','table_write','sequence_write','owns_routine','executable_definer')})
        self.calls=[]
        self.data=[{'payload':'{"id":1,"region":"north","total":12.30}','payload_bytes':42}]
        self.data_error=False
        self.rollbacks=0
        self.closed=False

    def cursor(self,name=None):
        return FakeCursor(self,name)

    def rollback(self):
        self.rollbacks+=1

    def close(self):
        self.closed=True


class StructuredReadTests(unittest.TestCase):
    def test_vietnamese_and_camelcase_secret_columns_need_review(self):
        for column in ('Mật khẩu','Xác nhận mật khẩu','mật_khẩu','matKhau','apiKey','AccessToken'):
            with self.subTest(column=column), self.assertRaises(DataAccessError):
                cfg,_=fixture_config()
                cfg['children']['child-a']['tables'][0]['columns'][column]='text'
                ReadOnlyService(cfg)

    def test_credential_configuration_and_state_tables_need_review(self):
        for table in ('example_account_Signin','example_bot_state','SystemConfig','settings'):
            with self.subTest(table=table), self.assertRaises(DataAccessError):
                cfg,_=fixture_config()
                cfg['children']['child-a']['tables'][0]['table']=table
                ReadOnlyService(cfg)

    def setUp(self):
        self.cfg,self.metadata=fixture_config()
        self.conn=FakeConnection(self.metadata)
        self.opens=0
        def connect(cfg):
            self.opens+=1
            return self.conn
        self.service=ReadOnlyService(self.cfg,connection_factory=connect)
        self.request={'schema':'business','table':'orders'}

    def test_parameter_values_never_enter_sql(self):
        attack="x'; DELETE FROM business.orders; --"
        query=self.service.compile('child-a',{**self.request,'filters':[{'column':'region','op':'eq','value':attack}]})
        self.assertNotIn(attack,query['sql'])
        self.assertEqual(query['params'][0],attack)
        self.assertIn('OPERATOR(pg_catalog.=) %s::pg_catalog."text"',query['sql'])

    def test_no_sql_rpc_utility_or_identity_override(self):
        for key,value in (('sql','SELECT writer()'),('rpc','writer'),('child_id','child-b'),('dsn','anything'),
                          ('role','postgres'),('transaction','read write'),('having','true')):
            with self.subTest(key=key),self.assertRaises(DataAccessError):
                self.service.execute('child-a',{**self.request,key:value})
        self.assertEqual(self.opens,0)

    def test_cross_child_scope_denied_before_connect(self):
        for child in ('child-b','unknown'):
            with self.subTest(child=child),self.assertRaises(DataAccessError):
                self.service.execute(child,self.request)
        self.assertEqual(self.opens,0)

    def test_unknown_table_column_and_filter_rejected(self):
        samples=[{**self.request,'table':'orders;DROP TABLE x'},
                 {**self.request,'columns':['password']},
                 {**self.request,'filters':[{'column':'total','op':{'sql':'='},'value':0}]},
                 {**self.request,'filters':[{'column':'total','op':'eq','value':{'sql':'1'}}]}]
        for request in samples:
            with self.subTest(request=request),self.assertRaises(DataAccessError):
                self.service.execute('child-a',request)
        self.assertEqual(self.opens,0)

    def test_aggregates_are_fixed_functions_with_quoted_aliases(self):
        request={**self.request,'operation':'aggregate','group_by':['region'],
                 'aggregates':[{'fn':'sum','column':'total','as':'sales"; DROP TABLE x;--'},
                               {'fn':'count','as':'n'}], 'order_by':[{'column':'n','direction':'desc'}]}
        query=self.service.compile('child-a',request)
        self.assertIn('pg_catalog.sum("total") AS "sales""; DROP TABLE x;--"',query['sql'])
        self.assertIn('pg_catalog.count(*)',query['sql'])
        self.assertIn('GROUP BY "region"',query['sql'])
        self.assertIn('ORDER BY "n" DESC NULLS LAST',query['sql'])

    def test_unapproved_aggregate_and_string_sum_denied(self):
        for fn,col in (('http_post','total'),('sum','region'),('set_config','region')):
            with self.subTest(fn=fn),self.assertRaises(DataAccessError):
                self.service.compile('child-a',{**self.request,'operation':'aggregate',
                    'aggregates':[{'fn':fn,'column':col,'as':'x'}]})

    def test_keyset_compiles_parameterized_primary_key_predicate(self):
        query=self.service.compile('child-a',{**self.request,'after':{'id':'9007199254740999'},'limit':3})
        self.assertIn('"id" OPERATOR(pg_catalog.>) %s::pg_catalog."int8"',query['sql'])
        self.assertEqual(query['params'],['9007199254740999',4,0])
        self.assertTrue(query['keyset_eligible'])

    def test_invalid_keyset_shapes_and_ordering_denied(self):
        for extra in ({'after':{'total':1}}, {'after':{'id':1},'offset':1},
                      {'after':{'id':1},'order_by':[]}, {'after':{'id':None}},
                      {'after':{'id':1},'columns':['region']}):
            with self.subTest(extra=extra),self.assertRaises(DataAccessError):
                self.service.compile('child-a',{**self.request,**extra})

    def test_limits_nan_and_multi_object_filters_rejected(self):
        for extra in ({'limit':True},{'limit':11},{'offset':-1},
                      {'filters':[{'column':'total','op':'eq','value':float('nan')}]},
                      {'filters':[{'column':'id','op':'in','value':[]}]}):
            with self.subTest(extra=extra),self.assertRaises(DataAccessError):
                self.service.execute('child-a',{**self.request,**extra})
        self.assertEqual(self.opens,0)

    def test_reads_begin_read_only_and_always_rollback_close(self):
        result=self.service.execute('child-a',self.request)
        self.assertEqual(self.conn.calls[0][0],'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        self.assertTrue(result['read_only'])
        self.assertEqual(result['rows'][0]['total'],'12.30')
        self.assertEqual(self.conn.rollbacks,1)
        self.assertTrue(self.conn.closed)
        self.assertFalse(any(sql.startswith(('INSERT','UPDATE','DELETE','COMMIT')) for sql,_ in self.conn.calls))

    def test_admin_roles_ownership_memberships_and_write_rights_fail_closed(self):
        for flag in ('rolsuper','rolcreatedb','rolcreaterole','rolreplication','rolbypassrls','membership',
                     'database_create','schema_control','table_write','sequence_write','owns_routine','executable_definer'):
            self.conn.role[flag]=True
            with self.subTest(flag=flag),self.assertRaisesRegex(DataAccessError,'failed preflight'):
                self.service.execute('child-a',self.request)
            self.conn.role[flag]=False
        self.assertFalse(any('fm-data:rows' in sql for sql,_ in self.conn.calls))

    def test_wrong_session_identity_or_readwrite_transaction_denied(self):
        for key,value in (('role','postgres'),('session_role','postgres'),('database','other'),('read_only','off')):
            old=self.conn.role[key]
            self.conn.role[key]=value
            with self.subTest(key=key),self.assertRaises(DataAccessError):
                self.service.execute('child-a',self.request)
            self.conn.role[key]=old

    def test_replica_setting_does_not_allow_primary_connection(self):
        self.cfg['connections']['fixture']['require_recovery']=True
        service=ReadOnlyService(self.cfg,lambda cfg:self.conn)
        with self.assertRaises(DataAccessError):
            service.execute('child-a',self.request)

    def test_catalog_and_executable_drift_reject_before_data_read(self):
        self.conn.stamp='changed routine code'
        with self.assertRaisesRegex(DataAccessError,'operator review'):
            self.service.execute('child-a',self.request)
        self.assertFalse(any('fm-data:rows' in sql for sql,_ in self.conn.calls))

    def test_declared_key_must_match_real_primary_key(self):
        self.metadata['primary_key']=[]
        self.cfg['children']['child-a']['tables'][0]['catalog_fingerprint']=catalog_fingerprint(self.metadata)
        service=ReadOnlyService(self.cfg,lambda cfg:self.conn)
        with self.assertRaisesRegex(DataAccessError,'verified database primary key'):
            service.execute('child-a',self.request)

    def test_foreign_partition_cannot_hide_under_enrolled_base_table(self):
        self.metadata['children']=[{'child_oid':999,'relation_kind':'f','schema':'business'}]
        self.cfg['children']['child-a']['tables'][0]['catalog_fingerprint']=catalog_fingerprint(self.metadata)
        service=ReadOnlyService(self.cfg,lambda cfg:self.conn)
        with self.assertRaises(DataAccessError) as caught:
            service.execute('child-a',self.request)
        self.assertEqual(caught.exception.code,'unsafe_relation')

    def test_schema_returns_only_enrolled_columns_and_no_connection_details(self):
        result=self.service.schema('child-a',{})
        self.assertEqual([x['name'] for x in result['tables'][0]['columns']],['id','region','total'])
        self.assertNotIn('dsn',json.dumps(result))
        self.assertEqual(result['tables'][0]['row_visibility'],'policy_filtered')

    def test_page_bound_and_bigint_cursor_are_explicit(self):
        self.conn.data=[{'payload':'{"id":9007199254740999,"total":0.01}','payload_bytes':40},
                        {'payload':'{"id":9007199254741000,"total":0.02}','payload_bytes':40}]
        result=self.service.execute('child-a',{**self.request,'limit':1})
        self.assertEqual(result['row_count'],1)
        self.assertTrue(result['has_more'])
        self.assertEqual(result['next_after'],{'id':'9007199254740999'})
        self.assertIn('later pages',result['consistency'])

    def test_oversize_server_row_and_total_bytes_return_no_partial_data(self):
        self.conn.data=[{'payload':None,'payload_bytes':10001}]
        with self.assertRaisesRegex(DataAccessError,'byte bound'):
            self.service.execute('child-a',self.request)
        self.assertTrue(self.conn.closed)

    def test_database_error_is_sanitized_and_connection_closed(self):
        self.conn.data_error=True
        with self.assertRaises(DataAccessError) as caught:
            self.service.execute('child-a',self.request)
        self.assertNotIn('PASSWORD',str(caught.exception))
        self.assertEqual(caught.exception.code,'read_failed')
        self.assertTrue(self.conn.closed)

    def test_view_foreign_system_secret_and_unreviewed_manifest_rejected(self):
        for change in ({'relation_kind':'f'},{'relation_kind':'v'},{'schema':'auth'},
                       {'rls_reviewed':False},{'columns':{'password':'text'}},{'catalog_fingerprint':'unreviewed'}):
            cfg=copy.deepcopy(self.cfg)
            cfg['children']['child-a']['tables'][0].update(change)
            with self.subTest(change=change),self.assertRaises(DataAccessError):
                ReadOnlyService(cfg,lambda cfg:self.conn)


if __name__=='__main__':
    unittest.main(verbosity=2)
