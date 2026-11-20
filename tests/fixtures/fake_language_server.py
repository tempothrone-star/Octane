"""Minimal stdio LSP peer for transport and semantic-provider regression tests."""
import json
import os
from pathlib import Path
import sys
import time

mode=sys.argv[1] if len(sys.argv)>1 else 'normal'


def send(value):
    data=json.dumps(value).encode()
    sys.stdout.buffer.write(f'Content-Length: {len(data)}\r\n\r\n'.encode()+data)
    sys.stdout.buffer.flush()


def read():
    headers={}
    while True:
        line=sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b'\r\n',b'\n'):
            break
        key,value=line.decode().split(':',1)
        headers[key.lower()]=value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers['content-length'])))


def item(name,uri,line=0):
    region={'start':{'line':line,'character':0},'end':{'line':line,'character':20}}
    return {'name':name,'kind':12,'uri':uri,'range':region,'selectionRange':region}


def result(request):
    method=request['method']; params=request.get('params',{})
    if method=='initialize':
        return {'capabilities':{'documentSymbolProvider':True,'callHierarchyProvider':True}}
    if method=='probe':
        send({'jsonrpc':'2.0','method':'textDocument/publishDiagnostics','params':{}})
        for index,name in enumerate(('workspace/configuration','workspace/applyEdit','unknown/request')):
            send({'jsonrpc':'2.0','id':f'server-{index}','method':name,'params':{'items':[{},{}]}})
        send({'jsonrpc':'2.0','id':-999,'result':None})
        sys.stderr.write('x'*20_000+'\nAPI_KEY=synthetic-secret\n');sys.stderr.flush()
        return {'environment':dict(os.environ),'ok':True}
    if method=='textDocument/documentSymbol':
        uri=params['textDocument']['uri']
        symbol=item('entry',uri)
        return [{'name':'module','kind':2,'children':[symbol]}]
    if method=='textDocument/prepareCallHierarchy':
        uri=params['textDocument']['uri']
        return [item('entry',uri),item('foreign','https://example.invalid/outside')]
    if method=='callHierarchy/outgoingCalls':
        root=Path.cwd()
        return [{'to':item('helper',(root/'helper.py').as_uri()),'fromRanges':[{'start':{'line':1,'character':0}}]},
                {'to':item('outside',root.parent.as_uri()),'fromRanges':[]}]
    return None


while request := read():
    if 'method' not in request or 'id' not in request:
        continue
    if mode=='exit':
        sys.stderr.write('provider exited\n');sys.stderr.flush();break
    if mode=='stall':
        time.sleep(60);break
    if mode=='bad-header':
        sys.stdout.buffer.write(b'not-a-header\r\n\r\n');sys.stdout.buffer.flush();break
    if mode=='oversize':
        sys.stdout.buffer.write(b'Content-Length: 4000001\r\n\r\n');sys.stdout.buffer.flush();break
    if mode=='short-body':
        sys.stdout.buffer.write(b'Content-Length: 50\r\n\r\n{}');sys.stdout.buffer.flush();break
    if mode=='error':
        send({'jsonrpc':'2.0','id':request['id'],'error':{'code':-32000,'message':'deliberate failure'}})
    else:
        send({'jsonrpc':'2.0','id':request['id'],'result':result(request)})
