#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paso 2: analiza fuentes (dos pasadas), consolida resultados y agrega vivos."""
from __future__ import annotations
import argparse, shutil, subprocess, sys, tempfile
from pathlib import Path
from m3u_utils import Entry,load_text,parse_playlist_file,stream_key,write_playlist

def sources(p:Path)->list[str]:return [x.strip() for x in p.read_text(encoding="utf-8",errors="replace").splitlines() if x.strip() and not x.lstrip().startswith("#")]
def unique(items:list[Entry])->list[Entry]:
    seen=set();out=[]
    for e in items:
        k=stream_key(e)
        if k and k not in seen:seen.add(k);e.idx=len(out);out.append(e)
    return out
def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--master",default="data/master.m3u");ap.add_argument("--sources",default="data/source_playlists.txt");ap.add_argument("--workers",type=int,default=3);ap.add_argument("--sample",type=float,default=12);ap.add_argument("--alive-output");ap.add_argument("--dead-output");ap.add_argument("--report-dir");args=ap.parse_args()
    master=Path(args.master).resolve();srcs=Path(args.sources).resolve();checker=Path(__file__).with_name("verificar_m3u.py")
    if not srcs.exists() or not checker.exists():print("ERROR: faltan fuentes o checker",file=sys.stderr);return 1
    header,master_items=parse_playlist_file(master) if master.exists() else ("#EXTM3U",[]);existing={stream_key(e) for e in master_items if e.url};alive=[];dead=[];added=errors=0
    rdir=Path(args.report_dir).resolve() if args.report_dir else None
    if rdir:rdir.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="iptv-merge-") as d:
        t=Path(d)
        for n,url in enumerate(sources(srcs),1):
            print(f"\n========== Fuente {n}: {url} ==========",flush=True)
            try:text=load_text(url)
            except Exception as x:print(f"SKIP descarga: {x}",flush=True);errors+=1;continue
            raw=t/f"source_{n}.m3u";ok=t/f"{n}_alive.m3u";fail=t/f"{n}_dead.m3u";csv=t/f"{n}_final.csv";raw.write_text(text,encoding="utf-8")
            cmd=[sys.executable,str(checker),str(raw),"-o",str(ok),"-f",str(fail),"-r",str(csv),"-w",str(args.workers),"-t",str(args.sample),"-v"]
            if rdir: cmd += ["--first-ok",str(rdir/f"source_{n:03d}_alive_first.m3u"),"--first-fail",str(rdir/f"source_{n:03d}_dead_first.m3u"),"--first-report",str(rdir/f"source_{n:03d}_first.csv")]
            if subprocess.run(cmd).returncode or not ok.exists():print("SKIP checker",flush=True);errors+=1;continue
            _,good=parse_playlist_file(ok);_,bad=parse_playlist_file(fail);alive+=good;dead+=bad
            if rdir and csv.exists():shutil.copyfile(csv,rdir/f"source_{n:03d}_final.csv")
            now=0
            for e in good:
                k=stream_key(e)
                if k and k not in existing: e.idx=len(master_items);master_items.append(e);existing.add(k);added+=1;now+=1
            print(f"vivos={len(good)} muertos={len(bad)} añadidos={now}",flush=True)
    alive=unique(alive);dead=unique(dead);write_playlist(master,header,master_items)
    if args.alive_output:write_playlist(args.alive_output,"#EXTM3U",alive)
    if args.dead_output:write_playlist(args.dead_output,"#EXTM3U",dead)
    print(f"FINAL fuentes: vivos={len(alive)} muertos={len(dead)} añadidos={added} errores_fuente={errors}",flush=True);return 0
if __name__=="__main__":raise SystemExit(main())
