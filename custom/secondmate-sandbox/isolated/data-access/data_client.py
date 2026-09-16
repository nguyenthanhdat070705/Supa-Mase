#!/usr/bin/env python3
"""Child CLI: structured reads through parent HTTP, never a database connection."""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def request_parent(endpoint,payload,instance_path):
    instance=json.loads(Path(instance_path).read_text())
    base=instance['parent_url'].rstrip('/')
    parsed=urllib.parse.urlsplit(base)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Invalid parent URL')
    token=Path(instance['control_token_file']).read_text().strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError('Invalid child capability')
    body=json.dumps(payload,allow_nan=False).encode()
    if len(body)>100000:
        raise ValueError('Request too large')
    req=urllib.request.Request(base+endpoint,data=body,method='POST',
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urllib.request.build_opener(NoRedirect()).open(req,timeout=130) as response:
        content=response.read(16_000_001)
        if len(content)>16_000_000:
            raise ValueError('Parent response too large')
        return json.loads(content)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('schema','query'))
    parser.add_argument('--request',type=Path,help='JSON request file; query otherwise reads stdin')
    parser.add_argument('--instance',default=os.environ.get('SM_INSTANCE_FILE','/opt/secondmate/instance.json'))
    args=parser.parse_args()
    payload=json.loads(args.request.read_text() if args.request else sys.stdin.read()) if args.request or args.operation=='query' else {}
    result=request_parent('/v1/data/'+args.operation,payload,args.instance)
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))


if __name__=='__main__':
    try:
        main()
    except Exception:
        print('Data request failed; private connection and capability details withheld.',file=sys.stderr)
        raise SystemExit(1)
