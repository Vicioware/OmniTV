#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paso 2:
- Descarga playlists indicadas en source_playlists.txt.
- Verifica cada una con verificar_m3u.py.
- Agrega streams vivos no duplicados a la maestra.
- Exporta, de forma opcional, un Ãºnico M3U con TODOS los vivos analizados
  y otro con TODOS los muertos analizados.

Cada entrada conserva su bloque completo:
  #EXTINF
  #EXTVLCOPT
  #KODIPROP
  #EXTHTTP
  URL
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from m3u_utils import (
    Entry,
    load_text,
    parse_playlist_file,
    stream_key,
    write_playlist,
)


def load_source_urls(path: Path) -> list[str]:
    """Obtiene una URL por lÃ­nea; ignora vacÃ­as y comentarios."""
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def run_checker(
    script: Path,
    input_path: Path,
    ok_path: Path,
    fail_path: Path,
    report_path: Path,
    workers: int,
    sample: float,
) -> int:
    """Ejecuta verificar_m3u.py sobre una fuente descargada."""
    cmd = [
        sys.executable,
        str(script),
        str(input_path),
        "-o",
        str(ok_path),
        "-f",
        str(fail_path),
        "-r",
        str(report_path),
        "-w",
        str(workers),
        "-t",
        str(sample),
        "-v",
    ]
    print("  checker:", " ".join(cmd), flush=True)
    return subprocess.run(cmd).returncode


