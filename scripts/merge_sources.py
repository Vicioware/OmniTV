#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paso 2: lee URLs de playlists, verifica vivos y los agrega a la maestra
preservando bloques completos (EXTINF + EXTVLCOPT + KODIPROP + URL).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from m3u_utils import (
    load_text,
    parse_playlist,
    parse_playlist_file,
    stream_key,
    write_playlist,
)


def load_source_urls(path: Path) -> list[str]:
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
    workers: int,
    sample: float,
) -> int:
    cmd = [
        sys.executable,
        str(script),
        str(input_path),
        "-o",
        str(ok_path),
        "-f",
        str(fail_path),
        "-w",
        str(workers),
        "-t",
        str(sample),
        "--no-csv",
        "-v",
    ]
    print("  checker:", " ".join(cmd), flush=True)
    return subprocess.run(cmd).returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", default="data/master.m3u")
    ap.add_argument("--sources", default="data/source_playlists.txt")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--sample", type=float, default=12.0)
    ap.add_argument(
        "--skip-check",
        action="store_true",
        help="No verificar fuentes (solo merge crudo; debug)",
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
        header, master_entries = parse_playlist_file(master_path)
    else:
        header, master_entries = "#EXTM3U", []

    existing = {stream_key(e) for e in master_entries if e.url}
    added = 0
    sources = load_source_urls(sources_path)
    print(f"Fuentes: {len(sources)} | ya en maestra: {len(master_entries)}", flush=True)

    with tempfile.TemporaryDirectory(prefix="iptv-merge-") as tmp:
        tmp_p = Path(tmp)

        for i, url in enumerate(sources, 1):
            print(f"\n[{i}/{len(sources)}] {url}", flush=True)
            try:
                text = load_text(url)
            except Exception as ex:  # noqa: BLE001
                print(f"  SKIP descarga: {ex}", flush=True)
                continue

            raw_path = tmp_p / f"src_{i}.m3u"
            ok_path = tmp_p / f"src_{i}_ok.m3u"
            fail_path = tmp_p / f"src_{i}_fail.m3u"
            raw_path.write_text(text, encoding="utf-8")

            if args.skip_check:
                alive_path = raw_path
            else:
                rc = run_checker(
                    script, raw_path, ok_path, fail_path, args.workers, args.sample
                )
                if rc != 0:
                    print(f"  SKIP checker rc={rc}", flush=True)
                    continue
                if not ok_path.exists():
                    print("  SKIP: sin archivo OK", flush=True)
                    continue
                alive_path = ok_path

            _, new_entries = parse_playlist_file(alive_path)
            local_added = 0
            for e in new_entries:
                if not e.url:
                    continue
                k = stream_key(e)
                if k in existing:
                    continue
                # reindex opcional; el idx solo es informativo
                e.idx = len(master_entries)
                master_entries.append(e)
                existing.add(k)
                local_added += 1
                added += 1
            print(
                f"  vivos={len(new_entries)} nuevos={local_added} total_maestra={len(master_entries)}",
                flush=True,
            )

    write_playlist(master_path, header, master_entries)
    print(f"\nMerge OK. Nuevos: {added}. Total: {len(master_entries)} -> {master_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())