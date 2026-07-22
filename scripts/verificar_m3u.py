#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verificador M3U con segunda pasada para candidatos fallidos."""
from __future__ import annotations
import argparse, csv, json, logging, os, re, shutil, signal, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from urllib.request import Request, urlopen

LOG=logging.getLogger("verificar_m3u")
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
HLS=re.compile(r"\.m3u8(?:\?|$)|/hls/|format=m3u8",re.I)
DASH=re.compile(r"\.mpd(?:\?|$)|/dash/|format=mpd",re.I)
TVG=re.compile(r'\btvg-name\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s,]+))',re.I)
BLACK=re.compile(r"black_duration:([-\d.]+)")
FREEZE=re.compile(r"freeze_duration:([\d.]+)")
FRAMES=re.compile(r"^frame=(\d+)\s*$",re.M)
OUTTIME=re.compile(r"^out_time_us=(\d+)\s*$",re.M)
ERR=re.compile(r"error|failed|invalid|denied|refused|timeout|unauthor|forbidden|not found|40[134]|50[023]|could not|cannot|connection|certificate|name resolution",re.I)
NETWORK=("404","403","401","429","500","502","503","504","not found","timed out","timeout","connection reset","connection refused","http error","server returned","temporarily","forbidden","certificate verify","name resolution","network is unreachable")

@dataclass
class Entry:
    idx:int; block:list[str]; extinf:str; url:str; name:str
    vlc:dict[str,str]=field(default_factory=dict); kodi:dict[str,str]=field(default_factory=dict); exthttp:dict[str,Any]=field(default_factory=dict)

def title(line:str)->str:
    m=TVG.search(line or "")
    if m:
        return (m.group(1) or m.group(2) or m.group(3) or "").strip() or "(sin nombre)"
    q=False; quote=""; comma=-1
    for i,c in enumerate(line or ""):
        if c in "\"'":
            if not q: q,quote=True,c
            elif c==quote: q,quote=False,""
        elif c=="," and not q: comma=i
    return (line[comma+1:].strip() if comma>=0 else "(sin nombre)") or "(sin nombre)"

def entry(idx:int, block:list[str])->Entry:
    extinf=url=""; vlc={}; kodi={}; exthttp={}
    for raw in block:
        s=raw.strip(); low=s.lower()
        if low.startswith("#extinf"): extinf=s
        elif low.startswith("#extvlcopt:"):
            k,_,v=s[11:].partition("="); vlc[k.strip().lower()]=v.strip()
        elif low.startswith("#kodiprop:"):
            k,_,v=s[10:].partition("="); kodi[k.strip().lower()]=v.strip()
        elif low.startswith("#exthttp:"):
            try:
                x=json.loads(s[9:]); exthttp=x if isinstance(x,dict) else {}
            except json.JSONDecodeError: pass
        elif not s.startswith("#"): url=s
    return Entry(idx,list(block),extinf,url,title(extinf),vlc,kodi,exthttp)

def parse(text:str)->tuple[str,list[Entry]]:
    header="#EXTM3U"; items=[]; block=[]; active=False
    for raw in text.splitlines():
        s=raw.strip()
        if not s: continue
        if s.startswith("#"):
            if s.upper().startswith("#EXTM3U") and not items and not block: header=s
            else:
                block.append(raw.rstrip("\r\n")); active |= s.lower().startswith("#extinf")
        elif active:
            block.append(raw.rstrip("\r\n")); items.append(entry(len(items),block)); block=[]; active=False
    return header,items

def load(src:str)->str:
    if src.lower().startswith(("http://","https://")):
        with urlopen(Request(src,headers={"User-Agent":UA}),timeout=90) as f: return f.read().decode("utf-8-sig","replace")
    return Path(src).read_text(encoding="utf-8-sig",errors="replace")

def write(path:str,header:str,items:list[Entry], reasons:dict[int,str]|None=None)->None:
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    with p.open("w",encoding="utf-8",newline="\n") as f:
        f.write(header.rstrip("\r\n")+"\n")
        for e in items:
            if reasons and e.idx in reasons: f.write("# MOTIVO_FALLO: "+reasons[e.idx]+"\n")
            for line in e.block: f.write(line.rstrip("\r\n")+"\n")

def input_args(e:Entry, default_ua:str)->list[str]:
    ua=e.vlc.get("http-user-agent",""); ref=e.vlc.get("http-referrer","") or e.vlc.get("http-referer",""); headers=[]
    def absorb(k:str,v:str):
        nonlocal ua,ref
        k=k.lower().replace("_","-")
        if k in ("user-agent","http-user-agent") and not ua: ua=v
        elif k in ("referer","referrer","http-referrer","http-referer") and not ref: ref=v
        else: headers.append(f"{k}: {v}")
    for k,v in e.exthttp.items(): absorb(str(k),str(v))
    for part in re.split(r"[&|]",e.kodi.get("inputstream.adaptive.stream_headers","") ):
        k,sep,v=part.partition("=")
        if sep: absorb(k.strip(),unquote(v).strip())
    cookie=e.vlc.get("http-cookie","")
    if cookie: headers.append("Cookie: "+cookie)
    args=["-user_agent",ua or default_ua] if (ua or default_ua) else []
    if ref: args += ["-referer",ref]
    if headers: args += ["-headers","".join(h+"\r\n" for h in headers)]
    return args