def unique_entries(entries: Iterable[Entry]) -> list[Entry]:
    """
    Quita duplicados por URL para que los reportes finales no repitan
    un mismo stream proveniente de dos fuentes distintas.
    """
    seen: set[str] = set()
    unique: list[Entry] = []

    for entry in entries:
        if not entry.url:
            continue

        key = stream_key(entry)
        if not key or key in seen:
            continue

        seen.add(key)
        entry.idx = len(unique)
        unique.append(entry)

    return unique


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Verifica fuentes M3U y agrega streams vivos a la maestra"
    )
    ap.add_argument("--master", default="data/master.m3u")
    ap.add_argument("--sources", default="data/source_playlists.txt")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--sample", type=float, default=12.0)
    ap.add_argument(
        "--alive-output",
        default=None,
        help="M3U consolidado de todos los streams vivos analizados en fuentes",
    )
    ap.add_argument(
        "--dead-output",
        default=None,
        help="M3U consolidado de todos los streams muertos analizados en fuentes",
    )
    ap.add_argument(
        "--report-dir",
        default=None,
        help="Directorio opcional para guardar CSV individual por fuente",
    )
    ap.add_argument(
        "--skip-check",
        action="store_true",
        help="No verifica las fuentes; Ãºtil solo para depuraciÃ³n",
    )
    args = ap.parse_args()

    master_path = Path(args.master).resolve()
    sources_path = Path(args.sources).resolve()
    script = Path(__file__).resolve().parent / "verificar_m3u.py"

    if not sources_path.exists():
        print(f"ERROR: no existe {sources_path}", file=sys.stderr)
        return 1

    if not script.exists():
        print(f"ERROR: no existe {script}", file=sys.stderr)
        return 1

    if master_path.exists():
        master_header, master_entries = parse_playlist_file(master_path)
    else:
        master_header, master_entries = "#EXTM3U", []

    source_urls = load_source_urls(sources_path)
    existing_master = {
        stream_key(entry) for entry in master_entries if entry.url
    }

    all_alive: list[Entry] = []
    all_dead: list[Entry] = []
    added = 0
    source_errors = 0

    report_dir: Path | None = None
    if args.report_dir:
        report_dir = Path(args.report_dir).resolve()
        report_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"Fuentes: {len(source_urls)} | Streams iniciales en maestra: "
        f"{len(master_entries)}",
        flush=True,
    )

    with tempfile.TemporaryDirectory(prefix="iptv-merge-") as tmp:
        tmp_path = Path(tmp)

        for number, source_url in enumerate(source_urls, 1):
            print(
                f"\n========== Fuente {number}/{len(source_urls)} ==========",
                flush=True,
            )
            print(source_url, flush=True)

            try:
                text = load_text(source_url)
            except Exception as ex:  # noqa: BLE001
                source_errors += 1
                print(f"  SKIP descarga: {ex}", flush=True)
                continue

            raw_path = tmp_path / f"source_{number}.m3u"
            ok_path = tmp_path / f"source_{number}_alive.m3u"
            fail_path = tmp_path / f"source_{number}_dead.m3u"
            csv_path = tmp_path / f"source_{number}_check.csv"

            raw_path.write_text(text, encoding="utf-8")

            if args.skip_check:
                # En debug se considera todo lo descargado como "vivo".
                alive_path = raw_path
                dead_path: Path | None = None
            else:
                rc = run_checker(
                    script=script,
                    input_path=raw_path,
                    ok_path=ok_path,
                    fail_path=fail_path,
                    report_path=csv_path,
                    workers=args.workers,
                    sample=args.sample,
                )

                if rc != 0:
                    source_errors += 1
                    print(f"  SKIP checker: cÃ³digo de salida {rc}", flush=True)
                    continue

                if not ok_path.exists() or not fail_path.exists():
                    source_errors += 1
                    print(
                        "  SKIP checker: no se generÃ³ playlist de vivos o muertos",
                        flush=True,
                    )
                    continue

                alive_path = ok_path
                dead_path = fail_path

            _, alive_entries = parse_playlist_file(alive_path)
            all_alive.extend(alive_entries)

            dead_entries: list[Entry] = []
            if dead_path and dead_path.exists():
                _, dead_entries = parse_playlist_file(dead_path)
                all_dead.extend(dead_entries)

            if report_dir and csv_path.exists():
                target_csv = report_dir / f"source_{number:03d}_check.csv"
                csv_path.replace(target_csv)

            source_added = 0
            for entry in alive_entries:
                if not entry.url:
                    continue

                key = stream_key(entry)
                if not key or key in existing_master:
                    continue

                entry.idx = len(master_entries)
                master_entries.append(entry)
                existing_master.add(key)
                source_added += 1
                added += 1

            print(
                f"  vivos={len(alive_entries)} | muertos={len(dead_entries)} | "
                f"nuevos agregados={source_added} | "
                f"maestra={len(master_entries)}",
                flush=True,
            )

    # Consolidar y quitar repetidos Ãºnicamente en los reportes.
    all_alive = unique_entries(all_alive)
    all_dead = unique_entries(all_dead)

    # La maestra contiene entradas previas + nuevos streams vivos no duplicados.
    write_playlist(master_path, master_header, master_entries)

    # Ambos archivos son opcionales: solo se generan si el workflow lo pide.
    report_header = "#EXTM3U"
    if args.alive_output:
        alive_output = Path(args.alive_output).resolve()
        write_playlist(alive_output, report_header, all_alive)
        print(
            f"\nReporte de vivos: {len(all_alive)} -> {alive_output}",
            flush=True,
        )

    if args.dead_output:
        dead_output = Path(args.dead_output).resolve()
        write_playlist(dead_output, report_header, all_dead)
        print(
            f"Reporte de muertos: {len(all_dead)} -> {dead_output}",
            flush=True,
        )

    print("\n=================== RESUMEN MERGE ===================", flush=True)
    print(f"Fuentes procesadas: {len(source_urls)}", flush=True)
    print(f"Fuentes con error: {source_errors}", flush=True)
    print(f"Vivos analizados en fuentes: {len(all_alive)}", flush=True)
    print(f"Muertos analizados en fuentes: {len(all_dead)}", flush=True)
    print(f"Nuevos aÃ±adidos a la maestra: {added}", flush=True)
    print(f"Total final de la maestra: {len(master_entries)}", flush=True)
    print(f"Maestra: {master_path}", flush=True)

    # Una fuente caÃ­da no detiene todo el proceso:
    # la maestra se actualiza con las demÃ¡s fuentes vÃ¡lidas.
    return 0


if __name__ == "__main__":
    sys.exit(main())