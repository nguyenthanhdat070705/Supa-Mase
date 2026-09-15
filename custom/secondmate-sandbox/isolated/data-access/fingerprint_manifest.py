"""Operator-only, read-only fingerprint candidate generator; never approves data.

Writes a NEW mode-0600 private file with empty audit_ref values (runtime rejects
these until a human supplies a completed audit reference). Never used by a child.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import sys

from data_service import ReadOnlyService, DataAccessError, inspect_table, catalog_fingerprint


def generate(config):
    candidate=copy.deepcopy(config)
    scratch=copy.deepcopy(config)
    for child in scratch['children'].values():
        for entry in child['tables']:
            entry['catalog_fingerprint']='0'*64
            entry['audit_ref']='fingerprint-generation-only'
    service=ReadOnlyService(scratch)
    for child_id,scope in candidate['children'].items():
        def task(conn,cursor,role,stamp):
            for entry in scope['tables']:
                metadata=inspect_table(cursor,entry['schema'],entry['table'],stamp)
                entry['catalog_fingerprint']=catalog_fingerprint(metadata)
                entry['audit_ref']=''
        service._run(child_id,task)
    return candidate


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        candidate=generate(json.loads(Path(args.config).read_text(encoding='utf-8')))
        # Exclusive create avoids overwriting the active private manifest.
        fd=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as output:
            json.dump(candidate,output,ensure_ascii=False,indent=2)
            output.write('\n')
    except DataAccessError as exc:
        print('Fingerprint generation refused: '+exc.code,file=sys.stderr)
        return 1
    except Exception:
        print('Fingerprint generation failed; no configuration was approved.',file=sys.stderr)
        return 1
    print('Private candidate written; audit_ref is empty and enrollment remains blocked.')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
