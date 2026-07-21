#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote
from urllib.request import Request, urlopen

LOG = logging.getLogger("verifica_m3u")

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

_HLS_HINT = re.compile(r"\.m3u8(\?|$)|/playlist\.m3u8|/hls/|format=m3u8", re.I)
_DASH_HINT = re.compile(r"\.mpd(\?|$)|/dash/|format=mpd", re.I)
_TVG_NAME_RE = re.compile(
    r'\btvg-name\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s,]+))',
    re.I,
)
_ATTR_START_RE = re.compile(r"^[\w.-]+\s*=")
_CASCADE_ERR = re.compile(
    r"output file does not contain any stream|"
    r"error opening output file|"
    r"error opening output files|"
    r"nothing was written|"
    r"matches no streams|"
    r"invalid argument\s*$",
    re.I,
)

BLACK_RE = re.compile(
    r"black_start:[-\d.]+\s+black_end:[-\d.]+\s+black_duration:([-\d.]+)"
)
FREEZE_RE = re.compile(r"freeze_duration:([\d.]+)")
FRAME_RE = re.compile(r"^frame=(\d+)\s*$", re.M)
OUTTIME_RE = re.compile(r"^out_time_us=(\d+)\s*$", re.M)
OUTTIME_MS_RE = re.compile(r"^out_time_ms=(\d+)\s*$", re.M)
ERR_RE = re.compile(
    r"error|failed|invalid|denied|refused|timed?\s*out|"
    r"unauthor|forbidden|not found|40[134]|50[023]|could not|cannot|"
    r"404|403|401|connection reset|no route|name resolution",
    re.I,
)

# ---------------------------------------------------------------------------
# Modelo y parseo de la lista
# ---------------------------------------------------------------------------


@dataclass
class Entry:
    idx: int
    block: List[str]
    extinf: str
    url: str
    name: str
    vlc: Dict[str, str] = field(default_factory=dict)
    kodi: Dict[str, str] = field(default_factory=dict)
    exthttp: Dict[str, Any] = field(default_factory=dict)


def extinf_title(line: str) -> str:
    """Nombre del canal desde #EXTINF (tolera comas entre atributos)."""
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
        if not _ATTR_START_RE.match(cleaned):
            return cleaned or "(sin nombre)"
        if cleaned and not _ATTR_START_RE.match(cleaned):
            return cleaned

    return "(sin nombre)"


def build_entry(idx: int, block: List[str]) -> Entry:
    extinf, url = "", ""
    vlc: Dict[str, str] = {}
    kodi: Dict[str, str] = {}
    exthttp: Dict[str, Any] = {}
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
    """
    Parsea M3U conservando el bloque completo de cada canal
    (#EXTINF + EXTVLCOPT + KODIPROP + URL, etc.).
    """
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
                # Conservar la línea original (sin CR) para reescritura fiel
                block.append(raw.rstrip("\r\n"))
                if line.lower().startswith("#extinf"):
                    has_extinf = True
            continue
        if has_extinf:
            block.append(raw.rstrip("\r\n"))
            entries.append(build_entry(len(entries), block))
            block, has_extinf = [], False
    return header, entries


def is_drm(e: Entry) -> bool:
    if e.kodi.get("inputstream.adaptive.license_key"):
        return True
    lt = e.kodi.get("inputstream.adaptive.license_type", "").lower()
    return bool(lt) and "clearkey" not in lt


def stream_kind(url: str) -> str:
    u = (url or "").strip()
    if _HLS_HINT.search(u):
        return "hls"
    if _DASH_HINT.search(u):
        return "dash"
    return "file"


def _clip(s: str, n: int = 38) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------------------
# Traducción de opciones de la lista -> argumentos de FFmpeg
# ---------------------------------------------------------------------------


