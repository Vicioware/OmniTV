#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paso 3: enriquece la playlist maestra con tvg-logo y tvg-id.

Optimizado para GitHub Actions:
- Descargas con timeout.
- Logs de progreso visibles.
- XMLTV procesado incrementalmente con iterparse().
- Matching O(1) por nombre normalizado; no recorre todo el EPG por canal.
- Conserva el bloque completo de cada Entry: solo cambia #EXTINF cuando
  añade tvg-logo o tvg-id.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import Request, urlopen

from m3u_utils import (
    normalize_name,
    parse_playlist_file,
    set_extinf_attr,
    write_playlist,
)

DEFAULT_LOGOS = (
    "https://raw.githubusercontent.com/iptv-org/database/master/data/logos.csv"
)

DEFAULT_CHANNELS = (
    "https://raw.githubusercontent.com/iptv-org/database/master/data/channels.csv"
)

DEFAULT_EPG = (
    "https://raw.githubusercontent.com/Puticastillo/EPGCL/"
    "refs/heads/main/smithers/guia-de-programacion.xml"
)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 "
    "OmniTV-Playlist-Updater/1.0"
)


def log(message: str) -> None:
    print(message, flush=True)


def fetch(url: str, timeout: int) -> str:
    """
    Descarga un recurso con timeout de socket y muestra tamaño/tiempo.

    Nota: timeout se aplica a la conexión/lecturas de red; el workflow también
    fijará un límite máximo para todo el paso de enriquecimiento.
    """
    started = time.monotonic()
    request = Request(url, headers={"User-Agent": USER_AGENT})

    with urlopen(request, timeout=timeout) as response:
        raw = response.read()

    elapsed = time.monotonic() - started
    size_mb = len(raw) / 1024 / 1024
    log(f"  Descargado: {size_mb:.2f} MiB en {elapsed:.1f}s")

    return raw.decode("utf-8-sig", errors="replace")


def add_index_value(index: dict[str, str], name: str, value: str) -> None:
    """Agrega una clave normalizada solo si no existe previamente."""
    key = normalize_name(name)
    if key and value:
        index.setdefault(key, value)


def build_logo_index(logos_csv: str, channels_csv: str) -> dict[str, str]:
    """
    Construye:
      nombre de canal normalizado -> URL de logo

    logos.csv usa principalmente IDs de iptv-org; channels.csv permite
    relacionar id -> nombre/alias legible.
    """
    id_to_logo: dict[str, str] = {}

    for row in csv.DictReader(io.StringIO(logos_csv)):
        channel_id = (row.get("channel") or row.get("id") or "").strip()
        logo_url = (row.get("url") or row.get("logo") or "").strip()

        if not channel_id or not logo_url.startswith(("http://", "https://")):
            continue

        # Se queda con el primero. Evita reemplazos no deterministas.
        id_to_logo.setdefault(channel_id, logo_url)

    name_to_logo: dict[str, str] = {}

    for row in csv.DictReader(io.StringIO(channels_csv)):
        channel_id = (row.get("id") or "").strip()
        logo_url = id_to_logo.get(channel_id, "")

        if not logo_url:
            continue

        name = (row.get("name") or "").strip()
        alt_names = (row.get("alt_names") or "").strip()

        add_index_value(name_to_logo, channel_id, logo_url)
        add_index_value(name_to_logo, name, logo_url)

        for alias in re.split(r"[;|]", alt_names):
            add_index_value(name_to_logo, alias.strip(), logo_url)

    return name_to_logo


def build_epg_index(xml_text: str) -> dict[str, str]:
    """
    Construye:
      display-name normalizado -> XMLTV channel id

    Usa iterparse para no construir/retener el árbol XMLTV completo.
    """
    index: dict[str, str] = {}
    channel_count = 0

    # StringIO permite iterparse sobre el XML descargado.
    source = io.StringIO(xml_text)

    for event, element in ET.iterparse(source, events=("end",)):
        if element.tag != "channel":
            continue

        channel_id = (element.get("id") or "").strip()

        if channel_id:
            add_index_value(index, channel_id, channel_id)

            for display_name in element.findall("display-name"):
                name = (display_name.text or "").strip()
                add_index_value(index, name, channel_id)

            channel_count += 1

            if channel_count % 10_000 == 0:
                log(
                    f"  EPG procesado: {channel_count:,} canales | "
                    f"{len(index):,} nombres indexados"
                )

        # Fundamental para no conservar nodos XML ya procesados.
        element.clear()

    log(
        f"  EPG indexado: {channel_count:,} canales | "
        f"{len(index):,} nombres/ids"
    )
    return index


