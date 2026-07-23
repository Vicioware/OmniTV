#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paso 1: elimina streams muertos de la playlist maestra."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def copy_if_requested(source: Path, destination: str | None) -> None:
    """Copia source a destination si el usuario proporcionÃ³ una ruta."""
    if not destination or not source.exists():
        return

    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    print(f"Reporte guardado: {target}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Limpia streams muertos de la playlist maestra"
    )
    ap.add_argument("--master", default="data/master.m3u")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--sample", type=float, default=12.0)
    ap.add_argument(
        "--ok-output",
        default=None,
        help="Copia persistente de streams vivos detectados",
    )
    ap.add_argument(
        "--fail-output",
        default=None,
        help="Copia persistente de streams muertos detectados",
    )
    ap.add_argument(
        "--report-output",
        default=None,
        help="Copia persistente del CSV del checker",
    )
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
        ok_path = tmp_p / "master_alive.m3u"
        fail_path = tmp_p / "master_dead.m3u"
        csv_path = tmp_p / "master_check.csv"

        cmd = [
            sys.executable,
            str(script),
            str(master),
            "-o",
            str(ok_path),
            "-f",
            str(fail_path),
            "-r",
            str(csv_path),
            "-w",
            str(args.workers),
            "-t",
            str(args.sample),
            "-v",
        ]

        print("Ejecutando:", " ".join(cmd), flush=True)
        result = subprocess.run(cmd)

        if result.returncode != 0:
            print(
                f"ERROR: verificar_m3u saliÃ³ con cÃ³digo {result.returncode}",
                file=sys.stderr,
            )
            return result.returncode

        if not ok_path.exists():
            print("ERROR: no se generÃ³ la playlist de streams vivos.", file=sys.stderr)
            return 1

        # Reportes persistentes antes de que TemporaryDirectory borre /tmp.
        copy_if_requested(ok_path, args.ok_output)
        copy_if_requested(fail_path, args.fail_output)
        copy_if_requested(csv_path, args.report_output)

        # La maestra pasa a contener exclusivamente los vivos detectados.
        shutil.copyfile(ok_path, master)
        print(f"Maestra actualizada: {master}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())