#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parseo/escritura M3U preservando el bloque completo de cada canal."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote
from urllib.request import Request, urlopen

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

_TVG_NAME_RE = re.compile(
    r'\btvg-name\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s,]+))',
    re.I,
)
_TVG_ID_RE = re.compile(
    r'\btvg-id\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s,]+))',
    re.I,
)
_TVG_LOGO_RE = re.compile(
    r'\btvg-logo\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s,]+))',
    re.I,
)
_ATTR_START_RE = re.compile(r"^[\w.-]+\s*=")
_ATTR_RE = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')


@dataclass
class Entry:
    idx: int
    block: List[str]
    extinf: str
    url: str
    name: str
    vlc: Dict[str, str] = field(default_factory=dict)
    kodi: Dict[str, str] = field(default_factory=dict)
    exthttp: Dict[str, object] = field(default_factory=dict)

    @property
    def tvg_id(self) -> str:
        m = _TVG_ID_RE.search(self.extinf or "")
        if not m:
            return ""
        return (m.group(1) or m.group(2) or m.group(3) or "").strip()

    @property
    def tvg_logo(self) -> str:
        m = _TVG_LOGO_RE.search(self.extinf or "")
        if not m:
            return ""
        return (m.group(1) or m.group(2) or m.group(3) or "").strip()


def extinf_title(line: str) -> str:
    if not line:
        return "(sin nombre)"
    m = _TVG_NAME_RE.search(line)
    if m:
        name = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if name:
            return name

    in_q = False
    qch = ""
    candidates: List[str] = []
    for i, ch in enumerate(line):
        if ch in ('"', "'"):
            if not in_q:
                in_q, qch = True, ch
            elif ch == qch:
                in_q, qch = False, ""
        elif ch == "," and not in_q:
            rest = line[i + 1 :].strip()
            if rest:
                candidates.append(rest)

    for rest in reversed(candidates):
        if not _ATTR_START_RE.match(rest):
            return rest

    if candidates:
        cleaned = candidates[-1]
        while True:
            m_attr = re.match(
                r'^[\w.-]+\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s,]*)\s*,?\s*',
                cleaned,
            )
            if not m_attr:
                break
            nxt = cleaned[m_attr.end() :].strip()
            if not nxt or nxt == cleaned:
                break
            cleaned = nxt
        if cleaned and not _ATTR_START_RE.match(cleaned):
            return cleaned
    return "(sin nombre)"


def build_entry(idx: int, block: List[str]) -> Entry:
    extinf, url = "", ""
    vlc: Dict[str, str] = {}
    kodi: Dict[str, str] = {}
    exthttp: Dict[str, object] = {}
    for raw in block:
        line = raw.strip()
        low = line.lower()
        if low.startswith("#extinf"):
            extinf = line
        elif low.startswith("#extvlcopt:"):
            k, _, v = line[len("#EXTVLCOPT:") :].partition("=")
            vlc[k.strip().lower()] = v.strip()
        elif low.startswith("#kodiprop:"):
            k, _, v = line[len("#KODIPROP:") :].partition("=")
            kodi[k.strip().lower()] = v.strip()
        elif low.startswith("#exthttp:"):
            try:
                parsed = json.loads(line[len("#EXTHTTP:") :])
                exthttp = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                exthttp = {}
        elif not line.startswith("#"):
            url = line
    return Entry(
        idx, list(block), extinf, url, extinf_title(extinf), vlc, kodi, exthttp
    )


def parse_playlist(text: str) -> Tuple[str, List[Entry]]:
    """Igual que verificar_m3u: conserva cada lÃ­nea del bloque tal cual."""
    header, entries = "#EXTM3U", []
    block: List[str] = []
    has_extinf = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.upper().startswith("#EXTM3U") and not entries and not block:
                header = line
            else:
                block.append(raw.rstrip("\r\n"))
                if line.lower().startswith("#extinf"):
                    has_extinf = True
            continue
        if has_extinf:
            block.append(raw.rstrip("\r\n"))
            entries.append(build_entry(len(entries), block))
            block, has_extinf = [], False
    return header, entries