def ffmpeg_input_args(e: Entry, default_ua: str) -> List[str]:
    vlc = e.vlc
    ua = vlc.get("http-user-agent", "")
    referer = vlc.get("http-referrer", "") or vlc.get("http-referer", "")
    cookie = vlc.get("http-cookie", "")
    headers: List[str] = []

    def absorb(k: str, v: str) -> None:
        nonlocal ua, referer, cookie
        kl = k.lower().replace("_", "-")
        if kl in ("user-agent", "http-user-agent") and not ua:
            ua = v
        elif kl in ("referer", "referrer", "http-referrer", "http-referer") and not referer:
            referer = v
        elif kl in ("cookie", "http-cookie") and not cookie:
            cookie = v
        else:
            headers.append(f"{k}: {v}")

    for k, v in e.exthttp.items():
        absorb(str(k), str(v))

    sh = e.kodi.get("inputstream.adaptive.stream_headers", "")
    for part in re.split(r"[&|]", sh):
        k, sep, v = part.partition("=")
        if sep:
            absorb(k.strip(), unquote(v).strip())

    extra = vlc.get("http-header-fields", "")
    for h in re.split(r"\r\n|\\r\\n|\n|;", extra):
        h = h.strip()
        if ":" in h:
            headers.append(h)

    if cookie:
        headers.append(f"Cookie: {cookie}")

    args: List[str] = []
    if ua:
        args += ["-user_agent", ua]
    elif default_ua:
        args += ["-user_agent", default_ua]
    if referer:
        args += ["-referer", referer]
    if headers:
        merged: Dict[str, str] = {}
        for h in headers:
            name, _, val = h.partition(":")
            name = name.strip()
            if name:
                merged[name] = val.strip()
        hdr_str = "".join(f"{k}: {v}\r\n" for k, v in merged.items())
        args += ["-headers", hdr_str]
    return args


# ---------------------------------------------------------------------------
# Ejecución robusta de FFmpeg
# ---------------------------------------------------------------------------


def last_error_line(stderr: str) -> str:
    useful: List[str] = []
    for line in (stderr or "").splitlines():
        s = line.strip()
        if not s or not ERR_RE.search(s):
            continue
        s = re.sub(r"^\[[^\]]*\]\s*", "", s)
        if _CASCADE_ERR.search(s):
            continue
        useful.append(s)
    if useful:
        return useful[0][:170]
    cand = ""
    for line in (stderr or "").splitlines():
        s = line.strip()
        if s and ERR_RE.search(s):
            cand = re.sub(r"^\[[^\]]*\]\s*", "", s)
    return cand[:170] if cand else "fallo desconocido"


def _kill_process_tree(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
            try:
                proc.wait(timeout=3)
                return
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Error matando pid=%s: %s", proc.pid, exc)
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def run_ffmpeg(
    cmd: List[str], timeout: float
) -> Tuple[Optional[int], str, str, bool]:
    popen_kwargs: Dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "errors": "replace",
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    LOG.debug("CMD: %s", " ".join(cmd))

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except OSError as ex:
        return None, "", str(ex), False

    timed_out = False
    out, err = "", ""
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        LOG.debug("Timeout a los %.1fs — matando pid=%s", timeout, proc.pid)
        _kill_process_tree(proc)
        try:
            out, err = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            try:
                out, err = proc.communicate(timeout=3)
            except Exception:  # noqa: BLE001
                out, err = out or "", err or ""
    except Exception as ex:  # noqa: BLE001
        _kill_process_tree(proc)
        return None, "", f"error ejecutando ffmpeg: {ex}", False

    return proc.returncode, out or "", err or "", timed_out


def ffmpeg_sync_args() -> List[str]:
    try:
        out = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
        m = re.search(r"ffmpeg version \D*?(\d+)\.(\d+)", out or "")
        if m and (int(m.group(1)), int(m.group(2))) >= (5, 1):
            return ["-fps_mode", "passthrough"]
    except Exception:  # noqa: BLE001
        pass
    return ["-vsync", "0"]


def ffmpeg_supports_extension_picky() -> bool:
    """Devuelve True si el demuxer HLS admite -extension_picky."""
    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", "demuxer=hls"],
            capture_output=True,
            text=True,
            timeout=20,
            errors="replace",
        )
        blob = (p.stdout or "") + (p.stderr or "")
        return "extension_picky" in blob
    except Exception as exc:  # noqa: BLE001
        LOG.debug("No se pudo consultar extension_picky: %s", exc)
        return False


