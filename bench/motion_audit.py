"""Motion benchmark: per-frame position error between a WeftCut project.json and
the converted project open in FCP (sampled from FCP's live channels).
usage: bench/motion_audit.py project.json   (open the converted project first)
"""
import json, sys, socket, subprocess, os, time, importlib.util
from fractions import Fraction
spec=importlib.util.spec_from_file_location('w',os.path.join(os.path.dirname(os.path.abspath(__file__)),'..','scripts','weftcut_to_fcpxml.py')); w=importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
p=json.load(open(sys.argv[1])); root=p['compositions'][p['root_id']]; w.FPS=Fraction(30000,1001)
W,H=root['width'],root['height']
res=w.Resources(p)
jobs=[]; expect={}
for tr in root['tracks']:
    for l in tr['layers']:
        pr=l['params']; t=pr.get('transform')
        if not t or (t['position']['x']['mode']=='Static' and t['position']['y']['mode']=='Static'): continue
        base=l.get('label') or (os.path.splitext(os.path.basename(p['media_pool'][pr['media']]['path_abs']))[0] if pr.get('media') in p['media_pool'] else pr['kind'])
        name=f"{base} ·{l['id'][-6:]}"
        if pr['kind']=='VideoClip':
            start=res.asset(pr['media'])[1]+w.frames(pr['src_in_us'])
            if pr.get('time_map'): r_=pr['time_map']['rate']; start=round(start/Fraction(r_['num'],r_['den']))
        elif pr['kind']=='CompositionRef': start=w.frames(pr['src_in_us'])
        else: start=0
        dur=l['t_end_us']-l['t_start_us']; nfr=w.frames(dur)
        fr=list(range(0,nfr,1))  # every frame
        ts=[float((start+f)/w.FPS) for f in fr]
        ex=[]
        for f in fr:
            tus=round(Fraction(f)/w.FPS*1_000_000)
            x=w.evaluate(t['position']['x'],tus); y=w.evaluate(t['position']['y'],tus)
            ex.append(((x-W/2)/H*100, -(y-H/2)/H*100))
        jobs.append({'name':name,'t':ts}); expect[name]=(ex,l['t_start_us'])
out={}
for i in range(0,len(jobs),8):
    PROBE=os.path.join(os.path.dirname(os.path.abspath(__file__)),'..','tools','probe')
    r=subprocess.run(['./run.py','recipes/sample_position.m',json.dumps({'jobs':jobs[i:i+8]})],cwd=PROBE,capture_output=True,text=True,env={**os.environ,'MAX':'100000000'})
    out.update(json.loads(r.stdout))
px=H/100  # 1 unit = 21.6 px at 2160
worst=[]; allerr=[]
missing=[n for n,v in out.items() if v=='missing']
for n,(ex,_) in expect.items():
    got=out.get(n)
    if not isinstance(got,list): continue
    errs=[max(abs(a-g[0]),abs(b-g[1]))*px for (a,b),g in zip(ex,got)]
    allerr+=errs; worst.append((max(errs),n,errs.index(max(errs))))
worst.sort(reverse=True)
import statistics
print(json.dumps({'clips_sampled':len(worst),'missing':missing,'frames_sampled':len(allerr),
  'max_px':round(max(allerr),3),'p99_px':round(sorted(allerr)[int(len(allerr)*.99)],3),'median_px':round(statistics.median(allerr),4),
  'clips_over_1px':sum(1 for e,_,_ in worst if e>1),'worst':[(round(e,2),n,f) for e,n,f in worst[:5]]},indent=1))
# --- shift analysis
print('per-clip:')
for e,n,f in worst[:12]:
    ex,_=expect[n]; got=out[n]
    bad=[i for i,((a,b),g) in enumerate(zip(ex,got)) if max(abs(a-g[0]),abs(b-g[1]))*px>1]
    best=None
    for k in range(-40,41):
        errs=[max(abs(ex[i][0]-got[i+k][0]),abs(ex[i][1]-got[i+k][1]))*px for i in range(max(0,-k),min(len(ex),len(got)-k))]
        m=max(errs); best=min(best,(m,k)) if best else (m,k)
    print(f'  {n}: bad_frames={len(bad)}/{len(ex)} first_bad={bad[:3]} last_bad={bad[-2:]} best_shift={best[1]}f->max {best[0]:.2f}px')
print('--- even (keyed) frames only')
ev=[]; per=[]
for n,(ex,_) in expect.items():
    got=out.get(n)
    if not isinstance(got,list): continue
    e=[max(abs(ex[i][0]-got[i][0]),abs(ex[i][1]-got[i][1]))*px for i in range(0,len(ex),2)]
    ev+=e; per.append((max(e),n,[i*2 for i,x in enumerate(e) if x>1][:6], sum(1 for x in e if x>1)))
per.sort(reverse=True)
print('max',round(max(ev),3),'median',round(statistics.median(ev),5),'clips>1px',sum(1 for p in per if p[0]>1))
for p in per[:6]: print('  ',round(p[0],1),p[1],'bad keyed frames',p[3],'at',p[2])
print('--- detail')
for n,f0 in [('Charizard_BaseSet_Arita_base1-4 ·c79adc',14),('backdrop_right ·9f45da',2036)]:
    ex,_=expect[n]; got=out[n]
    for i in range(f0,f0+10,2): print(f'  {n[:20]} f{i} exp=({ex[i][0]:.2f},{ex[i][1]:.2f}) fcp=({got[i][0]:.2f},{got[i][1]:.2f})')
for tr in root['tracks']:
    for l in tr['layers']:
        if l['id'].endswith('c79adc') or l['id'].endswith('9f45da'):
            t=l['params']['transform']['position']
            for ax in 'xy':
                an=t[ax]
                if an['mode']=='Keyframed': print('  ',l['id'][-6:],ax,[(round(k['t_us']/1e6*30000/1001,1),k['value'],k['segment']['kind']) for k in an['value']][:12], an['extrapolate'])
                else: print('  ',l['id'][-6:],ax,'static',an['value'])
