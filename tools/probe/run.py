#!/usr/bin/env python3
# usage: tools/probe/run.py recipes/foo.m ['{"k":1}']
# Compiles a probe (includes tools/probe/probe.h) to a uniquely named dylib (dyld
# caches by path) and runs it in FCP via the bridge's dev.probe verb.
import sys, os, subprocess, json, socket, time
src=os.path.abspath(sys.argv[1]); args=json.loads(sys.argv[2]) if len(sys.argv)>2 else {}
here=os.path.dirname(os.path.abspath(__file__)); d=os.path.join(here,'build'); os.makedirs(d,exist_ok=True); out=os.path.join(d,f"{os.path.basename(src)[:-2]}_{int(time.time()*1000)}.dylib")
inc=here
r=subprocess.run(['clang','-arch','arm64','-dynamiclib','-undefined','dynamic_lookup','-fobjc-arc','-I',inc,
  '-framework','Foundation','-framework','AppKit','-framework','CoreMedia','-framework','QuartzCore','-framework','IOSurface','-framework','CoreImage','-o',out,src],capture_output=True,text=True)
if r.returncode: print(r.stderr); sys.exit(1)
s=socket.create_connection(('127.0.0.1',9876),timeout=float(os.environ.get('T','120')))
s.sendall((json.dumps({"jsonrpc":"2.0","id":1,"method":"dev.probe","params":{"path":out,"args":args}})+"\n").encode())
buf=b''
while not buf.endswith(b'\n'):
    c=s.recv(1<<20)
    if not c: break
    buf+=c
resp=json.loads(buf)
print(json.dumps(resp.get('result',resp),indent=1)[:int(os.environ.get('MAX','20000'))])