def build_ffmpeg_cmd(
    e: Entry,
    cfg: argparse.Namespace,
    sync_args: List[str],
    *,
    with_filters: bool = True,
    start_ss: float = 0.0,
) -> List[str]:
    """
    Construye el comando FFmpeg para validar un stream.

    Importante:
    - extension_picky es una opción privada del demuxer HLS.
    - No se añaden opciones de reconexión globalmente: algunos builds nuevos
      pueden rechazarlas según el protocolo/demuxer.
    """
    kind = stream_kind(e.url)
    rw_us = str(int(max(cfg.rw_timeout, 1.0) * 1_000_000))

    cmd: List[str] = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        "-rw_timeout",
        rw_us,
        "-probesize",
        str(cfg.probesize),
        "-analyzeduration",
        str(cfg.analyzeduration),
        "-fflags",
        "+genpts+discardcorrupt",
        "-err_detect",
        "ignore_err",
    ]

    # Estas opciones pertenecen al demuxer HLS y deben estar antes de -i.
    if kind == "hls":
        if getattr(cfg, "ext_picky", False):
            cmd += ["-extension_picky", "0"]

        cmd += [
            "-allowed_extensions",
            "ALL",
            "-max_reload",
            str(cfg.max_reload),
            "-m3u8_hold_counters",
            str(cfg.max_reload),
        ]

    # Headers, User-Agent y Referer también deben ir antes de -i.
    cmd += [*ffmpeg_input_args(e, cfg.default_ua), "-i", e.url]

    # -ss después del input: salta contenido ya abierto; se usa para
    # reintentar casos de pantalla negra al inicio.
    if start_ss and start_ss > 0:
        cmd += ["-ss", f"{start_ss:.3f}"]

    cmd += [
        "-t",
        f"{cfg.sample:.3f}",
        "-map",
        "0:v:0",
    ]

    if with_filters:
        vf = (
            f"blackdetect=d={cfg.black_min}:pix_th={cfg.pix_th},"
            f"freezedetect=n={cfg.freeze_noise}dB:d={cfg.freeze_min}"
        )
        cmd += ["-vf", vf]

    cmd += [
        *sync_args,
        "-an",
        "-sn",
        "-dn",
        "-progress",
        "pipe:1",
        "-nostats",
        "-f",
        "null",
        "-",
    ]
    return cmd

# ---------------------------------------------------------------------------
# Análisis de un canal
# ---------------------------------------------------------------------------


def _parse_progress(stdout: str) -> Tuple[int, float]:
    fr = FRAME_RE.findall(stdout)
    frames = int(fr[-1]) if fr else 0

    dur = 0.0
    tm = OUTTIME_RE.findall(stdout)
    if tm:
        dur = int(tm[-1]) / 1_000_000.0
    else:
        tm_ms = OUTTIME_MS_RE.findall(stdout)
        if tm_ms:
            val = int(tm_ms[-1])
            dur = val / 1_000_000.0 if val > 1_000_000 else val / 1_000.0

    if dur <= 0 and frames > 0:
        dur = frames / 25.0
    return frames, dur


def _transient_fail(err: str) -> bool:
    blob = (err or "").lower()
    keys = (
        "404",
        "403",
        "401",
        "502",
        "503",
        "504",
        "not found",
        "timed out",
        "timeout",
        "connection reset",
        "connection refused",
        "http error",
        "unable to open",
        "i/o error",
        "server returned",
        "temporarily",
        "try again",
        "access denied",
        "forbidden",
        "input/output error",
        "error number",
    )
    return any(k in blob for k in keys)