def supports(opt:str, scope:str="full")->bool:
    try:
        p=subprocess.run(["ffmpeg","-hide_banner","-h",scope],capture_output=True,text=True,errors="replace",timeout=15)
        return opt in ((p.stdout or "")+(p.stderr or ""))
    except Exception: return False

def sync_args()->list[str]:
    return ["-fps_mode","passthrough"] if supports("fps_mode") else []

def command(e:Entry,cfg:argparse.Namespace,sync:list[str], filters:bool=True, skip:float=0)->list[str]:
    kind="hls" if HLS.search(e.url) else ("dash" if DASH.search(e.url) else "file")
    c=["ffmpeg","-hide_banner","-nostdin","-loglevel","info","-rw_timeout",str(int(max(1,cfg.rw_timeout)*1_000_000)),"-reconnect","1","-reconnect_streamed","1","-reconnect_delay_max","3","-probesize",str(cfg.probesize),"-analyzeduration",str(cfg.analyzeduration),"-fflags","+genpts+discardcorrupt","-err_detect","ignore_err"]
    if not cfg.tls_verify and e.url.lower().startswith("https://"): c += ["-tls_verify","0"]
    if kind=="hls":
        if cfg.ext_picky: c += ["-extension_picky","0"]
        c += ["-allowed_extensions","ALL","-max_reload",str(cfg.max_reload),"-m3u8_hold_counters",str(cfg.max_reload)]
    c += input_args(e,cfg.default_ua)+["-i",e.url]
    if skip: c += ["-ss",f"{skip:.3f}"]
    c += ["-t",f"{cfg.sample:.3f}","-map","0:v:0"]
    if filters: c += ["-vf",f"blackdetect=d={cfg.black_min}:pix_th={cfg.pix_th},freezedetect=n={cfg.freeze_noise}dB:d={cfg.freeze_min}"]
    return c+sync+["-an","-sn","-dn","-progress","pipe:1","-nostats","-f","null","-"]

def kill(p:subprocess.Popen)->None:
    try:
        if os.name=="nt": p.kill()
        else: os.killpg(p.pid,signal.SIGKILL)
    except Exception:
        try:p.kill()
        except Exception:pass

def run(c:list[str],timeout:float)->tuple[int|None,str,str,bool]:
    kw={"stdout":subprocess.PIPE,"stderr":subprocess.PIPE,"text":True,"errors":"replace"}
    if os.name!="nt": kw["start_new_session"]=True
    try:p=subprocess.Popen(c,**kw)
    except OSError as x:return None,"",str(x),False
    try:
        o,e=p.communicate(timeout=timeout); return p.returncode,o or "",e or "",False
    except subprocess.TimeoutExpired:
        kill(p)
        try:o,e=p.communicate(timeout=5)
        except Exception:o,e="",""
        return p.returncode,o or "",e or "",True

def progress(out:str)->tuple[int,float]:
    f=FRAMES.findall(out); frames=int(f[-1]) if f else 0
    t=OUTTIME.findall(out); dur=int(t[-1])/1_000_000 if t else (frames/25 if frames else 0)
    return frames,dur

def errline(err:str)->str:
    for x in err.splitlines():
        x=x.strip()
        if x and ERR.search(x): return re.sub(r"^\[[^]]+\]\s*","",x)[:180]
    return "fallo desconocido"

def network(err:str)->bool:return any(x in err.lower() for x in NETWORK)
def drm(e:Entry)->bool:return bool(e.kodi.get("inputstream.adaptive.license_key") or (e.kodi.get("inputstream.adaptive.license_type","") and "clearkey" not in e.kodi.get("inputstream.adaptive.license_type","").lower()))

