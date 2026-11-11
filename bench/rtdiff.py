"""Structural benchmark: diff two FCPXML files clip-by-clip, matched by (lane,
absolute frame). usage: bench/rtdiff.py ours.fcpxml fcp_roundtrip.fcpxml
"""
import sys, xml.etree.ElementTree as ET, collections as C
from fractions import Fraction as F
FPS=F(30000,1001)
def sec(s): return F(s.rstrip('s')) if s else F(0)
def fr(x): return round(x*FPS)
def nv(v):
    try: return tuple(round(float(x),2) for x in v.replace('dB','').split())
    except: return v
def clips(p):
    r=ET.parse(p).getroot(); assets={a.get('id'):a.get('name') for a in r.iter('asset')}; assets.update({m.get('id'):m.get('name') for m in r.iter('media')})
    out={}
    def rec(ch, absoff, parent_start_rel):
        kfs={}
        for prm in ch.iter('param'):
            ks=prm.findall('./keyframeAnimation/keyframe')
            # keyframe times relative to clip start
            st=sec(ch.get('start'))
            if ks: kfs[prm.get('name')]=[(fr(sec(k.get('time'))-st),nv(k.get('value')),k.get('interp','linear')) for k in ks]
            fd=[(x.tag,fr(sec(x.get('duration')))) for x in prm if x.tag in('fadeIn','fadeOut')]
            if fd: kfs[prm.get('name')+':fades']=fd
        tr=ch.find('adjust-transform'); cr=ch.find('adjust-crop/trim-rect'); bl=ch.find('adjust-blend'); vol=ch.find('adjust-volume')
        d=dict(tag=ch.tag,src=assets.get(ch.get('ref')),dur=fr(sec(ch.get('duration'))),start=fr(sec(ch.get('start'))),en=ch.get('enabled','1'),
            pos=nv(tr.get('position','0 0')) if tr is not None else (0.0,0.0), scale=nv(tr.get('scale','1 1')) if tr is not None else (1.0,1.0),
            crop=None if cr is None else tuple(round(float(cr.get(x,0)),2) for x in('left','top','right','bottom')),
            blend=1.0 if bl is None else round(float(bl.get('amount','1')),3), vol=None if vol is None else round(float(vol.get('amount','0dB').rstrip('dB')),2),
            role=ch.get('audioRole') or ('video' if ch.tag!='asset-clip' or ch.get('srcEnable')=='video' else 'dialogue?'), kfs=kfs, tmap=len(ch.findall('timeMap/timept')))
        out.setdefault((ch.get('lane'),fr(absoff)),[]).append(d)
    def walk(e, base):  # base: (abs offset of parent item, parent start)
        for ch in e:
            if ch.tag in('asset-clip','video','ref-clip','gap'):
                off=sec(ch.get('offset'))
                if base is None: absoff=off
                else: absoff=base[0]+(off-base[1])
                if ch.tag!='gap': rec(ch,absoff,None)
                walk(ch,(absoff,sec(ch.get('start'))))
            elif ch.tag in('spine','sequence'): walk(ch,base)
    walk(r.find('.//project'),None)
    return out
A,B=clips(sys.argv[1]),clips(sys.argv[2])
ka,kb=set(A),set(B)
print('keys ours',len(ka),'fcp',len(kb),'only-ours',sorted(ka-kb)[:10],'only-fcp',sorted(kb-ka)[:10])
diffs=C.Counter(); ex={}
for k in ka&kb:
    for x,y in zip(A[k],B[k]):
        for f in x:
            if x[f]!=y[f]: diffs[f]+=1; ex.setdefault(f,(k,x['src'],x[f],y[f]))
print('field mismatches over',len(ka&kb),'clips:',dict(diffs))
for f,(k,n,a,b) in ex.items(): print(f'  {f} @{k} [{n}]\n    ours={str(a)[:400]}\n    fcp ={str(b)[:400]}')
for k in sorted(ka-kb): print('MISSING in FCP', k, A[k][0]['src'], A[k][0]['dur'])
