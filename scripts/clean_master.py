#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paso 1: elimina streams muertos de la playlist maestra (bloque completo)."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="Limpia streams muertos de la maestra")
    ap.add_argument("--master", default="data/master.m3u")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--sample", type=float, default=12.0)
    ap.add_argument("--extra-args", default="", help="Args extra para verificar_m3u")
    args = ap.parse_args()

    master = Path(args.master).resolve()
    script = Path(__file__).resolve().parent / "verificar_m3u.py"

    if not master.exists():
        print(f"ERROR: no existe {master}", file=sys.stderr)
        return 1
    if not script.exists():
        print(f"ERROR: no existe {script}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="iptv-clean-") as tmp:
        tmp_p = Path(tmp)
        ok_path = tmp_p / "ok.m3u"
        fail_path = tmp_p / "fail.m3u"

        cmd = [
            sys.executable,
            str(script),
            str(master),
            "-o",
            str(ok_path),
            "-f",
            str(fail_path),
            "-w",
            str(args.workers),
            "-t",
            str(args.sample),
            "--no-csv",
            "-v",
        ]
        if args.extra_args.strip():
            cmd.extend(args.extra_args.split())

        print("Ejecutando:", " ".join(cmd), flush=True)
        r = subprocess.run(cmd)
        if r.returncode != 0:
            print(f"ERROR: verificar_m3u salió con código {r.returncode}", file=sys.stderr)
            return r.returncode

        if not ok_path.exists():
            print("ERROR: no se generó el M3U de OK", file=sys.stderr)
            return 1

        shutil.copyfile(ok_path, master)
        # copiar fallos como artefacto opcional junto a la maestra
        fail_dest = master.with_name("master_fail_last.m3u")
        if fail_path.exists():
            shutil.copyfile(fail_path, fail_dest)
            print(f"Fallos guardados en {fail_dest}", flush=True)

        print(f"Maestra actualizada: {master}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())