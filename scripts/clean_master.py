#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paso 1: valida la maestra; verificar_m3u realiza ambas pasadas."""
from __future__ import annotations
import argparse, shutil, subprocess, sys, tempfile
from pathlib import Path

def copy(src:Path,dst:str|None)->None:
    if dst and src.exists():
        p=Path(dst).resolve();p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,p);print(f"Reporte: {p}",flush=True)
def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--master",default="data/master.m3u");ap.add_argument("--workers",type=int,default=3);ap.add_argument("--sample",type=float,default=12)
    ap.add_argument("--ok-output");ap.add_argument("--fail-output");ap.add_argument("--report-output");ap.add_argument("--first-ok-output");ap.add_argument("--first-fail-output");ap.add_argument("--first-report-output")
    args=ap.parse_args();master=Path(args.master).resolve();checker=Path(__file__).with_name("verificar_m3u.py")
    if not master.exists() or not checker.exists(): print("ERROR: falta maestra o verificar_m3u.py",file=sys.stderr);return 1
    with tempfile.TemporaryDirectory(prefix="iptv-clean-") as d:
        t=Path(d); ok=t/"alive_final.m3u";fail=t/"dead_final.m3u";report=t/"final.csv";fok=t/"alive_first.m3u";ffail=t/"dead_first.m3u";freport=t/"first.csv"
        cmd=[sys.executable,str(checker),str(master),"-o",str(ok),"-f",str(fail),"-r",str(report),"--first-ok",str(fok),"--first-fail",str(ffail),"--first-report",str(freport),"-w",str(args.workers),"-t",str(args.sample),"-v"]
        print("Ejecutando:"," ".join(cmd),flush=True)
        if subprocess.run(cmd).returncode:return 1
        if not ok.exists():print("ERROR: checker no produjo vivos",file=sys.stderr);return 1
        copy(ok,args.ok_output);copy(fail,args.fail_output);copy(report,args.report_output);copy(fok,args.first_ok_output);copy(ffail,args.first_fail_output);copy(freport,args.first_report_output)
        shutil.copyfile(ok,master);print(f"Maestra actualizada: {master}",flush=True)
    return 0
if __name__=="__main__":raise SystemExit(main())
