import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import data_client


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self,*args):
        pass

    def read(self,limit):
        return b'{"read_only":true,"rows":[]}'


class ChildClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token=Path(self.tmp.name)/'child-token'
        self.token.write_text('synthetic-child-token')
        self.config=Path(self.tmp.name)/'instance.json'
        self.config.write_text(json.dumps({'parent_url':'http://parent-control:8787',
            'control_token_file':str(self.token)}))

    def test_child_token_goes_only_to_fixed_parent_without_redirects(self):
        seen=[]
        class Opener:
            def open(self,request,timeout):
                seen.append((request,timeout))
                return FakeResponse()
        with patch('data_client.urllib.request.build_opener',return_value=Opener()) as builder:
            result=data_client.request_parent('/v1/data/schema',{},self.config)
        req,timeout=seen[0]
        self.assertEqual(req.full_url,'http://parent-control:8787/v1/data/schema')
        self.assertEqual(req.get_header('Authorization'),'Bearer synthetic-child-token')
        self.assertEqual(req.data,b'{}')
        self.assertTrue(result['read_only'])
        self.assertIsInstance(builder.call_args.args[0],data_client.NoRedirect)
        self.assertIsNone(builder.call_args.args[0].redirect_request(None,None,None,None,None,None))

    def test_embedded_credentials_and_external_scheme_are_rejected_before_send(self):
        for url in ('https://user:password@parent','file:///etc/secret','https://parent/#hidden','https://parent/?token=secret'):
            with self.subTest(url=url):
                self.config.write_text(json.dumps({'parent_url':url,'control_token_file':str(self.token)}))
                with patch('data_client.urllib.request.build_opener') as opener,self.assertRaises(ValueError):
                    data_client.request_parent('/v1/data/schema',{},self.config)
                opener.assert_not_called()

    def test_request_size_and_nonfinite_values_rejected_before_send(self):
        for payload in ({'value':'x'*100001},{'value':float('nan')}):
            with self.subTest(payload_type=type(payload)),patch('data_client.urllib.request.build_opener') as opener,self.assertRaises(ValueError):
                data_client.request_parent('/v1/data/query',payload,self.config)
            opener.assert_not_called()


if __name__=='__main__':
    unittest.main()