def analyze(e: Entry, cfg: argparse.Namespace, sync_args: List[str]) -> dict:
    r: Dict[str, Any] = {
        "idx": e.idx,
        "name": e.name,
        "url": e.url,
        "status": "fail",
        "reason": "",
        "frames": 0,
        "duration": 0.0,
        "black_pct": 0.0,
        "freeze_s": 0.0,
        "elapsed": 0.0,
        "ffmpeg_rc": None,
        "timed_out": False,
    }

    if not e.url:
        r["reason"] = "sin URL"
        return r

    if is_drm(e):
        r["reason"] = "DRM: no verificable sin licencia"
        return r

    retries = max(0, int(getattr(cfg, "retries", 2)))
    retry_delay = max(0.0, float(getattr(cfg, "retry_delay", 2.0)))
    startup_skip = max(0.0, float(getattr(cfg, "startup_skip", 8.0)))
    attempts = retries + 1

    best: Dict[str, Any] = {}
    total_elapsed = 0.0
    retry_reason = ""

    for attempt in range(attempts):
        start_ss = 0.0
        if attempt > 0 and retry_reason == "black":
            start_ss = startup_skip

        def _run(
            with_filters: bool, ss: float
        ) -> Tuple[Optional[int], str, str, bool, float]:
            cmd = build_ffmpeg_cmd(
                e, cfg, sync_args, with_filters=with_filters, start_ss=ss
            )
            t0 = time.monotonic()
            rc, out, err, timed_out = run_ffmpeg(cmd, cfg.timeout)
            return rc, out, err, timed_out, round(time.monotonic() - t0, 1)

        rc, out, err, timed_out, elapsed = _run(True, start_ss)
        frames, dur = _parse_progress(out)
        total_elapsed += elapsed

        if frames == 0:
            rc2, out2, err2, to2, el2 = _run(False, start_ss)
            total_elapsed += el2
            fr2, dur2 = _parse_progress(out2)
            if fr2 > frames:
                rc, out, err, timed_out = rc2, out2, err2, to2
                frames, dur = fr2, dur2

        black_total = sum(float(x) for x in BLACK_RE.findall(err))
        freeze_total = sum(float(x) for x in FREEZE_RE.findall(err))
        if dur > 0:
            black_total = min(black_total, dur * 1.05)
            freeze_total = min(freeze_total, dur * 1.05)
        black_pct = (black_total / dur * 100.0) if dur > 0 else 0.0

        cand = {
            "rc": rc,
            "out": out,
            "err": err,
            "timed_out": timed_out,
            "frames": frames,
            "dur": dur,
            "black_pct": black_pct,
            "freeze_s": freeze_total,
            "attempt": attempt,
            "start_ss": start_ss,
        }

        if not best or frames > best["frames"] or (
            frames == best["frames"] and black_pct < best.get("black_pct", 999.0)
        ):
            best = cand

        LOG.debug(
            "[%s] attempt=%s/%s ss=%.1f frames=%s black=%.1f%% rc=%s",
            e.name[:40],
            attempt + 1,
            attempts,
            start_ss,
            frames,
            black_pct,
            rc,
        )

        min_ok_dur = max(cfg.sample * 0.35, cfg.min_frames / 30.0)
        freeze_threshold = (cfg.freeze_fail_pct / 100.0) * max(dur, 0.001)
        freeze_fail = (
            dur > 0
            and freeze_total >= freeze_threshold
            and freeze_total >= cfg.freeze_min
        )
        black_fail = dur > 0 and black_pct >= cfg.black_pct and frames > 0

        if frames >= cfg.min_frames and (
            dur >= min_ok_dur or frames >= cfg.min_frames
        ):
            if not black_fail and not freeze_fail:
                break

        if attempt >= attempts - 1:
            break

        retry = False
        retry_reason = ""
        if frames == 0 and _transient_fail(err):
            retry, retry_reason = True, "http"
        elif frames == 0 and timed_out:
            retry, retry_reason = True, "timeout"
        elif frames == 0:
            retry, retry_reason = True, "no_frames"
        elif black_fail:
            retry, retry_reason = True, "black"
        elif freeze_fail:
            retry, retry_reason = True, "freeze"

        if not retry:
            break

        delay = retry_delay * (attempt + 1)
        LOG.info(
            "Reintento %s/%s '%s' por %s (pausa %.1fs, next_ss=%.1f)",
            attempt + 2,
            attempts,
            e.name[:50],
            retry_reason,
            delay,
            startup_skip if retry_reason == "black" else 0.0,
        )
        time.sleep(delay)

    rc = best.get("rc")
    err = best.get("err", "") or ""
    timed_out = bool(best.get("timed_out", False))
    frames = int(best.get("frames") or 0)
    dur = float(best.get("dur") or 0.0)
    black_pct = float(best.get("black_pct") or 0.0)
    freeze_total = float(best.get("freeze_s") or 0.0)

    r["elapsed"] = round(total_elapsed, 1)
    r["ffmpeg_rc"] = rc
    r["timed_out"] = timed_out
    r.update(
        frames=frames,
        duration=round(dur, 1),
        black_pct=round(black_pct, 1),
        freeze_s=round(freeze_total, 1),
    )

    LOG.debug(
        "[%s] rc=%s timeout=%s frames=%s dur=%.2f black=%.1f%% freeze=%.1fs err_tail=%r",
        e.name[:40],
        rc,
        timed_out,
        frames,
        dur,
        black_pct,
        freeze_total,
        err[-300:].replace("\n", " | ") if err else "",
    )

    if rc is None and err and not frames:
        r["reason"] = (
            err
            if err.startswith("error") or "ffmpeg" in err.lower()
            else f"no se pudo ejecutar ffmpeg: {err}"
        )
        return r

    min_ok_dur = max(cfg.sample * 0.35, cfg.min_frames / 30.0)
    err_l = err.lower()

    if frames == 0:
        if "allowed_segment_extensions" in err_l:
            r["reason"] = (
                "HLS beacon/sin extensión de segmento: "
                "FFmpeg necesita -extension_picky 0 "
                f"(soporte en esta build: {getattr(cfg, 'ext_picky', False)})"
            )
        elif re.search(r"empty segment|parse_playlist error", err_l):
            r["reason"] = "HLS: playlist de medios vacía o no parseable"
        else:
            has_audio = bool(
                re.search(r"Stream\s+#\d+:\d+.*\bAudio\b", err, re.I)
            )
            has_video = bool(
                re.search(r"Stream\s+#\d+:\d+.*\bVideo\b", err, re.I)
            )
            if has_audio and not has_video:
                r["reason"] = "sin pista de vídeo (stream solo de audio)"
            elif timed_out:
                r["reason"] = f"timeout sin frames de vídeo en {cfg.timeout:.0f}s"
            elif "option not found" in err_l or "unrecognized option" in err_l:
                r["reason"] = "opciones FFmpeg incompatibles: " + last_error_line(err)
            elif rc not in (0, None):
                r["reason"] = "no abre/no decodifica: " + last_error_line(err)
            else:
                r["reason"] = "0 frames de vídeo decodificados"
        return r

    if frames < cfg.min_frames and dur < min_ok_dur:
        if timed_out:
            r["reason"] = (
                f"timeout con solo {frames} frames / {dur:.1f}s "
                f"(mín. {cfg.min_frames} frames)"
            )
        else:
            r["reason"] = (
                f"solo {frames} frames en {dur:.1f}s: "
                "no llega a mostrar imagen estable"
            )
        return r

    if dur > 0 and black_pct >= cfg.black_pct:
        r["reason"] = f"pantalla negra ({black_pct:.0f}% del muestreo)"
        return r

    freeze_threshold = (cfg.freeze_fail_pct / 100.0) * max(dur, 0.001)
    if (
        dur > 0
        and freeze_total >= freeze_threshold
        and freeze_total >= cfg.freeze_min
    ):
        r["reason"] = f"imagen congelada ({freeze_total:.1f}s de {dur:.1f}s)"
        return r

    if (
        not timed_out
        and rc not in (0, None)
        and dur < cfg.sample * 0.5
        and frames < cfg.min_frames * 2
    ):
        r["reason"] = "se interrumpe durante la reproducción: " + last_error_line(err)
        return r

    r["status"] = "ok"
    extras: List[str] = []
    if best.get("attempt", 0) > 0:
        extras.append(f"ok tras reintento #{best['attempt'] + 1}")
    if best.get("start_ss", 0) > 0:
        extras.append(f"ss={best['start_ss']:.0f}s")
    if timed_out:
        extras.append(
            f"muestreo parcial {dur:.1f}s/{cfg.sample:.0f}s por timeout de proceso"
        )
    elif rc not in (0, None):
        extras.append("ffmpeg rc≠0 al cerrar, imagen válida")
    r["reason"] = " · ".join(extras)
    return r