def parse_playlist_file(path: str | Path) -> Tuple[str, List[Entry]]:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    return parse_playlist(text)


def write_playlist(path: str | Path, header: str, entries: List[Entry]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write((header or "#EXTM3U").rstrip("\r\n") + "\n")
        for e in entries:
            for ln in e.block:
                fh.write(ln.rstrip("\r\n") + "\n")


def load_text(src: str, timeout: int = 90) -> str:
    if src.lower().startswith(("http://", "https://")):
        req = Request(src, headers={"User-Agent": CHROME_UA})
        with urlopen(req, timeout=timeout) as fh:
            return fh.read().decode("utf-8-sig", "replace")
    return Path(src).read_text(encoding="utf-8-sig", errors="replace")


def stream_key(e: Entry) -> str:
    """DeduplicaciÃ³n por URL (sin querystring)."""
    u = (e.url or "").strip()
    return u.split("?", 1)[0].rstrip("/")


def normalize_name(name: str) -> str:
    n = unquote(name or "").strip().lower()
    n = re.sub(r"\[.*?\]|\(.*?\)", " ", n)
    n = re.sub(r"[^\w\sÃ Ã¡Ã¢Ã£Ã¤Ã¥Ã¨Ã©ÃªÃ«Ã¬Ã­Ã®Ã¯Ã²Ã³Ã´ÃµÃ¶Ã¹ÃºÃ»Ã¼Ã±Ã§.+-]", " ", n, flags=re.UNICODE)
    n = re.sub(r"\s+", " ", n).strip()
    n = re.sub(r"\b(hd|fhd|uhd|4k|sd|hevc|h265|h264)\b", "", n).strip()
    return re.sub(r"\s+", " ", n)


def _set_or_replace_attr(extinf: str, key: str, value: str, overwrite: bool) -> str:
    """Inserta o reemplaza un atributo en la lÃ­nea #EXTINF sin tocar el resto."""
    if not extinf.lower().startswith("#extinf"):
        return extinf

    # Ya existe?
    pat = re.compile(
        rf'(\b{re.escape(key)}\s*=\s*)(?:"[^"]*"|\'[^\']*\'|[^\s,]+)',
        re.I,
    )
    m = pat.search(extinf)
    if m:
        current = m.group(0).split("=", 1)[-1].strip().strip("\"'")
        if current and not overwrite:
            return extinf
        return pat.sub(rf'\1"{value}"', extinf, count=1)

    # Insertar antes de la coma del tÃ­tulo (Ãºltima coma fuera de comillas)
    in_q = False
    qch = ""
    last_comma = -1
    for i, ch in enumerate(extinf):
        if ch in ('"', "'"):
            if not in_q:
                in_q, qch = True, ch
            elif ch == qch:
                in_q, qch = False, ""
        elif ch == "," and not in_q:
            last_comma = i

    attr = f'{key}="{value}"'
    if last_comma > 0:
        left = extinf[:last_comma].rstrip()
        right = extinf[last_comma:]  # incluye la coma y el nombre
        return f"{left} {attr}{right}"

    # Sin coma de tÃ­tulo: aÃ±adir al final
    return f"{extinf.rstrip()} {attr}"


def set_extinf_attr(entry: Entry, key: str, value: str, overwrite: bool = False) -> bool:
    """
    Actualiza tvg-logo / tvg-id (u otro attr) en extinf Y en block[lÃ­nea EXTINF].
    Devuelve True si hubo cambio.
    """
    if not value:
        return False
    new_extinf = _set_or_replace_attr(entry.extinf, key, value, overwrite)
    if new_extinf == entry.extinf:
        return False

    entry.extinf = new_extinf
    for i, ln in enumerate(entry.block):
        if ln.strip().lower().startswith("#extinf"):
            # preservar indentaciÃ³n original si la hubiera
            prefix = ln[: len(ln) - len(ln.lstrip())]
            entry.block[i] = prefix + new_extinf
            break
    # refrescar name por si venÃ­a de tvg-name
    entry.name = extinf_title(entry.extinf)
    return True