def lookup_exact(name: str, index: dict[str, str]) -> str | None:
    """
    Match exacto normalizado: O(1).

    Evita el anterior matching por contención, que podía comparar cada canal
    contra miles de nombres EPG y dejar el workflow aparentemente congelado.
    """
    key = normalize_name(name)
    return index.get(key) if key else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Añade tvg-logo y tvg-id faltantes a una playlist M3U"
    )
    parser.add_argument("--master", default="data/master.m3u")
    parser.add_argument("--logos-url", default=DEFAULT_LOGOS)
    parser.add_argument("--channels-url", default=DEFAULT_CHANNELS)
    parser.add_argument("--epg-url", default=DEFAULT_EPG)
    parser.add_argument(
        "--download-timeout",
        type=int,
        default=120,
        help="Timeout de red por descarga, en segundos",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Reemplaza tvg-logo/tvg-id existentes",
    )
    args = parser.parse_args()

    master_path = Path(args.master).resolve()

    if not master_path.exists():
        print(f"ERROR: no existe {master_path}", file=sys.stderr)
        return 1

    total_start = time.monotonic()

    try:
        log("1/6 Descargando logos.csv...")
        logos_csv = fetch(args.logos_url, args.download_timeout)

        log("2/6 Descargando channels.csv...")
        channels_csv = fetch(args.channels_url, args.download_timeout)

        log("3/6 Descargando XMLTV EPG...")
        epg_xml = fetch(args.epg_url, args.download_timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: no se pudo descargar metadata: {exc}", file=sys.stderr)
        return 1

    try:
        log("4/6 Creando índice de logos...")
        logo_index = build_logo_index(logos_csv, channels_csv)
        log(f"  Logos indexados: {len(logo_index):,}")

        log("5/6 Creando índice XMLTV...")
        epg_index = build_epg_index(epg_xml)
    except (csv.Error, ET.ParseError, ValueError) as exc:
        print(f"ERROR: no se pudo procesar metadata: {exc}", file=sys.stderr)
        return 1

    log("6/6 Actualizando playlist maestra...")
    header, entries = parse_playlist_file(master_path)

    added_logo = 0
    added_tvg_id = 0
    existing_logo = 0
    existing_tvg_id = 0

    total = len(entries)

    for position, entry in enumerate(entries, start=1):
        if entry.tvg_logo and not args.overwrite:
            existing_logo += 1
        else:
            logo = lookup_exact(entry.name, logo_index)
            if logo and set_extinf_attr(
                entry,
                "tvg-logo",
                logo,
                overwrite=args.overwrite,
            ):
                added_logo += 1

        if entry.tvg_id and not args.overwrite:
            existing_tvg_id += 1
        else:
            tvg_id = lookup_exact(entry.name, epg_index)
            if tvg_id and set_extinf_attr(
                entry,
                "tvg-id",
                tvg_id,
                overwrite=args.overwrite,
            ):
                added_tvg_id += 1

        if position % 100 == 0 or position == total:
            log(f"  Playlist procesada: {position}/{total}")

    write_playlist(master_path, header, entries)

    elapsed = time.monotonic() - total_start
    log("\n================ RESUMEN ENRICH ================")
    log(f"Canales en maestra: {total}")
    log(f"tvg-logo ya existente: {existing_logo}")
    log(f"tvg-logo añadido: {added_logo}")
    log(f"tvg-id ya existente: {existing_tvg_id}")
    log(f"tvg-id añadido: {added_tvg_id}")
    log(f"Duración: {elapsed:.1f}s")
    log(f"Maestra actualizada: {master_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())