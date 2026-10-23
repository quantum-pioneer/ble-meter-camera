#!/usr/bin/env python3
"""Validate the published historical S3 layout and app fit; no private dump or device."""
from pathlib import Path
import csv, hashlib, struct, sys
root=Path(__file__).resolve().parent
built=Path(sys.argv[1])
expected=[('otadata','data','ota',0x9000,0x2000),('phy_init','data','phy',0xb000,0x1000),('app0','app','ota_0',0x10000,0x1c0000),('app1','app','ota_1',0x1d0000,0x1c0000),('nvs','data','nvs',0x390000,0x6d000)]
actual=[]
for row in csv.reader(x for x in (root/'BleMeterCamera/partitions.csv').read_text().splitlines() if x.strip() and not x.lstrip().startswith('#')):
    name,kind,sub,off,size,flags=[x.strip() for x in row]
    if flags: raise SystemExit('Unexpected partition flags')
    actual.append((name,kind,sub,int(off,0),int(size,0)))
if actual!=expected: raise SystemExit('Historical S3 partition layout changed')
if (built/'partitions.csv').read_bytes()!=(root/'BleMeterCamera/partitions.csv').read_bytes(): raise SystemExit('Build used another partition layout')
types={'app':0,'data':1}; subs={'ota':0,'phy':1,'ota_0':16,'ota_1':17,'nvs':2}
payload=b''.join(struct.pack('<HBBII16sI',0x50aa,types[kind],subs[sub],off,size,name.encode().ljust(16,b'\0'),0) for name,kind,sub,off,size in expected)
payload+=(b'\xeb\xeb'+b'\xff'*14+hashlib.md5(payload).digest())
if (built/'BleMeterCamera.ino.partitions.bin').read_bytes()!=payload.ljust(0xc00,b'\xff'): raise SystemExit('Generated partition binary differs')
app=built/'BleMeterCamera.ino.bin'
if not 0<app.stat().st_size<=0x1c0000 or app.read_bytes()[0]!=0xe9: raise SystemExit('App invalid or exceeds preserved slot')
print('Historical S3 table and application fit verified. This does not establish the table on another device.')