def analyze(e:Entry,cfg:argparse.Namespace,sync:list[str],pass_no:int)->dict[str,Any]:
    r={"idx":e.idx,"name":e.name,"url":e.url,"status":"fail","reason":"","frames":0,"duration":0.0,"black_pct":0.0,"freeze_s":0.0,"elapsed":0.0,"ffmpeg_rc":None,"timed_out":False,"pass":pass_no}
    if not e.url: r["reason"]="sin URL"; return r
    if drm(e): r["reason"]="DRM: no verificable sin licencia"; return r
    best=None; total=0.0; previous=""
    for attempt in range(cfg.retries+1):
        skip=cfg.startup_skip if attempt and previous=="black" else 0
        started=time.monotonic(); rc,out,err,to=run(command(e,cfg,sync,True,skip),cfg.timeout); total+=time.monotonic()-started
        fr,dur=progress(out)
        if fr==0:
            started=time.monotonic(); rc2,out2,err2,to2=run(command(e,cfg,sync,False,skip),cfg.timeout); total+=time.monotonic()-started
            fr2,dur2=progress(out2)
            if fr2>=fr: rc,out,err,to,fr,dur=rc2,out2,err2,to2,fr2,dur2
        black=sum(map(float,BLACK.findall(err))); freeze=sum(map(float,FREEZE.findall(err))); blackpct=black/dur*100 if dur else 0
        cand=(fr,-blackpct,rc,err,to,dur,blackpct,freeze,attempt,skip)
        if best is None or cand[:2]>best[:2]: best=cand
        min_d=max(cfg.sample*.35,cfg.min_frames/30); black_bad=fr>0 and dur>0 and blackpct>=cfg.black_pct; freeze_bad=dur>0 and freeze>=max(cfg.freeze_min,dur*cfg.freeze_fail_pct/100)
        if fr>=cfg.min_frames and dur>=min_d and not black_bad and not freeze_bad: break
        if attempt<cfg.retries:
            previous="black" if black_bad else "retry"
            delay=cfg.retry_delay*(attempt+1); LOG.info("Reintento %s/%s '%s' (pasada %s, pausa %.1fs)",attempt+2,cfg.retries+1,e.name[:50],pass_no,delay); time.sleep(delay)
    fr,_,rc,err,to,dur,blackpct,freeze,attempt,skip=best
    r.update(frames=fr,duration=round(dur,1),black_pct=round(blackpct,1),freeze_s=round(freeze,1),elapsed=round(total,1),ffmpeg_rc=rc,timed_out=to)
    min_d=max(cfg.sample*.35,cfg.min_frames/30)
    if fr>=cfg.min_frames and dur>=min_d:
        if blackpct>=cfg.black_pct: r["reason"]=f"pantalla negra ({blackpct:.0f}% del muestreo)"; return r
        if freeze>=max(cfg.freeze_min,dur*cfg.freeze_fail_pct/100): r["reason"]=f"imagen congelada ({freeze:.1f}s de {dur:.1f}s)"; return r
        r["status"]="ok"; r["reason"]=(f"ok tras reintento interno #{attempt+1}" if attempt else ""); return r
    if fr==0 and network(err): r["status"]="uncertain"; r["reason"]="no verificable desde este runner: "+errline(err); return r
    if fr==0 and to: r["status"]="uncertain"; r["reason"]=f"timeout sin frames en {cfg.timeout:.0f}s"; return r
    if fr==0 and "audio" in err.lower() and "video" not in err.lower(): r["reason"]="sin pista de vídeo (solo audio)"
    elif fr==0: r["reason"]="no abre/no decodifica: "+errline(err)
    else: r["reason"]=f"solo {fr} frames en {dur:.1f}s"
    return r

def write_csv(path:str,results:dict[int,dict[str,Any]])->None:
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    with p.open("w",encoding="utf-8",newline="") as f:
        w=csv.writer(f); w.writerow(["n","pasada","canal","url","estado","motivo","frames","muestreo_s","negro_%","congelado_s","check_s","ffmpeg_rc","timed_out"])
        for i in sorted(results):
            r=results[i]; w.writerow([i+1,r["pass"],r["name"],r["url"],r["status"],r["reason"],r["frames"],r["duration"],r["black_pct"],r["freeze_s"],r["elapsed"],r["ffmpeg_rc"],r["timed_out"]])

def parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("lista"); p.add_argument("-o","--ok",default="funcionan.m3u"); p.add_argument("-f","--fail",default="fallan.m3u"); p.add_argument("-r","--report",default="reporte.csv")
    p.add_argument("--first-ok");p.add_argument("--first-fail");p.add_argument("--first-report")
    p.add_argument("-w","--workers",type=int,default=3);p.add_argument("-t","--sample",type=float,default=15);p.add_argument("--timeout",type=float,default=0);p.add_argument("--rw-timeout",type=float,default=15);p.add_argument("--min-frames",type=int,default=20)
    p.add_argument("--black-min",type=float,default=2);p.add_argument("--pix-th",type=float,default=.10);p.add_argument("--black-pct",type=float,default=90);p.add_argument("--freeze-min",type=float,default=4);p.add_argument("--freeze-noise",type=float,default=-60);p.add_argument("--freeze-fail-pct",type=float,default=80)
    p.add_argument("--default-ua",default=UA);p.add_argument("--probesize",type=int,default=5_000_000);p.add_argument("--analyzeduration",type=int,default=5_000_000);p.add_argument("--max-reload",type=int,default=20);p.add_argument("--retries",type=int,default=1);p.add_argument("--retry-delay",type=float,default=2);p.add_argument("--startup-skip",type=float,default=8);p.add_argument("--tls-verify",action="store_true")
    p.add_argument("--recheck-workers",type=int,default=1);p.add_argument("--recheck-sample",type=float,default=18);p.add_argument("--recheck-timeout",type=float,default=180);p.add_argument("--recheck-retries",type=int,default=2);p.add_argument("--no-recheck",action="store_true");p.add_argument("--annotate",action="store_true");p.add_argument("-v","--verbose",action="count",default=0)
    return p

