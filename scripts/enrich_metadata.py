#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paso 3: asigna tvg-logo y tvg-id sin destruir el resto del bloque EXTINF
ni las líneas EXTVLCOPT/KODIPROP.
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
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

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def fetch(url: str, timeout: int = 180) -> str:
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def build_logo_index(logos_csv: str, channels_csv: str) -> dict[str, str]:
    id_to_logo: dict[str, str] = {}
    name_to_logo: dict[str, str] = {}

    for row in csv.DictReader(io.StringIO(logos_csv)):
        ch_id = (row.get("channel") or row.get("id") or "").strip()
        logo = (row.get("url") or row.get("logo") or "").strip()
        if not (ch_id and logo.startswith("http")):
            continue
        try:
            w = int(row.get("width") or 0)
        except ValueError:
            w = 0
        prev = id_to_logo.get(ch_id)
        if not prev or w >= 512:
            id_to_logo[ch_id] = logo

    for row in csv.DictReader(io.StringIO(channels_csv)):
        cid = (row.get("id") or "").strip()
        logo = id_to_logo.get(cid, "")
        if not logo:
            continue
        name = (row.get("name") or "").strip()
        alts = (row.get("alt_names") or "").strip()
        if name:
            name_to_logo.setdefault(normalize_name(name), logo)
        if alts:
            for alt in re.split(r"[;|]", alts):
                alt = alt.strip()
                if alt:
                    name_to_logo.setdefault(normalize_name(alt), logo)
        name_to_logo.setdefault(normalize_name(cid), logo)

    return name_to_logo


def build_epg_index(xml_text: str) -> dict[str, str]:
    xml_text = re.sub(r'\sxmlns="[^"]+"', "", xml_text, count=1)
    root = ET.fromstring(xml_text)
    index: dict[str, str] = {}
    for ch in root.findall("channel"):
        cid = (ch.get("id") or "").strip()
        if not cid:
            continue
        for dn in ch.findall("display-name"):
            text = (dn.text or "").strip()
            if text:
                index.setdefault(normalize_name(text), cid)
        index.setdefault(normalize_name(cid), cid)
    return index


def find_best(name: str, index: dict[str, str]) -> str | None:
    key = normalize_name(name)
    if not key:
        return None
    if key in index:
        return index[key]
    # match por contención (solo si la clave es razonablemente larga)
    for k, v in index.items():
        if len(key) >= 4 and len(k) >= 4 and (key in k or k in key):
            return v
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", default="data/master.m3u")
    ap.add_argument("--logos-url", default=DEFAULT_LOGOS)
    ap.add_argument("--channels-url", default=DEFAULT_CHANNELS)
    ap.add_argument("--epg-url", default=DEFAULT_EPG)
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    master = Path(args.master).resolve()
    if not master.exists():
        print(f"ERROR: no existe {master}", file=sys.stderr)
        return 1

    print("Descargando logos.csv ...", flush=True)
    logos_csv = fetch(args.logos_url)
    print("Descargando channels.csv ...", flush=True)
    channels_csv = fetch(args.channels_url)
    print("Descargando EPG XML ...", flush=True)
    epg_xml = fetch(args.epg_url)

    logo_idx = build_logo_index(logos_csv, channels_csv)
    epg_idx = build_epg_index(epg_xml)
    print(f"Índices: logos={len(logo_idx)} epg={len(epg_idx)}", flush=True)

    header, entries = parse_playlist_file(master)
    logo_hits = epg_hits = 0

    for e in entries:
        # nombre preferente: tvg-name ya resuelto en e.name
        if args.overwrite or not e.tvg_logo:
            logo = find_best(e.name, logo_idx)
            if logo and set_extinf_attr(e, "tvg-logo", logo, args.overwrite):
                logo_hits += 1
        if args.overwrite or not e.tvg_id:
            tid = find_best(e.name, epg_idx)
            if tid and set_extinf_attr(e, "tvg-id", tid, args.overwrite):
                epg_hits += 1

    write_playlist(master, header, entries)
    print(
        f"Enrich OK. logos+={logo_hits} tvg-id+={epg_hits} total={len(entries)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())