# ---------------------------------------------------------------------------
# E/S
# ---------------------------------------------------------------------------


def load_text(src: str, timeout: int = 90) -> str:
    if src.lower().startswith(("http://", "https://")):
        req = Request(src, headers={"User-Agent": CHROME_UA})
        with urlopen(req, timeout=timeout) as fh:
            return fh.read().decode("utf-8-sig", "replace")
    with open(src, "r", encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def write_m3u(
    path: str,
    header: str,
    entries: List[Entry],
    reasons: Optional[Dict[int, str]] = None,
) -> None:
    """Escribe M3U volcando cada Entry.block íntegro (EXTINF+opts+URL)."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write((header or "#EXTM3U").rstrip("\r\n") + "\n")
        for e in entries:
            if reasons and e.idx in reasons:
                fh.write(f"# MOTIVO_FALLO: {reasons[e.idx]}\n")
            for ln in e.block:
                fh.write(ln.rstrip("\r\n") + "\n")


def write_csv(path: str, results: Dict[int, dict]) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "n",
                "canal",
                "url",
                "estado",
                "motivo",
                "frames",
                "muestreo_s",
                "negro_%",
                "congelado_s",
                "check_s",
                "ffmpeg_rc",
                "timed_out",
            ]
        )
        for i in sorted(results):
            r = results[i]
            w.writerow(
                [
                    i + 1,
                    r["name"],
                    r["url"],
                    r["status"],
                    r["reason"],
                    r["frames"],
                    r["duration"],
                    r["black_pct"],
                    r["freeze_s"],
                    r["elapsed"],
                    r.get("ffmpeg_rc", ""),
                    r.get("timed_out", ""),
                ]
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=(
            "Verifica con FFmpeg qué canales de un M3U se reproducen de verdad "
            "(decodifica vídeo y detecta negro/congelado)."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("lista", help="archivo .m3u/.m3u8 local o URL http(s)")
    ap.add_argument("-o", "--ok", default="funcionan.m3u", help="salida con canales OK")
    ap.add_argument(
        "-f", "--fail", default="fallan.m3u", help="salida con canales que fallan"
    )
    ap.add_argument("-r", "--report", default="reporte.csv", help="informe CSV")
    ap.add_argument(
        "-w",
        "--workers",
        type=int,
        default=3,
        help="canales en paralelo (bajar si hay muchos timeouts por CPU/red)",
    )
    ap.add_argument(
        "-t",
        "--sample",
        type=float,
        default=15.0,
        help="segundos de vídeo muestreados por canal (en directo ≈ tiempo real)",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help="tope duro por canal en segundos (0 = auto: max(120, sample*5+45))",
    )
    ap.add_argument(
        "--rw-timeout",
        type=float,
        default=15.0,
        help="timeout de red por operación (s)",
    )
    ap.add_argument(
        "--min-frames",
        type=int,
        default=20,
        help="frames mínimos para dar por bueno",
    )
    ap.add_argument(
        "--black-min", type=float, default=2.0, help="negro mínimo detectable (s)"
    )
    ap.add_argument(
        "--pix-th", type=float, default=0.10, help="umbral de píxel negro (0-1)"
    )
    ap.add_argument(
        "--black-pct",
        type=float,
        default=90.0,
        help="%% del muestreo en negro para declarar pantalla negra",
    )
    ap.add_argument(
        "--freeze-min",
        type=float,
        default=4.0,
        help="congelado mínimo detectable (s)",
    )
    ap.add_argument(
        "--freeze-noise",
        type=float,
        default=-60.0,
        help="tolerancia de ruido freezedetect (dB)",
    )
    ap.add_argument(
        "--freeze-fail-pct",
        type=float,
        default=80.0,
        help="%% del muestreo congelado para declarar imagen congelada",
    )
    ap.add_argument(
        "--default-ua",
        default=CHROME_UA,
        help='User-Agent cuando el canal no define uno ("" = no enviar)',
    )
    ap.add_argument(
        "--probesize",
        type=int,
        default=5_000_000,
        help="bytes de probe FFmpeg",
    )
    ap.add_argument(
        "--analyzeduration",
        type=int,
        default=5_000_000,
        help="microsegundos de analyze FFmpeg",
    )
    ap.add_argument(
        "--max-reload",
        type=int,
        default=20,
        help="máximo reloads de playlist HLS (evita cuelgues infinitos)",
    )
    ap.add_argument(
        "--retries",
        type=int,
        default=2,
        help="reintentos extra por canal ante fallo transitorio o negro total",
    )
    ap.add_argument(
        "--retry-delay",
        type=float,
        default=2.0,
        help="pausa base entre reintentos (s); crece de forma lineal",
    )
    ap.add_argument(
        "--startup-skip",
        type=float,
        default=8.0,
        help="en reintento por pantalla negra, segundos iniciales a ignorar (-ss)",
    )
    ap.add_argument(
        "--annotate",
        action="store_true",
        help="añadir línea '# MOTIVO_FALLO:' en el m3u de fallos",
    )
    ap.add_argument(
        "--no-csv",
        action="store_true",
        help="No generar reporte CSV (útil en CI)",
    )
    ap.add_argument(
        "--no-fail-file",
        action="store_true",
        help="No escribir el M3U de fallos",
    )
    ap.add_argument(
        "--quiet-summary",
        action="store_true",
        help="Omitir desglose de motivos de fallo al final",
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="logging (-v = INFO, -vv = DEBUG con comando FFmpeg y cola de stderr)",
    )
    return ap


def setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


def main() -> int:
    ap = build_arg_parser()
    cfg = ap.parse_args()
    setup_logging(cfg.verbose)

    if cfg.timeout <= 0:
        cfg.timeout = max(120.0, cfg.sample * 5.0 + 45.0)

    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg no está instalado o no está en el PATH.", file=sys.stderr)
        return 1

    sync_args = ffmpeg_sync_args()
    cfg.ext_picky = ffmpeg_supports_extension_picky()
    LOG.info(
        "sync_args=%s timeout=%.1fs workers=%d extension_picky=%s retries=%d",
        sync_args,
        cfg.timeout,
        cfg.workers,
        cfg.ext_picky,
        cfg.retries,
    )
    if not cfg.ext_picky:
        print(
            "AVISO: tu FFmpeg no soporta -extension_picky; "
            "canales HLS tipo Amagi/beacon pueden marcarse FAIL. "
            "Actualiza FFmpeg a 6.1+.",
            flush=True,
        )

    try:
        text = load_text(cfg.lista)
    except Exception as ex:  # noqa: BLE001
        print(f"ERROR: no se pudo leer la lista: {ex}", file=sys.stderr)
        return 1

    header, entries = parse_playlist(text)

    # Lista vacía: resultado válido para el pipeline (escribe OK vacío, exit 0)
    if not entries:
        write_m3u(cfg.ok, header, [])
        if not cfg.no_fail_file:
            write_m3u(cfg.fail, header, [])
        if not cfg.no_csv:
            write_csv(cfg.report, {})
        print(
            "AVISO: no se encontraron entradas #EXTINF con URL. "
            f"Se escribió lista OK vacía en {cfg.ok}",
            flush=True,
        )
        return 0

    mins = math.ceil(len(entries) / max(cfg.workers, 1)) * cfg.sample / 60
    print(
        f"{len(entries)} canales | {cfg.workers} hilos | "
        f"muestreo {cfg.sample:.0f}s/canal | timeout {cfg.timeout:.0f}s | "
        f"reintentos {cfg.retries}",
        flush=True,
    )
    print(f"Tiempo estimado aprox. (directos): ~{mins:.0f} min\n", flush=True)

    results: Dict[int, dict] = {}
    total = len(entries)
    pool = ThreadPoolExecutor(max_workers=max(cfg.workers, 1))
    try:
        futs = {pool.submit(analyze, e, cfg, sync_args): e for e in entries}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                r = fut.result()
            except Exception as ex:  # noqa: BLE001
                e = futs[fut]
                LOG.exception("Excepción analizando %s", e.name)
                r = {
                    "idx": e.idx,
                    "name": e.name,
                    "url": e.url,
                    "status": "fail",
                    "reason": f"excepción interna: {ex}",
                    "frames": 0,
                    "duration": 0.0,
                    "black_pct": 0.0,
                    "freeze_s": 0.0,
                    "elapsed": 0.0,
                    "ffmpeg_rc": None,
                    "timed_out": False,
                }
            results[r["idx"]] = r
            tag = "OK  " if r["status"] == "ok" else "FAIL"
            if r["status"] == "ok":
                det = f'{r["frames"]} frames en {r["duration"]:.0f}s'
                if r.get("reason"):
                    det += f' · {r["reason"]}'
            else:
                det = r["reason"]
            print(
                f'[{n:>{len(str(total))}}/{total}] {tag} | '
                f'{_clip(r["name"], 38):<38} | {det}',
                flush=True,
            )
    except KeyboardInterrupt:
        print(
            "\nInterrumpido: se escriben los resultados parciales "
            "(espera a que acaben los análisis en curso).",
            flush=True,
        )
        pool.shutdown(wait=False, cancel_futures=True)
    else:
        pool.shutdown()

    for e in entries:
        if e.idx not in results:
            results[e.idx] = {
                "idx": e.idx,
                "name": e.name,
                "url": e.url,
                "status": "pendiente",
                "reason": "no verificado",
                "frames": 0,
                "duration": 0.0,
                "black_pct": 0.0,
                "freeze_s": 0.0,
                "elapsed": 0.0,
                "ffmpeg_rc": None,
                "timed_out": False,
            }

    oks = [e for e in entries if results[e.idx]["status"] == "ok"]
    fails = [e for e in entries if results[e.idx]["status"] == "fail"]
    reasons = (
        {e.idx: results[e.idx]["reason"] for e in fails} if cfg.annotate else None
    )

    write_m3u(cfg.ok, header, oks)

    if not cfg.no_fail_file:
        write_m3u(cfg.fail, header, fails, reasons)

    if not cfg.no_csv:
        write_csv(cfg.report, results)

    print("\n=================== RESUMEN ===================", flush=True)
    print(f"OK:   {len(oks)} -> {cfg.ok}", flush=True)
    if not cfg.no_fail_file:
        print(f"FAIL: {len(fails)} -> {cfg.fail}", flush=True)
    else:
        print(f"FAIL: {len(fails)}", flush=True)

    pend = total - len(oks) - len(fails)
    if pend:
        print(f"PENDIENTE: {pend}", flush=True)
    if not cfg.no_csv:
        print(f"Informe: {cfg.report}", flush=True)

    if not cfg.quiet_summary:
        c = Counter(
            results[e.idx]["reason"].split("(")[0].split(":")[0].strip()
            for e in fails
        )
        if c:
            print("\nMotivos de fallo:", flush=True)
            for k, v in c.most_common():
                print(f"  {v:>4}  {k}", flush=True)

        partial_ok = sum(
            1
            for e in oks
            if "muestreo parcial" in str(results[e.idx].get("reason", ""))
        )
        if partial_ok:
            print(
                f"\nNota: {partial_ok} canal(es) OK con muestreo parcial "
                "(FFmpeg no cerró a tiempo pero sí hubo imagen válida).",
                flush=True,
            )

    # Siempre 0 si el proceso terminó bien: 0 OK es válido para el pipeline
    return 0


if __name__ == "__main__":
    sys.exit(main())