def batch(items:list[Entry],cfg:argparse.Namespace,sync:list[str],pass_no:int)->dict[int,dict[str,Any]]:
    ans={}; total=len(items)
    with ThreadPoolExecutor(max_workers=max(1,cfg.workers)) as pool:
        fut={pool.submit(analyze,e,cfg,sync,pass_no):e for e in items}
        for n,f in enumerate(as_completed(fut),1):
            e=fut[f]
            try:r=f.result()
            except Exception as x:r={"idx":e.idx,"name":e.name,"url":e.url,"status":"fail","reason":f"excepción interna: {x}","frames":0,"duration":0,"black_pct":0,"freeze_s":0,"elapsed":0,"ffmpeg_rc":None,"timed_out":False,"pass":pass_no}
            ans[e.idx]=r; print(f"[{n:>3}/{total}] {r['status'].upper():<9} | {e.name[:42]:<42} | {r['reason'] or str(r['frames'])+' frames'}",flush=True)
    return ans

def main()->int:
    cfg=parser().parse_args(); logging.basicConfig(level=logging.DEBUG if cfg.verbose>1 else logging.INFO if cfg.verbose else logging.WARNING,format="%(asctime)s %(levelname)-5s %(message)s",datefmt="%H:%M:%S",force=True)
    if not shutil.which("ffmpeg"): print("ERROR: ffmpeg no está en PATH",file=sys.stderr); return 1
    if cfg.timeout<=0: cfg.timeout=max(120,cfg.sample*5+45)
    cfg.ext_picky=supports("extension_picky","demuxer=hls"); sync=sync_args()
    LOG.info("sync_args=%s timeout=%.0fs workers=%s extension_picky=%s tls_verify=%s",sync,cfg.timeout,cfg.workers,cfg.ext_picky,cfg.tls_verify)
    try: header,items=parse(load(cfg.lista))
    except Exception as x: print(f"ERROR leyendo lista: {x}",file=sys.stderr); return 1
    if not items: write(cfg.ok,header,[]);write(cfg.fail,header,[]);write_csv(cfg.report,{});return 0
    print(f"Primera pasada: {len(items)} canales",flush=True); first=batch(items,cfg,sync,1)
    first_live=[e for e in items if first[e.idx]["status"] in ("ok","uncertain")]; first_dead=[e for e in items if first[e.idx]["status"]=="fail"]
    if cfg.first_ok:write(cfg.first_ok,header,first_live)
    if cfg.first_fail:write(cfg.first_fail,header,first_dead,{e.idx:first[e.idx]["reason"] for e in first_dead} if cfg.annotate else None)
    if cfg.first_report:write_csv(cfg.first_report,first)
    final=dict(first)
    candidates=[e for e in items if first[e.idx]["status"] in ("fail","uncertain")]
    if candidates and not cfg.no_recheck:
        print(f"\nSegunda pasada: {len(candidates)} candidatos (más lenta y secuencial)",flush=True)
        rcfg=argparse.Namespace(**vars(cfg)); rcfg.workers=max(1,cfg.recheck_workers);rcfg.sample=max(cfg.sample,cfg.recheck_sample);rcfg.timeout=max(cfg.timeout,cfg.recheck_timeout);rcfg.retries=max(cfg.retries,cfg.recheck_retries)
        second=batch(candidates,rcfg,sync,2)
        for i,r in second.items(): final[i]=r
    alive=[e for e in items if final[e.idx]["status"] in ("ok","uncertain")]; dead=[e for e in items if final[e.idx]["status"]=="fail"]
    write(cfg.ok,header,alive);write(cfg.fail,header,dead,{e.idx:final[e.idx]["reason"] for e in dead} if cfg.annotate else None);write_csv(cfg.report,final)
    recovered=sum(1 for e in candidates if first[e.idx]["status"]!="ok" and final[e.idx]["status"]=="ok")
    print(f"\nFINAL: vivos={len(alive)} | muertos={len(dead)} | recuperados en segunda pasada={recovered}",flush=True);print(f"OK: {cfg.ok}\nFAIL: {cfg.fail}\nCSV: {cfg.report}",flush=True)
    return 0
if __name__=="__main__": raise SystemExit(main())
