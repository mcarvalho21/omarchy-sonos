#!/usr/bin/env python3
"""Zero-dependency local Sonos -> MPRIS bridge & Control RPC daemon for Omarchy.

Runs as a background service plugin managed by omarchy-shell:
  - Discovers local Sonos speakers via Avahi mDNS (works through UFW) with SSDP fallback
  - Publishes active/paused zone coordinators as standard MPRIS D-Bus players
  - Minimizes idle work while the panel is closed: polls only active coordinators for
    MPRIS, backs off to 30s when idle or away from LAN, and performs zero per-speaker
    volume polling and zero runtime state-file writes while closed
  - Serves fast on-demand state/control/browse RPC over $XDG_RUNTIME_DIR/mc-sonos.sock

CLI usage (called by Panel.qml):
  sonos-mpris.py --rpc '{"cmd":"get_state"}'
  sonos-mpris.py --rpc '{"cmd":"play_pause","coord_uuid":"..."}'
  sonos-mpris.py --status
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import fcntl
import html
import http.client
import http.server
import ipaddress
import json
import math
import os
import re
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace

MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

NS_DIDL = "{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}"
NS_DC = "{http://purl.org/dc/elements/1.1/}"
NS_UPNP = "{urn:schemas-upnp-org:metadata-1-0/upnp/}"
NS_R = "{urn:schemas-rinconnetworks-com:metadata-1-0/}"

MAX_HTTP_BYTES = 1024 * 1024
MAX_DIDL_METADATA_BYTES = 16384
MAX_IDLE_CONNS_PER_IP = 6
SEED_CACHE_MAX_IPS = 32
MAX_ZONE_GROUPS = 64
MAX_SPEAKERS = 64
MAX_MPRIS_PLAYERS = 32
MAX_DURATION_SEC = 86400 * 365
MAX_BROWSE_PAGES = 16
HTTP_TIMEOUT = 2.5
DISCOVERY_INTERVAL = 60.0
FORCE_DISCOVERY_MIN_INTERVAL = 5.0
TOPOLOGY_ACTIVE_INTERVAL = 20.0
TOPOLOGY_IDLE_INTERVAL = 60.0
POLL_ACTIVE_INTERVAL = 2.0
POLL_IDLE_INTERVAL = 30.0
IDLE_SWEEP_INTERVAL = 30.0
SETTINGS_REFRESH_INTERVAL = 10.0
LIBRARY_CACHE_TTL = 45.0
LIBRARY_CACHE_TTL_GENA = 600.0
QUEUE_CACHE_TTL = 15.0
SERVICE_CACHE_RETRY_INTERVAL = 60.0
GENA_DEFAULT_PORT = 14001
GENA_SUB_TIMEOUT_SEC = 600
GENA_RENEW_BEFORE_SEC = 180.0
GENA_PROBE_WAIT_SEC = 2.5
GENA_HEARTBEAT_INTERVAL = 60.0
SUSPEND_DRIFT_SEC = 10.0

_NON_QUEUE_URI_PREFIXES = (
    "x-sonos-vli:",
    "x-sonos-htastream:",
    "x-rincon-stream:",
    "x-sonosapi-stream:",
    "x-sonosapi-radio:",
    "x-sonosapi-hls:",
    "x-sonosapi-hls-static:",
    "x-rincon-mp3radio:",
    "aac:",
    "hls-radio:",
)


def _debug_enabled() -> bool:
    val = (os.environ.get("MC_SONOS_DEBUG") or "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _debug_log(msg: str, exc: BaseException | None = None) -> None:
    if not _debug_enabled():
        return
    ts = time.strftime("%H:%M:%S")
    if exc is not None:
        line = f"[mc.sonos {ts}] {msg}: {type(exc).__name__}: {exc}\n"
    else:
        line = f"[mc.sonos {ts}] {msg}\n"
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except OSError:
        pass


def _boottime() -> float:
    if hasattr(time, "CLOCK_BOOTTIME"):
        try:
            return time.clock_gettime(time.CLOCK_BOOTTIME)
        except OSError:
            pass
    return time.time()


def runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def legacy_state_file_path() -> str:
    return os.path.join(runtime_dir(), "mc-sonos-state.json")


def socket_file_path() -> str:
    return os.path.join(runtime_dir(), "mc-sonos.sock")


def lock_file_path() -> str:
    return os.path.join(runtime_dir(), "mc-sonos.lock")


def seed_cache_path() -> str:
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    if cache_home and os.path.isdir(os.path.dirname(cache_home)):
        return os.path.join(cache_home, "mc-sonos-seeds.json")
    return os.path.join(runtime_dir(), "mc-sonos-seeds.json")


def load_cached_seeds(path: str | None = None) -> tuple[list[str], dict[str, float]]:
    target = path or seed_cache_path()
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.loads(fh.read(32768))
    except (OSError, ValueError):
        return [], {}
    if not isinstance(data, dict):
        return [], {}
    ips: list[str] = []
    seen: set[str] = set()
    raw_ips = data.get("ips")
    if isinstance(raw_ips, list):
        for item in raw_ips:
            if isinstance(item, str) and is_valid_private_ipv4(item) and item not in seen:
                seen.add(item)
                ips.append(item)
                if len(ips) >= SEED_CACHE_MAX_IPS:
                    break
    rtts: dict[str, float] = {}
    raw_rtts = data.get("rtts")
    if isinstance(raw_rtts, dict):
        for ip, ms in raw_rtts.items():
            if (
                isinstance(ip, str)
                and is_valid_private_ipv4(ip)
                and isinstance(ms, (int, float))
                and math.isfinite(ms)
                and 0.5 <= float(ms) <= 10000.0
            ):
                rtts[ip] = float(ms)
    return ips, rtts


def save_cached_seeds(
    ips: list[str], rtts: dict[str, float], path: str | None = None
) -> None:
    clean_ips: list[str] = []
    seen: set[str] = set()
    for ip in ips:
        if is_valid_private_ipv4(ip) and ip not in seen:
            seen.add(ip)
            clean_ips.append(ip)
            if len(clean_ips) >= SEED_CACHE_MAX_IPS:
                break
    if not clean_ips:
        return
    clean_rtts = {
        ip: round(float(ms), 1)
        for ip, ms in rtts.items()
        if ip in seen and math.isfinite(ms) and 0.5 <= float(ms) <= 10000.0
    }
    target = path or seed_cache_path()
    parent = os.path.dirname(target)
    tmp = f"{target}.tmp.{os.getpid()}"
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(
                fd,
                json.dumps({"ips": clean_ips, "rtts": clean_rtts}).encode("utf-8"),
            )
        finally:
            os.close(fd)
        os.replace(tmp, target)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


class _RttTracker:
    """Thread-safe EWMA latency tracker used to route topology/library to the fastest speaker."""

    def __init__(self, alpha: float = 0.35) -> None:
        self._alpha = alpha
        self._lock = threading.Lock()
        self._ewma: dict[str, float] = {}

    def seed(self, rtts: dict[str, float]) -> None:
        with self._lock:
            for ip, ms in rtts.items():
                if ip not in self._ewma and math.isfinite(ms) and ms > 0:
                    self._ewma[ip] = float(ms)

    def record(self, ip: str, elapsed_ms: float) -> None:
        if not ip or not math.isfinite(elapsed_ms) or elapsed_ms < 0:
            return
        sample = max(0.5, min(10000.0, float(elapsed_ms)))
        with self._lock:
            prev = self._ewma.get(ip)
            self._ewma[ip] = (
                sample
                if prev is None
                else (self._alpha * sample + (1.0 - self._alpha) * prev)
            )

    def record_failure(self, ip: str) -> None:
        if not ip:
            return
        with self._lock:
            prev = self._ewma.get(ip, 500.0)
            self._ewma[ip] = min(10000.0, max(1500.0, prev * 2.0))

    def rank(self, ips: list[str]) -> list[str]:
        unique = list(dict.fromkeys(ips))
        with self._lock:
            snap = dict(self._ewma)
        return sorted(unique, key=lambda ip: snap.get(ip, 500.0))

    def pick_preferred(
        self,
        ips: list[str],
        current: str = "",
        *,
        switch_ratio: float = 0.75,
        min_delta_ms: float = 15.0,
    ) -> str:
        """Pick the preferred speaker IP with hysteresis to keep album-art URLs & GENA stable."""
        unique = list(dict.fromkeys(ips))
        if not unique:
            return ""
        with self._lock:
            snap = dict(self._ewma)
        ranked = sorted(unique, key=lambda ip: snap.get(ip, 500.0))
        best = ranked[0]
        if current and current in unique and best != current:
            cur_ms = snap.get(current, 500.0)
            best_ms = snap.get(best, 500.0)
            if not (
                best_ms < cur_ms * switch_ratio
                and (cur_ms - best_ms) >= min_delta_ms
            ):
                return current
        return best

    def rank_with_preferred(
        self,
        ips: list[str],
        current: str = "",
        *,
        switch_ratio: float = 0.75,
        min_delta_ms: float = 15.0,
    ) -> list[str]:
        unique = list(dict.fromkeys(ips))
        if not unique:
            return []
        pref = self.pick_preferred(
            unique,
            current=current,
            switch_ratio=switch_ratio,
            min_delta_ms=min_delta_ms,
        )
        ranked = self.rank(unique)
        if pref and pref in ranked and ranked[0] != pref:
            ranked.remove(pref)
            ranked.insert(0, pref)
        return ranked

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._ewma)


_RTT = _RttTracker()


def set_parent_death_signal() -> None:
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


_LAN_IPV4_NETWORKS = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),
)


def is_valid_private_ipv4(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value)
        if not isinstance(addr, ipaddress.IPv4Address):
            return False
        if (
            addr.is_loopback
            or addr.is_unspecified
            or addr.is_multicast
            or addr.is_reserved
        ):
            return False
        return any(addr in net for net in _LAN_IPV4_NETWORKS)
    except ValueError:
        return False


def _xml_fromstring(text: str, *, max_bytes: int = MAX_HTTP_BYTES) -> ET.Element:
    """Parse XML strictly bounded in size and rejecting DTD/ENTITY declarations."""
    if not text or len(text) > max_bytes:
        raise ET.ParseError("empty or oversized XML")
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ET.ParseError("DTD/ENTITY declarations not allowed")
    return ET.fromstring(text)


def parse_hms_to_sec(text: str) -> int:
    if not text or text in ("NOT_IMPLEMENTED", "0:00:00", "00:00:00"):
        return 0
    parts = text.strip().split(":")
    if len(parts) != 3:
        return 0
    try:
        h = int(parts[0])
        m = int(parts[1])
        s = float(parts[2])
        if h < 0 or m < 0 or s < 0 or not math.isfinite(s):
            return 0
        total = h * 3600 + m * 60 + int(s)
        return min(total, MAX_DURATION_SEC)
    except (ValueError, OverflowError):
        return 0


def format_sec_to_hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def xml_escape(text: str) -> str:
    return html.escape(text or "", quote=True)


def compute_poll_interval(any_playing: bool, has_speakers: bool) -> float:
    if not has_speakers or not any_playing:
        return POLL_IDLE_INTERVAL
    return POLL_ACTIVE_INTERVAL


class _HttpPool:
    """Thread-safe HTTP/1.1 keep-alive connection pool keyed by speaker IP."""

    def __init__(self, max_per_ip: int = MAX_IDLE_CONNS_PER_IP) -> None:
        self._max_per_ip = max_per_ip
        self._lock = threading.Lock()
        self._idle: dict[str, list[http.client.HTTPConnection]] = {}

    def _acquire(self, ip: str) -> tuple[http.client.HTTPConnection, bool]:
        with self._lock:
            bucket = self._idle.get(ip)
            if bucket:
                return bucket.pop(), True
        return http.client.HTTPConnection(ip, 1400, timeout=HTTP_TIMEOUT), False

    def _release(self, ip: str, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            bucket = self._idle.setdefault(ip, [])
            if len(bucket) < self._max_per_ip:
                bucket.append(conn)
                return
        try:
            conn.close()
        except Exception:
            pass

    def close_all(self) -> None:
        with self._lock:
            buckets = list(self._idle.values())
            self._idle.clear()
        for bucket in buckets:
            for conn in bucket:
                try:
                    conn.close()
                except Exception:
                    pass

    def post(self, ip: str, path: str, headers: dict[str, str], payload: bytes) -> str:
        conn, reused = self._acquire(ip)
        for attempt in range(2):
            try:
                conn.request("POST", path, body=payload, headers=headers)
                resp = conn.getresponse()
                raw = resp.read(MAX_HTTP_BYTES)
                status = resp.status
                reason = resp.reason
                reusable = (not resp.will_close) and resp.isclosed() and status < 400
            except (TimeoutError, socket.timeout):
                try:
                    conn.close()
                except Exception:
                    pass
                # Never retry timed-out requests (avoids 2x latency stalls and duplicate actions).
                raise
            except (
                http.client.RemoteDisconnected,
                http.client.BadStatusLine,
                BrokenPipeError,
                ConnectionResetError,
                ConnectionAbortedError,
            ):
                try:
                    conn.close()
                except Exception:
                    pass
                # Retry once only when a reused keep-alive connection went stale.
                if reused and attempt == 0:
                    conn = http.client.HTTPConnection(ip, 1400, timeout=HTTP_TIMEOUT)
                    reused = False
                    continue
                raise
            except (http.client.HTTPException, OSError):
                try:
                    conn.close()
                except Exception:
                    pass
                raise
            if reusable:
                self._release(ip, conn)
            else:
                try:
                    conn.close()
                except Exception:
                    pass
            if status >= 400:
                raise http.client.HTTPException(
                    f"HTTP {status} {reason} for http://{ip}:1400{path}"
                )
            return raw.decode("utf-8", "replace")
        raise OSError("SOAP request failed")


_HTTP_POOL = _HttpPool()


def soap_call(ip: str, path: str, service: str, action: str, body: str = "") -> str:
    payload = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}:1">'
        f"{body}</u:{action}></s:Body></s:Envelope>"
    ).encode("utf-8")
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"urn:schemas-upnp-org:service:{service}:1#{action}"',
        "Connection": "keep-alive",
        "Content-Length": str(len(payload)),
    }
    t0 = time.monotonic()
    try:
        resp = _HTTP_POOL.post(ip, path, headers, payload)
    except Exception:
        _RTT.record_failure(ip)
        raise
    _RTT.record(ip, (time.monotonic() - t0) * 1000.0)
    return resp


def discover_via_avahi(
    timeout: float = 3.5, first_hit_grace: float = 0.08
) -> list[str]:
    """Stream avahi-browse and return shortly after the first valid Sonos IPv4.

    A single speaker IP is enough for GetZoneGroupState to discover the whole house,
    so returning right after the cached mDNS burst avoids avahi-browse -t's ~1s wait.
    Uses raw unbuffered os.read() with selectors so buffered lines are never missed.
    """
    ips: list[str] = []
    seen: set[str] = set()
    try:
        proc = subprocess.Popen(
            ["avahi-browse", "-rpk", "_sonos._tcp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError:
        return ips

    sel = selectors.DefaultSelector()
    try:
        if proc.stdout is None:
            return ips
        fd = proc.stdout.fileno()
        sel.register(fd, selectors.EVENT_READ)
        deadline = time.monotonic() + max(0.2, timeout)
        buf = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            events = sel.select(timeout=remaining)
            if not events:
                break
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw_line, buf = buf.split(b"\n", 1)
                line = raw_line.decode("utf-8", "replace")
                if not line.startswith("=;"):
                    continue
                fields = line.split(";")
                if len(fields) < 8:
                    continue
                ip = fields[7].strip()
                if is_valid_private_ipv4(ip) and ip not in seen:
                    seen.add(ip)
                    ips.append(ip)
                    if len(ips) == 1:
                        deadline = min(deadline, time.monotonic() + first_hit_grace)
    except OSError:
        pass
    finally:
        try:
            sel.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=0.5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=0.5)
            except Exception:
                pass
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass
    return ips


def discover_via_ssdp() -> list[str]:
    ips: list[str] = []
    seen: set[str] = set()
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n\r\n"
    ).encode("ascii")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.sendto(msg, ("239.255.255.250", 1900))
        deadline = time.monotonic() + 2.5
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            low = data.lower()
            if b"sonos" not in low and b"zoneplayer" not in low:
                continue
            ip = addr[0]
            if is_valid_private_ipv4(ip) and ip not in seen:
                seen.add(ip)
                ips.append(ip)
    except OSError:
        pass
    finally:
        sock.close()
    return ips


def discover_sonos_ips() -> list[str]:
    return discover_via_avahi() or discover_via_ssdp()


@dataclass(frozen=True)
class SpeakerInfo:
    uuid: str
    ip: str
    name: str
    coord_uuid: str
    is_coordinator: bool


@dataclass(frozen=True)
class ZoneGroup:
    coord_uuid: str
    coord_ip: str
    coord_name: str
    member_uuids: tuple[str, ...]
    member_names: tuple[str, ...]

    @property
    def room_label(self) -> str:
        extra = len(self.member_names) - 1
        if extra > 0:
            return f"{self.coord_name} (+{extra})"
        return self.coord_name

    @property
    def bus_suffix(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9_]+", "_", self.coord_uuid).strip("_")
        return safe[-16:] if len(safe) > 16 else (safe or "default")


@dataclass(frozen=True)
class TrackState:
    playback_status: str  # Playing | Paused | Stopped
    title: str
    artist: str
    album: str
    art_url: str
    source_name: str
    source_icon: str
    duration_sec: int
    position_sec: int
    track_num: int
    shuffle: bool
    repeat: str  # none | all | one
    group_volume: int
    group_muted: bool
    can_next: bool
    can_prev: bool
    position_at: float = 0.0
    current_uri: str = ""
    was_transitioning: bool = False

    @property
    def duration_us(self) -> int:
        return self.duration_sec * 1_000_000

    @property
    def position_us(self) -> int:
        return self.position_sec * 1_000_000

    @property
    def has_track(self) -> bool:
        return bool(self.title or self.artist)


def extrapolate_position_sec(state: TrackState, now: float | None = None) -> int:
    if (
        state.playback_status == "Playing"
        and state.duration_sec > 0
        and state.position_at > 0.0
    ):
        cur = time.monotonic() if now is None else now
        elapsed = max(0, int(cur - state.position_at))
        return min(state.duration_sec, state.position_sec + elapsed)
    return state.position_sec


_SERVICE_NAMES_LOCK = threading.Lock()
_SERVICE_NAMES_CACHE: dict[str, str] = {}
_SERVICE_NAMES_FETCHED = False
_SERVICE_NAMES_LAST_ATTEMPT = 0.0

KNOWN_SERVICE_NAMES: dict[str, str] = {
    "9": "Spotify",
    "12": "Spotify",
    "160": "SoundCloud",
    "174": "Tidal",
    "201": "Amazon Music",
    "204": "Apple Music",
    "231": "YouTube Music",
    "254": "TuneIn",
    "284": "YouTube Music",
    "303": "Sonos Radio",
}


def fetch_music_services(ip: str) -> dict[str, str]:
    global _SERVICE_NAMES_CACHE, _SERVICE_NAMES_FETCHED, _SERVICE_NAMES_LAST_ATTEMPT
    now = time.monotonic()
    with _SERVICE_NAMES_LOCK:
        if _SERVICE_NAMES_FETCHED and _SERVICE_NAMES_CACHE:
            return dict(_SERVICE_NAMES_CACHE)
        if not ip or (
            _SERVICE_NAMES_LAST_ATTEMPT > 0.0
            and (now - _SERVICE_NAMES_LAST_ATTEMPT) < SERVICE_CACHE_RETRY_INTERVAL
        ):
            return dict(_SERVICE_NAMES_CACHE or KNOWN_SERVICE_NAMES)
        _SERVICE_NAMES_LAST_ATTEMPT = now

    result = dict(KNOWN_SERVICE_NAMES)
    fetched = False
    try:
        resp = soap_call(
            ip, "/MusicServices/Control", "MusicServices", "ListAvailableServices"
        )
        desc = _xml_fromstring(resp).findtext(".//AvailableServiceDescriptorList") or ""
        if desc:
            sroot = _xml_fromstring(desc)
            for el in sroot.findall(".//Service"):
                sid = el.attrib.get("Id")
                name = (el.attrib.get("Name") or "").strip()
                if sid and name:
                    result[sid] = name
            fetched = True
    except Exception as exc:
        _debug_log(f"fetch_music_services({ip}) failed", exc)

    with _SERVICE_NAMES_LOCK:
        if fetched:
            _SERVICE_NAMES_CACHE = result
            _SERVICE_NAMES_FETCHED = True
            return dict(_SERVICE_NAMES_CACHE)
        return dict(_SERVICE_NAMES_CACHE or KNOWN_SERVICE_NAMES)


def _cached_music_services() -> dict[str, str]:
    with _SERVICE_NAMES_LOCK:
        return dict(_SERVICE_NAMES_CACHE or KNOWN_SERVICE_NAMES)


def source_icon_for(name: str) -> str:
    low = (name or "").lower()
    if "spotify" in low:
        return ""
    if "youtube" in low:
        return "󰗃"
    if "apple" in low:
        return "󰀵"
    if "soundcloud" in low:
        return ""
    if "airplay" in low:
        return "󰐰"
    if "tv" in low or "hdmi" in low:
        return "󰟴"
    if "line-in" in low:
        return "󰦢"
    if "radio" in low or "tunein" in low:
        return "󰐹"
    if "library" in low:
        return "󰈣"
    return "󰎈"


def detect_audio_source(
    ip: str,
    track_uri: str,
    current_uri: str,
    services: dict[str, str] | None = None,
) -> tuple[str, str]:
    track_uri = (track_uri or "").strip()
    current_uri = (current_uri or "").strip()
    combined = f"{track_uri} {current_uri}".strip()
    low = combined.lower()

    if "x-sonos-vli:" in low:
        if "spotify" in low:
            return "Spotify Connect", source_icon_for("spotify")
        if "airplay" in low:
            return "AirPlay", source_icon_for("airplay")
        if "amazon" in low:
            return "Amazon Music", source_icon_for("amazon")
        if "apple" in low:
            return "Apple Music", source_icon_for("apple")
        return "Direct Control", "󰐰"

    if track_uri.startswith("x-sonos-htastream:") or current_uri.startswith("x-sonos-htastream:"):
        return "TV / HDMI", source_icon_for("tv")
    if track_uri.startswith("x-rincon-stream:") or current_uri.startswith("x-rincon-stream:"):
        return "Line-In", source_icon_for("line-in")
    if track_uri.startswith("x-file-cifs:"):
        return "Music Library", source_icon_for("library")

    m = re.search(r"[?&]sid=(\d+)", combined)
    if m:
        sid = m.group(1)
        catalog = services if services is not None else fetch_music_services(ip)
        name = catalog.get(sid, f"Service #{sid}")
        return name, source_icon_for(name)

    if "spotify" in low:
        return "Spotify", source_icon_for("spotify")
    if "apple" in low:
        return "Apple Music", source_icon_for("apple")
    if "radio" in low or track_uri.startswith(
        (
            "x-sonosapi-stream:",
            "x-sonosapi-radio:",
            "x-sonosapi-hls:",
            "x-sonosapi-hls-static:",
            "x-rincon-mp3radio:",
            "aac:",
            "hls-radio:",
        )
    ):
        return "Radio", source_icon_for("radio")
    if combined:
        return "Sonos", "󰓃"
    return "", ""


def decode_play_mode(mode: str) -> tuple[bool, str]:
    m = (mode or "NORMAL").upper()
    if m == "SHUFFLE":
        return True, "all"
    if m == "SHUFFLE_NOREPEAT":
        return True, "none"
    if m == "SHUFFLE_REPEAT_ONE":
        return True, "one"
    if m == "REPEAT_ALL":
        return False, "all"
    if m == "REPEAT_ONE":
        return False, "one"
    return False, "none"


def encode_play_mode(shuffle: bool, repeat: str) -> str:
    r = (repeat or "none").lower()
    if shuffle:
        if r == "all":
            return "SHUFFLE"
        if r == "one":
            return "SHUFFLE_REPEAT_ONE"
        return "SHUFFLE_NOREPEAT"
    if r == "all":
        return "REPEAT_ALL"
    if r == "one":
        return "REPEAT_ONE"
    return "NORMAL"


def parse_zone_group_state(state_xml: str) -> tuple[list[ZoneGroup], list[SpeakerInfo]]:
    if not state_xml:
        return [], []
    try:
        zroot = _xml_fromstring(state_xml)
    except ET.ParseError:
        return [], []

    groups: list[ZoneGroup] = []
    speakers: list[SpeakerInfo] = []
    seen_coords: set[str] = set()
    seen_speakers: set[str] = set()
    for zg in zroot.findall(".//ZoneGroup"):
        if len(groups) >= MAX_ZONE_GROUPS or len(speakers) >= MAX_SPEAKERS:
            break
        coord_uuid = (zg.attrib.get("Coordinator") or "").strip()[:128]
        if not coord_uuid or coord_uuid in seen_coords:
            continue
        coord_ip = ""
        coord_name = ""
        member_uuids: list[str] = []
        member_names: list[str] = []
        group_speakers: list[SpeakerInfo] = []
        group_seen: set[str] = set()
        remaining_speakers = MAX_SPEAKERS - len(speakers)
        for m in zg.findall("ZoneGroupMember"):
            if m.attrib.get("Invisible") == "1":
                continue
            uuid = (m.attrib.get("UUID") or "").strip()[:128]
            name = (m.attrib.get("ZoneName") or "").strip()[:120]
            loc = m.attrib.get("Location") or ""
            try:
                mip = urllib.parse.urlparse(loc).hostname or ""
            except ValueError:
                continue
            if (
                not uuid
                or uuid in seen_speakers
                or uuid in group_seen
                or not is_valid_private_ipv4(mip)
            ):
                continue
            is_coord = uuid == coord_uuid
            if len(group_speakers) >= remaining_speakers:
                if not is_coord:
                    continue
                if group_speakers:
                    dropped = group_speakers.pop()
                    member_uuids.pop()
                    member_names.pop()
                    group_seen.discard(dropped.uuid)
            group_seen.add(uuid)
            member_uuids.append(uuid)
            member_names.append(name or mip)
            group_speakers.append(
                SpeakerInfo(
                    uuid=uuid,
                    ip=mip,
                    name=name or mip,
                    coord_uuid=coord_uuid,
                    is_coordinator=is_coord,
                )
            )
            if is_coord:
                coord_ip = mip
                coord_name = name or mip
        if coord_ip and coord_uuid:
            if coord_uuid in member_uuids:
                cidx = member_uuids.index(coord_uuid)
                if cidx > 0:
                    member_uuids.insert(0, member_uuids.pop(cidx))
                    member_names.insert(0, member_names.pop(cidx))
            seen_coords.add(coord_uuid)
            seen_speakers.update(group_seen)
            speakers.extend(group_speakers)
            groups.append(
                ZoneGroup(
                    coord_uuid=coord_uuid,
                    coord_ip=coord_ip,
                    coord_name=coord_name,
                    member_uuids=tuple(member_uuids),
                    member_names=tuple(member_names),
                )
            )
    groups.sort(key=lambda g: g.coord_name.lower())
    speakers.sort(key=lambda s: s.name.lower())
    return groups, speakers


def _fetch_topology_from_ip(ip: str) -> tuple[list[ZoneGroup], list[SpeakerInfo]]:
    try:
        resp = soap_call(
            ip,
            "/ZoneGroupTopology/Control",
            "ZoneGroupTopology",
            "GetZoneGroupState",
        )
        root = _xml_fromstring(resp)
        state_xml = root.findtext(".//ZoneGroupState")
        if not state_xml:
            return [], []
        return parse_zone_group_state(state_xml)
    except Exception as exc:
        _debug_log(f"fetch_topology({ip}) failed", exc)
        return [], []


def fetch_topology(
    seed_ips: list[str],
    *,
    pool: concurrent.futures.ThreadPoolExecutor | None = None,
) -> tuple[list[ZoneGroup], list[SpeakerInfo]]:
    unique_ips = list(dict.fromkeys(seed_ips))
    if not unique_ips:
        return [], []
    # Fast path: query the top-ranked preferred speaker first.
    groups, speakers = _fetch_topology_from_ip(unique_ips[0])
    if groups:
        return groups, speakers
    rest = unique_ips[1:5]
    if not rest:
        return [], []
    # Parallel bounded fallback across up to 4 additional seeds avoids multi-second
    # sequential stalls when off-LAN or after a speaker reboot.
    if pool is not None:
        futs = [pool.submit(_fetch_topology_from_ip, ip) for ip in rest]
        for fut in concurrent.futures.as_completed(futs):
            try:
                g, s = fut.result()
                if g:
                    return g, s
            except Exception:
                continue
        return [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(rest)) as ex:
        futs = [ex.submit(_fetch_topology_from_ip, ip) for ip in rest]
        for fut in concurrent.futures.as_completed(futs):
            try:
                g, s = fut.result()
                if g:
                    return g, s
            except Exception:
                continue
    return [], []


_THIS_NETWORK_IPV4 = ipaddress.IPv4Network("0.0.0.0/8")


def _is_blocked_art_ip(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if (
        addr.is_loopback
        or addr.is_unspecified
        or addr.is_multicast
        or addr.is_reserved
    ):
        return True
    if isinstance(addr, ipaddress.IPv4Address) and addr in _THIS_NETWORK_IPV4:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return _is_blocked_art_ip(addr.ipv4_mapped)
    return False


def resolve_art_url(ip: str, raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "\\" in raw or any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        return ""
    if raw.startswith("/") and not raw.startswith("//"):
        return f"http://{ip}:1400{raw}" if is_valid_private_ipv4(ip) else ""
    try:
        parsed = urllib.parse.urlparse(raw)
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    if port is not None and port <= 0:
        return ""
    if not parsed.netloc.isascii():
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if (
        not host
        or not host.isascii()
        or "%" in host
        or host == "localhost"
        or host.endswith(".localhost")
    ):
        return ""
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Strict ASCII hostname characters only (eliminates IDNA2003 vs UTS#46
        # parser differentials and malformed netloc syntax).
        if not re.fullmatch(r"[a-z0-9.-]+", host):
            return ""
        # Also block non-canonical IPv4 literals accepted by inet_aton / QUrl
        # (e.g. 127.1, 2130706433, 0x7f000001, 0177.0.0.1, 0).
        try:
            addr = ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            addr = None
    if addr is not None and _is_blocked_art_ip(addr):
        return ""
    return raw


def parse_didl_metadata(ip: str, meta_xml: str) -> tuple[str, str, str, str]:
    if not meta_xml or meta_xml == "NOT_IMPLEMENTED":
        return "", "", "", ""
    try:
        mroot = _xml_fromstring(meta_xml, max_bytes=MAX_HTTP_BYTES)
    except ET.ParseError:
        return "", "", "", ""

    title = (mroot.findtext(f".//{NS_DC}title") or "").strip()
    artist = (mroot.findtext(f".//{NS_DC}creator") or "").strip()
    album = (mroot.findtext(f".//{NS_UPNP}album") or "").strip()
    art = resolve_art_url(ip, mroot.findtext(f".//{NS_UPNP}albumArtURI") or "")
    stream = (mroot.findtext(f".//{NS_R}streamContent") or "").strip()

    if stream and (not artist or re.search(r"\.(mp3|aac|m3u8?|pls)$", title, re.I)):
        if " - " in stream:
            s_artist, s_title = stream.split(" - ", 1)
            artist = s_artist.strip() or artist
            title = s_title.strip() or title
        elif not title or re.search(r"\.(mp3|aac|m3u8?|pls)$", title, re.I):
            title = stream

    return title, artist, album, art


def _parse_int_tag(
    xml_text: str | None,
    tag: str,
    min_val: int = 0,
    max_val: int = 100,
    default: int = 0,
) -> int:
    if not xml_text:
        return default
    try:
        raw = _xml_fromstring(xml_text).findtext(f".//{tag}")
        if raw is not None:
            return max(min_val, min(max_val, int(raw)))
    except Exception:
        pass
    return default


def _parse_bool_tag(
    xml_text: str | None, tag: str, default: bool = False
) -> bool:
    if not xml_text:
        return default
    try:
        raw = _xml_fromstring(xml_text).findtext(f".//{tag}")
        if raw is not None:
            return raw.strip() in ("1", "true", "True")
    except Exception:
        pass
    return default


def _parse_transport_status(xml_text: str) -> str:
    tstate = (_xml_fromstring(xml_text).findtext(".//CurrentTransportState") or "").strip()
    if tstate in ("PLAYING", "TRANSITIONING"):
        return "Playing"
    if tstate == "PAUSED_PLAYBACK":
        return "Paused"
    return "Stopped"


def _stopped_track_state() -> TrackState:
    return TrackState(
        playback_status="Stopped",
        title="",
        artist="",
        album="",
        art_url="",
        source_name="",
        source_icon="",
        duration_sec=0,
        position_sec=0,
        track_num=0,
        shuffle=False,
        repeat="none",
        group_volume=0,
        group_muted=False,
        can_next=False,
        can_prev=False,
    )


def _parse_position_info(ip: str, xml_text: str | None) -> tuple[str, str, str, str, str, int, int, int]:
    title = artist = album = art_url = track_uri = ""
    duration_sec = position_sec = track_num = 0
    if not xml_text:
        return title, artist, album, art_url, track_uri, duration_sec, position_sec, track_num
    try:
        proot = _xml_fromstring(xml_text)
        track_uri = (proot.findtext(".//TrackURI") or "").strip()
        meta_xml = proot.findtext(".//TrackMetaData") or ""
        duration_sec = parse_hms_to_sec(proot.findtext(".//TrackDuration") or "")
        position_sec = parse_hms_to_sec(proot.findtext(".//RelTime") or "")
        raw_track = proot.findtext(".//Track") or "0"
        try:
            track_num = max(0, int(raw_track))
        except ValueError:
            track_num = 0
        title, artist, album, art_url = parse_didl_metadata(ip, meta_xml)
    except Exception:
        pass
    return title, artist, album, art_url, track_uri, duration_sec, position_sec, track_num


def _parse_media_info(ip: str, xml_text: str | None) -> tuple[str, str, str, str, str]:
    if not xml_text:
        return "", "", "", "", ""
    try:
        mroot = _xml_fromstring(xml_text)
        current_uri = (mroot.findtext(".//CurrentURI") or "").strip()
        uri_meta = mroot.findtext(".//CurrentURIMetaData") or ""
        m_title, m_artist, m_album, m_art = parse_didl_metadata(ip, uri_meta)
        return current_uri, m_title, m_artist, m_album, m_art
    except Exception:
        return "", "", "", "", ""


def _parse_play_mode_xml(xml_text: str | None) -> tuple[bool, str]:
    if not xml_text:
        return False, "none"
    try:
        play_mode = _xml_fromstring(xml_text).findtext(".//PlayMode") or "NORMAL"
        return decode_play_mode(play_mode)
    except Exception:
        return False, "none"


def _assemble_track_state(
    group: ZoneGroup,
    status: str,
    pos_xml: str | None,
    media_xml: str | None,
    settings_xml: str | None,
    vol_xml: str | None,
    mute_xml: str | None,
    include_audio: bool,
    *,
    pos_parsed: tuple[str, str, str, str, str, int, int, int] | None = None,
    fallback_play_mode: tuple[bool, str] | None = None,
    fallback_current_uri: str = "",
    fallback_audio: tuple[int, bool] | None = None,
    now: float | None = None,
) -> TrackState:
    ip = group.coord_ip
    title, artist, album, art_url, track_uri, duration_sec, position_sec, track_num = (
        pos_parsed if pos_parsed is not None else _parse_position_info(ip, pos_xml)
    )
    current_uri = ""
    if media_xml:
        current_uri, m_title, m_artist, m_album, m_art = _parse_media_info(ip, media_xml)
        title = title or m_title
        artist = artist or m_artist
        album = album or m_album
        art_url = art_url or m_art
    elif (
        fallback_current_uri
        and track_uri
        and not track_uri.startswith(_NON_QUEUE_URI_PREFIXES)
    ):
        current_uri = fallback_current_uri

    can_next = can_prev = True
    is_tv_or_linein = track_uri.startswith(
        ("x-sonos-htastream:", "x-rincon-stream:")
    ) or current_uri.startswith(("x-sonos-htastream:", "x-rincon-stream:"))
    if is_tv_or_linein:
        can_next = False
        can_prev = False
        if not title:
            title = (
                "TV Audio"
                if ("htastream" in track_uri or "htastream" in current_uri)
                else "Line-In"
            )

    source_name, source_icon = detect_audio_source(ip, track_uri, current_uri)
    if settings_xml is not None:
        shuffle, repeat = _parse_play_mode_xml(settings_xml)
    elif fallback_play_mode is not None:
        shuffle, repeat = fallback_play_mode
    else:
        shuffle, repeat = False, "none"
    default_vol = fallback_audio[0] if fallback_audio is not None else 0
    default_muted = fallback_audio[1] if fallback_audio is not None else False
    group_vol = (
        _parse_int_tag(vol_xml, "CurrentVolume", default=default_vol)
        if include_audio
        else 0
    )
    group_muted = (
        _parse_bool_tag(mute_xml, "CurrentMute", default=default_muted)
        if include_audio
        else False
    )

    return TrackState(
        playback_status=status,
        title=title[:256],
        artist=artist[:256],
        album=album[:256],
        art_url=art_url[:1024],
        source_name=source_name[:80],
        source_icon=source_icon,
        duration_sec=duration_sec,
        position_sec=position_sec,
        track_num=track_num,
        shuffle=shuffle,
        repeat=repeat,
        group_volume=group_vol,
        group_muted=group_muted,
        can_next=can_next,
        can_prev=can_prev,
        position_at=time.monotonic() if now is None else now,
        current_uri=current_uri[:1024],
    )


def _soap_safe(ip: str, path: str, service: str, action: str, body: str = "") -> str | None:
    try:
        return soap_call(ip, path, service, action, body)
    except Exception as exc:
        _debug_log(f"SOAP {service}#{action} on {ip} failed", exc)
        return None


def _submit_group_detail_calls(
    pool: concurrent.futures.ThreadPoolExecutor,
    group: ZoneGroup,
    *,
    include_settings: bool = True,
    include_media: bool = False,
) -> dict[str, concurrent.futures.Future[str | None]]:
    ip = group.coord_ip
    futs: dict[str, concurrent.futures.Future[str | None]] = {
        "pos": pool.submit(
            _soap_safe,
            ip,
            "/MediaRenderer/AVTransport/Control",
            "AVTransport",
            "GetPositionInfo",
            "<InstanceID>0</InstanceID>",
        ),
    }
    if include_settings:
        futs["settings"] = pool.submit(
            _soap_safe,
            ip,
            "/MediaRenderer/AVTransport/Control",
            "AVTransport",
            "GetTransportSettings",
            "<InstanceID>0</InstanceID>",
        )
    if include_media:
        futs["media"] = pool.submit(
            _soap_safe,
            ip,
            "/MediaRenderer/AVTransport/Control",
            "AVTransport",
            "GetMediaInfo",
            "<InstanceID>0</InstanceID>",
        )
    if len(group.member_uuids) > 1:
        futs["vol"] = pool.submit(
            _soap_safe,
            ip,
            "/MediaRenderer/GroupRenderingControl/Control",
            "GroupRenderingControl",
            "GetGroupVolume",
            "<InstanceID>0</InstanceID>",
        )
        futs["mute"] = pool.submit(
            _soap_safe,
            ip,
            "/MediaRenderer/GroupRenderingControl/Control",
            "GroupRenderingControl",
            "GetGroupMute",
            "<InstanceID>0</InstanceID>",
        )
    else:
        futs["vol"] = pool.submit(
            _soap_safe,
            ip,
            "/MediaRenderer/RenderingControl/Control",
            "RenderingControl",
            "GetVolume",
            "<InstanceID>0</InstanceID><Channel>Master</Channel>",
        )
        futs["mute"] = pool.submit(
            _soap_safe,
            ip,
            "/MediaRenderer/RenderingControl/Control",
            "RenderingControl",
            "GetMute",
            "<InstanceID>0</InstanceID><Channel>Master</Channel>",
        )
    return futs


def fetch_groups_batch(
    groups: list[ZoneGroup],
    speakers: list[SpeakerInfo],
    pool: concurrent.futures.ThreadPoolExecutor,
    *,
    include_speakers: bool = True,
    known_active_uuids: set[str] | None = None,
    prev_states: dict[str, TrackState] | None = None,
    prev_speaker_audio: dict[str, tuple[int, bool]] | None = None,
    media_info_hints: set[str] | None = None,
    selected_coord_uuid: str = "",
    refresh_all_settings: bool = True,
) -> tuple[dict[str, TrackState], dict[str, tuple[int, bool]], bool]:
    """Fetch group states (and optionally per-speaker audio) using flat leaf SOAP calls.

    When known_active_uuids is None, fetches full details and audio for every group
    in a single parallel batch. Speculatively includes GetMediaInfo in Phase 1 for
    groups in media_info_hints and skips redundant GetMediaInfo on already-stopped
    idle rooms, collapsing multi-wave polling into 1 LAN round-trip.
    """
    group_states: dict[str, TrackState] = {}
    speaker_audio: dict[str, tuple[int, bool]] = {}
    failed_any = False

    if not groups:
        return group_states, speaker_audio, False

    full_all = known_active_uuids is None
    transport_futs: dict[str, concurrent.futures.Future[str | None]] = {}
    detail_futs: dict[str, dict[str, concurrent.futures.Future[str | None]]] = {}
    for g in groups:
        transport_futs[g.coord_uuid] = pool.submit(
            _soap_safe,
            g.coord_ip,
            "/MediaRenderer/AVTransport/Control",
            "AVTransport",
            "GetTransportInfo",
            "<InstanceID>0</InstanceID>",
        )
        if full_all or (known_active_uuids is not None and g.coord_uuid in known_active_uuids):
            has_prev = prev_states is not None and g.coord_uuid in prev_states
            need_settings = (
                refresh_all_settings
                or not selected_coord_uuid
                or g.coord_uuid == selected_coord_uuid
                or not has_prev
            )
            spec_media = media_info_hints is not None and g.coord_uuid in media_info_hints
            detail_futs[g.coord_uuid] = _submit_group_detail_calls(
                pool,
                g,
                include_settings=need_settings,
                include_media=spec_media,
            )

    single_coords = {g.coord_uuid for g in groups if len(g.member_uuids) == 1}
    speaker_futs: dict[
        str, tuple[concurrent.futures.Future[str | None], concurrent.futures.Future[str | None]]
    ] = {}
    if include_speakers:
        for s in speakers:
            if s.is_coordinator and s.coord_uuid in single_coords:
                continue
            speaker_futs[s.uuid] = (
                pool.submit(
                    _soap_safe,
                    s.ip,
                    "/MediaRenderer/RenderingControl/Control",
                    "RenderingControl",
                    "GetVolume",
                    "<InstanceID>0</InstanceID><Channel>Master</Channel>",
                ),
                pool.submit(
                    _soap_safe,
                    s.ip,
                    "/MediaRenderer/RenderingControl/Control",
                    "RenderingControl",
                    "GetMute",
                    "<InstanceID>0</InstanceID><Channel>Master</Channel>",
                ),
            )

    statuses: dict[str, str] = {}
    newly_active_details: dict[str, dict[str, concurrent.futures.Future[str | None]]] = {}
    for g in groups:
        tresp = transport_futs[g.coord_uuid].result()
        if not tresp:
            failed_any = True
            continue
        try:
            status = _parse_transport_status(tresp)
        except Exception:
            failed_any = True
            continue
        statuses[g.coord_uuid] = status
        if g.coord_uuid not in detail_futs:
            if status == "Stopped":
                prev = prev_states.get(g.coord_uuid) if prev_states else None
                if prev is not None:
                    group_states[g.coord_uuid] = replace(
                        _stopped_track_state(),
                        group_volume=prev.group_volume,
                        group_muted=prev.group_muted,
                        shuffle=prev.shuffle,
                        repeat=prev.repeat,
                    )
                else:
                    group_states[g.coord_uuid] = _stopped_track_state()
            else:
                spec_media = media_info_hints is not None and g.coord_uuid in media_info_hints
                newly_active_details[g.coord_uuid] = _submit_group_detail_calls(
                    pool, g, include_settings=True, include_media=spec_media
                )

    detail_futs.update(newly_active_details)

    # Collect detail responses and only issue Phase 2 GetMediaInfo if not already fetched.
    collected: dict[
        str,
        tuple[
            tuple[str, str, str, str, str, int, int, int],
            str | None,
            str | None,
            str | None,
            str | None,
        ],
    ] = {}
    media_futs: dict[str, concurrent.futures.Future[str | None]] = {}
    for g in groups:
        if g.coord_uuid not in statuses or g.coord_uuid not in detail_futs:
            continue
        futs = detail_futs[g.coord_uuid]
        pos_xml = futs["pos"].result()
        if pos_xml is None:
            failed_any = True
            continue
        settings_xml = futs["settings"].result() if "settings" in futs else None
        vol_xml = futs["vol"].result()
        mute_xml = futs["mute"].result()
        media_xml_p1 = futs["media"].result() if "media" in futs else None
        pos_parsed = _parse_position_info(g.coord_ip, pos_xml)
        title, _, _, _, track_uri, _, _, _ = pos_parsed
        collected[g.coord_uuid] = (pos_parsed, settings_xml, vol_xml, mute_xml, media_xml_p1)

        needs_media = not title or not track_uri
        prev = prev_states.get(g.coord_uuid) if prev_states else None
        already_idle = (
            statuses[g.coord_uuid] == "Stopped"
            and not title
            and not track_uri
            and prev is not None
            and prev.playback_status == "Stopped"
            and not prev.title
        )
        if needs_media and not already_idle:
            if "media" not in futs:
                media_futs[g.coord_uuid] = pool.submit(
                    _soap_safe,
                    g.coord_ip,
                    "/MediaRenderer/AVTransport/Control",
                    "AVTransport",
                    "GetMediaInfo",
                    "<InstanceID>0</InstanceID>",
                )
            if media_info_hints is not None:
                if statuses[g.coord_uuid] != "Stopped":
                    media_info_hints.add(g.coord_uuid)
                else:
                    media_info_hints.discard(g.coord_uuid)
        elif media_info_hints is not None:
            media_info_hints.discard(g.coord_uuid)

    for g in groups:
        if g.coord_uuid not in collected:
            continue
        pos_parsed, settings_xml, vol_xml, mute_xml, media_xml_p1 = collected[g.coord_uuid]
        media_xml = (
            media_futs[g.coord_uuid].result()
            if g.coord_uuid in media_futs
            else media_xml_p1
        )
        prev = prev_states.get(g.coord_uuid) if prev_states else None
        fallback_mode = (prev.shuffle, prev.repeat) if prev is not None else None
        fallback_uri = prev.current_uri if prev is not None else ""
        fallback_audio = (
            (prev.group_volume, prev.group_muted) if prev is not None else None
        )
        group_states[g.coord_uuid] = _assemble_track_state(
            group=g,
            status=statuses[g.coord_uuid],
            pos_xml=None,
            media_xml=media_xml,
            settings_xml=settings_xml,
            vol_xml=vol_xml,
            mute_xml=mute_xml,
            include_audio=True,
            pos_parsed=pos_parsed,
            fallback_play_mode=fallback_mode,
            fallback_current_uri=fallback_uri,
            fallback_audio=fallback_audio,
        )

    if include_speakers:
        for s in speakers:
            if s.is_coordinator and s.coord_uuid in single_coords and s.coord_uuid in group_states:
                st = group_states[s.coord_uuid]
                speaker_audio[s.uuid] = (st.group_volume, st.group_muted)
            elif s.uuid in speaker_futs:
                vfut, mfut = speaker_futs[s.uuid]
                v_xml = vfut.result()
                m_xml = mfut.result()
                prev_sp = (
                    prev_speaker_audio.get(s.uuid)
                    if prev_speaker_audio is not None
                    else None
                )
                if v_xml is None and m_xml is None and prev_sp is None:
                    continue
                def_v = prev_sp[0] if prev_sp is not None else 0
                def_m = prev_sp[1] if prev_sp is not None else False
                speaker_audio[s.uuid] = (
                    _parse_int_tag(v_xml, "CurrentVolume", default=def_v),
                    _parse_bool_tag(m_xml, "CurrentMute", default=def_m),
                )

    return group_states, speaker_audio, failed_any


def fetch_speaker_audio(ip: str) -> tuple[int, bool]:
    vresp = _soap_safe(
        ip,
        "/MediaRenderer/RenderingControl/Control",
        "RenderingControl",
        "GetVolume",
        "<InstanceID>0</InstanceID><Channel>Master</Channel>",
    )
    mresp = _soap_safe(
        ip,
        "/MediaRenderer/RenderingControl/Control",
        "RenderingControl",
        "GetMute",
        "<InstanceID>0</InstanceID><Channel>Master</Channel>",
    )
    return _parse_int_tag(vresp, "CurrentVolume"), _parse_bool_tag(mresp, "CurrentMute")


def fetch_group_state(
    group: ZoneGroup,
    include_audio: bool = True,
    skip_stopped_details: bool = False,
) -> TrackState | None:
    ip = group.coord_ip
    try:
        tresp = soap_call(
            ip,
            "/MediaRenderer/AVTransport/Control",
            "AVTransport",
            "GetTransportInfo",
            "<InstanceID>0</InstanceID>",
        )
        status = _parse_transport_status(tresp)
    except Exception:
        return None

    if skip_stopped_details and status == "Stopped":
        return _stopped_track_state()

    pos_xml = _soap_safe(
        ip,
        "/MediaRenderer/AVTransport/Control",
        "AVTransport",
        "GetPositionInfo",
        "<InstanceID>0</InstanceID>",
    )
    media_xml: str | None = None
    title, _, _, _, track_uri, _, _, _ = _parse_position_info(ip, pos_xml)
    if not title or not track_uri:
        media_xml = _soap_safe(
            ip,
            "/MediaRenderer/AVTransport/Control",
            "AVTransport",
            "GetMediaInfo",
            "<InstanceID>0</InstanceID>",
        )

    settings_xml: str | None = None
    if include_audio or status == "Playing":
        settings_xml = _soap_safe(
            ip,
            "/MediaRenderer/AVTransport/Control",
            "AVTransport",
            "GetTransportSettings",
            "<InstanceID>0</InstanceID>",
        )

    vol_xml: str | None = None
    mute_xml: str | None = None
    if include_audio:
        if len(group.member_uuids) > 1:
            vol_xml = _soap_safe(
                ip,
                "/MediaRenderer/GroupRenderingControl/Control",
                "GroupRenderingControl",
                "GetGroupVolume",
                "<InstanceID>0</InstanceID>",
            )
            mute_xml = _soap_safe(
                ip,
                "/MediaRenderer/GroupRenderingControl/Control",
                "GroupRenderingControl",
                "GetGroupMute",
                "<InstanceID>0</InstanceID>",
            )
        else:
            vol_xml = _soap_safe(
                ip,
                "/MediaRenderer/RenderingControl/Control",
                "RenderingControl",
                "GetVolume",
                "<InstanceID>0</InstanceID><Channel>Master</Channel>",
            )
            mute_xml = _soap_safe(
                ip,
                "/MediaRenderer/RenderingControl/Control",
                "RenderingControl",
                "GetMute",
                "<InstanceID>0</InstanceID><Channel>Master</Channel>",
            )

    return _assemble_track_state(
        group=group,
        status=status,
        pos_xml=pos_xml,
        media_xml=media_xml,
        settings_xml=settings_xml,
        vol_xml=vol_xml,
        mute_xml=mute_xml,
        include_audio=include_audio,
    )


def browse_content_directory(
    ip: str,
    object_id: str,
    limit: int = 100,
    *,
    minimal: bool = False,
    raise_on_error: bool = False,
) -> list[dict]:
    items: list[dict] = []
    start = 0
    batch = min(200, max(1, limit))
    pages = 0
    while len(items) < limit and pages < MAX_BROWSE_PAGES:
        pages += 1
        body = (
            f"<ObjectID>{xml_escape(object_id)}</ObjectID>"
            "<BrowseFlag>BrowseDirectChildren</BrowseFlag>"
            "<Filter>*</Filter>"
            f"<StartingIndex>{start}</StartingIndex>"
            f"<RequestedCount>{min(batch, limit - len(items))}</RequestedCount>"
            "<SortCriteria></SortCriteria>"
        )
        try:
            resp = soap_call(
                ip,
                "/MediaServer/ContentDirectory/Control",
                "ContentDirectory",
                "Browse",
                body,
            )
            root = _xml_fromstring(resp)
            returned = int(root.findtext(".//NumberReturned") or "0")
            total = int(root.findtext(".//TotalMatches") or "0")
            res_xml = root.findtext(".//Result") or ""
            if returned <= 0 or not res_xml:
                break
            rroot = _xml_fromstring(res_xml)
            children = list(rroot)[: limit - len(items)]
            if not children:
                break
            for idx, el in enumerate(children):
                title = (el.findtext(f"{NS_DC}title") or "").strip()
                artist = (el.findtext(f"{NS_DC}creator") or "").strip()
                album = (el.findtext(f"{NS_UPNP}album") or "").strip()
                res_el = el.find(f"{NS_DIDL}res")
                dur_attr = res_el.attrib.get("duration", "") if res_el is not None else ""
                if minimal:
                    items.append(
                        {
                            "id": el.attrib.get("id", ""),
                            "index": start + idx + 1,
                            "title": title[:200],
                            "artist": artist[:200],
                            "album": album[:200],
                            "duration_sec": parse_hms_to_sec(dur_attr),
                        }
                    )
                    continue
                art = resolve_art_url(ip, el.findtext(f"{NS_UPNP}albumArtURI") or "")
                uri = ((res_el.text if res_el is not None else "") or "").strip()
                res_md = (el.findtext(f"{NS_R}resMD") or "").strip()
                # Avoid slicing DIDL XML mid-tag; drop oversized metadata instead of corrupting it.
                safe_md = res_md if len(res_md) <= MAX_DIDL_METADATA_BYTES else ""
                desc = (el.findtext(f"{NS_R}description") or "").strip()
                upnp_class = (el.findtext(f"{NS_UPNP}class") or "").strip()
                items.append(
                    {
                        "id": el.attrib.get("id", ""),
                        "index": start + idx + 1,
                        "title": title[:200],
                        "artist": artist[:200],
                        "album": album[:200],
                        "description": desc[:120],
                        "art_url": art[:1024],
                        "uri": uri[:1024],
                        "metadata": safe_md,
                        "kind": "favorite"
                        if object_id == "FV:2"
                        else ("playlist" if object_id == "SQ:" else "track"),
                        "duration_sec": parse_hms_to_sec(dur_attr),
                        "upnp_class": upnp_class,
                    }
                )
            start += max(returned, len(children))
            if start >= total or len(items) >= limit:
                break
        except Exception as exc:
            _debug_log(f"browse_content_directory({ip}, {object_id}) failed", exc)
            if raise_on_error:
                raise
            break
    return items


def play_uri_on_coordinator(
    group: ZoneGroup,
    uri: str,
    metadata: str,
    as_next: bool = False,
    *,
    current_uri: str = "",
    already_playing: bool = False,
) -> str:
    ip = group.coord_ip
    is_container = uri.startswith(
        (
            "x-rincon-cpcontainer:",
            "x-rincon-playlist:",
            "file:///jffs/settings/savedqueues.rsq",
        )
    )
    is_stream = uri.startswith(_NON_QUEUE_URI_PREFIXES)

    if as_next:
        if is_stream:
            raise ValueError("cannot enqueue a live stream as next")
        body = (
            "<InstanceID>0</InstanceID>"
            f"<EnqueuedURI>{xml_escape(uri)}</EnqueuedURI>"
            f"<EnqueuedURIMetaData>{xml_escape(metadata)}</EnqueuedURIMetaData>"
            "<DesiredFirstTrackNumberEnqueued>0</DesiredFirstTrackNumberEnqueued>"
            "<EnqueueAsNext>1</EnqueueAsNext>"
        )
        soap_call(
            ip,
            "/MediaRenderer/AVTransport/Control",
            "AVTransport",
            "AddURIToQueue",
            body,
        )
        return ""

    if is_container or not is_stream:
        queue_uri = f"x-rincon-queue:{group.coord_uuid}#0"
        try:
            soap_call(
                ip,
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "RemoveAllTracksFromQueue",
                "<InstanceID>0</InstanceID>",
            )
            body = (
                "<InstanceID>0</InstanceID>"
                f"<EnqueuedURI>{xml_escape(uri)}</EnqueuedURI>"
                f"<EnqueuedURIMetaData>{xml_escape(metadata)}</EnqueuedURIMetaData>"
                "<DesiredFirstTrackNumberEnqueued>0</DesiredFirstTrackNumberEnqueued>"
                "<EnqueueAsNext>0</EnqueueAsNext>"
            )
            soap_call(
                ip,
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "AddURIToQueue",
                body,
            )
            if current_uri != queue_uri:
                soap_call(
                    ip,
                    "/MediaRenderer/AVTransport/Control",
                    "AVTransport",
                    "SetAVTransportURI",
                    "<InstanceID>0</InstanceID>"
                    f"<CurrentURI>{xml_escape(queue_uri)}</CurrentURI>"
                    "<CurrentURIMetaData></CurrentURIMetaData>",
                )
                soap_call(
                    ip,
                    "/MediaRenderer/AVTransport/Control",
                    "AVTransport",
                    "Play",
                    "<InstanceID>0</InstanceID><Speed>1</Speed>",
                )
            else:
                try:
                    soap_call(
                        ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "Play",
                        "<InstanceID>0</InstanceID><Speed>1</Speed>",
                    )
                except Exception:
                    soap_call(
                        ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "SetAVTransportURI",
                        "<InstanceID>0</InstanceID>"
                        f"<CurrentURI>{xml_escape(queue_uri)}</CurrentURI>"
                        "<CurrentURIMetaData></CurrentURIMetaData>",
                    )
                    soap_call(
                        ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "Play",
                        "<InstanceID>0</InstanceID><Speed>1</Speed>",
                    )
            return queue_uri
        except Exception as exc:
            _debug_log(f"queue play fallback to direct URI on {ip}", exc)

    if current_uri != uri:
        soap_call(
            ip,
            "/MediaRenderer/AVTransport/Control",
            "AVTransport",
            "SetAVTransportURI",
            "<InstanceID>0</InstanceID>"
            f"<CurrentURI>{xml_escape(uri)}</CurrentURI>"
            f"<CurrentURIMetaData>{xml_escape(metadata)}</CurrentURIMetaData>",
        )
        soap_call(
            ip,
            "/MediaRenderer/AVTransport/Control",
            "AVTransport",
            "Play",
            "<InstanceID>0</InstanceID><Speed>1</Speed>",
        )
    elif not already_playing:
        try:
            soap_call(
                ip,
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "Play",
                "<InstanceID>0</InstanceID><Speed>1</Speed>",
            )
        except Exception:
            soap_call(
                ip,
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "SetAVTransportURI",
                "<InstanceID>0</InstanceID>"
                f"<CurrentURI>{xml_escape(uri)}</CurrentURI>"
                f"<CurrentURIMetaData>{xml_escape(metadata)}</CurrentURIMetaData>",
            )
            soap_call(
                ip,
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "Play",
                "<InstanceID>0</InstanceID><Speed>1</Speed>",
            )
    return uri


def build_state_payload(
    groups: list[ZoneGroup],
    speakers: list[SpeakerInfo],
    group_states: dict[str, TrackState],
    speaker_audio: dict[str, tuple[int, bool]],
    *,
    gena_active: bool = False,
    cached: bool = False,
    library_version: int = 0,
    now: float | None = None,
) -> dict:
    cur_now = time.monotonic() if now is None else now
    g_list = []
    for g in groups:
        st = group_states.get(g.coord_uuid)
        if not st:
            continue
        g_list.append(
            {
                "coord_uuid": g.coord_uuid,
                "coord_ip": g.coord_ip,
                "coord_name": g.coord_name,
                "room_label": g.room_label,
                "member_uuids": list(g.member_uuids),
                "member_names": list(g.member_names),
                "playback_status": st.playback_status,
                "title": st.title,
                "artist": st.artist,
                "album": st.album,
                "art_url": st.art_url,
                "source_name": st.source_name,
                "source_icon": st.source_icon,
                "duration_sec": st.duration_sec,
                "position_sec": extrapolate_position_sec(st, cur_now),
                "track_num": st.track_num,
                "shuffle": st.shuffle,
                "repeat": st.repeat,
                "group_volume": st.group_volume,
                "group_muted": st.group_muted,
                "can_next": st.can_next,
                "can_prev": st.can_prev,
            }
        )
    s_list = []
    for s in speakers:
        vol, muted = speaker_audio.get(s.uuid, (0, False))
        s_list.append(
            {
                "uuid": s.uuid,
                "ip": s.ip,
                "name": s.name,
                "coord_uuid": s.coord_uuid,
                "is_coordinator": s.is_coordinator,
                "volume": vol,
                "muted": muted,
            }
        )
    speakers_ready = all(s.uuid in speaker_audio for s in speakers)
    return {
        "ok": True,
        "timestamp": int(time.time()),
        "gena_active": gena_active,
        "speakers_ready": speakers_ready,
        "library_version": library_version,
        "cached": cached,
        "groups": g_list,
        "speakers": s_list,
    }


def send_rpc_client(payload_str: str, *, pretty: bool = False) -> int:
    sock_path = socket_file_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(8.0)
    try:
        sock.connect(sock_path)
        sock.sendall((payload_str.strip() + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
        out = b"".join(chunks).decode("utf-8", "replace").strip()
        if out:
            if pretty:
                try:
                    print(json.dumps(json.loads(out), indent=2))
                except ValueError:
                    print(out)
            else:
                print(out)
        else:
            print(json.dumps({"ok": False, "error": "empty response"}))
        return 0
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    finally:
        sock.close()


def find_group(groups: list[ZoneGroup], coord_uuid: str) -> ZoneGroup | None:
    if not coord_uuid:
        return groups[0] if groups else None
    for g in groups:
        if g.coord_uuid == coord_uuid:
            return g
    return None


def detect_local_ip_for(peer_ip: str) -> str:
    if not is_valid_private_ipv4(peer_ip):
        return ""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((peer_ip, 1400))
        local_ip = sock.getsockname()[0]
        return local_ip if is_valid_private_ipv4(local_ip) else ""
    except OSError:
        return ""
    finally:
        sock.close()


def _xml_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse_gena_properties(xml_body: str) -> dict[str, str]:
    props: dict[str, str] = {}
    if not xml_body:
        return props
    try:
        root = _xml_fromstring(xml_body)
    except ET.ParseError:
        return props
    for prop in root:
        if _xml_local(prop.tag) != "property":
            continue
        for child in prop:
            props[_xml_local(child.tag)] = child.text or ""
    return props


def parse_gena_topology_event(
    xml_body: str,
) -> tuple[list[ZoneGroup], list[SpeakerInfo]] | None:
    props = _parse_gena_properties(xml_body)
    state_xml = props.get("ZoneGroupState", "")
    if not state_xml:
        return None
    groups, speakers = parse_zone_group_state(state_xml)
    return (groups, speakers) if groups else None


def parse_gena_group_rc_event(xml_body: str) -> tuple[int | None, bool | None]:
    props = _parse_gena_properties(xml_body)
    vol: int | None = None
    muted: bool | None = None
    raw_vol = props.get("GroupVolume")
    if raw_vol is not None:
        try:
            vol = max(0, min(100, int(raw_vol.strip())))
        except ValueError:
            pass
    raw_mute = props.get("GroupMute")
    if raw_mute is not None:
        muted = raw_mute.strip() in ("1", "true", "True")
    return vol, muted


def parse_gena_rc_event(xml_body: str) -> tuple[int | None, bool | None]:
    props = _parse_gena_properties(xml_body)
    lc = props.get("LastChange", "")
    if not lc:
        return None, None
    try:
        lroot = _xml_fromstring(lc)
    except ET.ParseError:
        return None, None
    vol: int | None = None
    muted: bool | None = None
    for el in lroot.iter():
        tag = _xml_local(el.tag)
        if tag == "Volume" and el.attrib.get("channel") == "Master":
            try:
                vol = max(0, min(100, int(el.attrib.get("val", ""))))
            except ValueError:
                pass
        elif tag == "Mute" and el.attrib.get("channel") == "Master":
            val = (el.attrib.get("val") or "").strip()
            if val != "":
                muted = val in ("1", "true", "True")
    return vol, muted


def parse_gena_content_directory_event(xml_body: str) -> dict[str, str]:
    """Extract Favorites/SavedQueues version tokens from ContentDirectory/Event XML.

    Ignores unrelated container updates such as Q:0 (playback queue).
    """
    props = _parse_gena_properties(xml_body)
    if not props:
        return {}
    updates: dict[str, str] = {}
    fav_id = (props.get("FavoritesUpdateID") or "").strip()
    if fav_id:
        updates["fav"] = fav_id
    sq_id = (props.get("SavedQueuesUpdateID") or "").strip()
    if sq_id:
        updates["sq"] = sq_id
    cids = (props.get("ContainerUpdateIDs") or "").strip()
    if cids:
        tokens = [t.strip() for t in cids.split(",")]
        for i in range(0, len(tokens) - 1, 2):
            cid, uid = tokens[i], tokens[i + 1]
            if cid == "FV:2" or cid == "SQ:" or cid.startswith("SQ:"):
                updates[cid] = uid
    return updates


def _extract_gena_avtransport_payload(
    ip: str, xml_body: str
) -> tuple[dict[str, str], tuple[str, str, str, str] | None, dict[str, str] | None] | None:
    """Parse GENA AVTransport XML & pre-fetch music services without holding controller locks."""
    props = _parse_gena_properties(xml_body)
    lc = props.get("LastChange", "")
    if not lc:
        return None
    try:
        lroot = _xml_fromstring(lc)
    except ET.ParseError:
        return None

    vals: dict[str, str] = {}
    for el in lroot.iter():
        if "val" in el.attrib:
            vals[_xml_local(el.tag)] = el.attrib["val"]
    if not vals:
        return None

    track_meta: tuple[str, str, str, str] | None = None
    if "CurrentTrackMetaData" in vals:
        track_meta = parse_didl_metadata(
            ip, vals.get("CurrentTrackMetaData") or ""
        )
    uri_meta: tuple[str, str, str, str] | None = None
    if "AVTransportURIMetaData" in vals:
        uri_meta = parse_didl_metadata(
            ip, vals.get("AVTransportURIMetaData") or ""
        )

    services: dict[str, str] | None = None
    track_uri = (vals.get("CurrentTrackURI") or "").strip()
    avt_uri = (vals.get("AVTransportURI") or "").strip()
    if re.search(r"[?&]sid=\d+", f"{track_uri} {avt_uri}"):
        services = fetch_music_services(ip)
    return vals, track_meta, uri_meta, services


def _apply_gena_avtransport_payload(
    ip: str,
    vals: dict[str, str],
    track_meta: tuple[str, str, str, str] | None,
    services: dict[str, str] | None,
    prev: TrackState | None,
    *,
    uri_meta: tuple[str, str, str, str] | None = None,
    now: float | None = None,
) -> tuple[TrackState, bool, bool]:
    cur_now = time.monotonic() if now is None else now
    base = prev if prev is not None else _stopped_track_state()
    status = base.playback_status
    raw_tstate = ""
    was_transitioning = base.was_transitioning
    if "TransportState" in vals:
        raw_tstate = vals["TransportState"].strip()
        if raw_tstate in ("PLAYING", "TRANSITIONING"):
            status = "Playing"
        elif raw_tstate == "PAUSED_PLAYBACK":
            status = "Paused"
        else:
            status = "Stopped"
        was_transitioning = raw_tstate == "TRANSITIONING"

    shuffle, repeat = base.shuffle, base.repeat
    if "CurrentPlayMode" in vals:
        shuffle, repeat = decode_play_mode(vals["CurrentPlayMode"])

    track_num = base.track_num
    if "CurrentTrack" in vals:
        try:
            track_num = max(0, int(vals["CurrentTrack"]))
        except ValueError:
            pass

    duration_sec = base.duration_sec
    if "CurrentTrackDuration" in vals:
        duration_sec = parse_hms_to_sec(vals["CurrentTrackDuration"])

    title = base.title
    artist = base.artist
    album = base.album
    art_url = base.art_url
    source_name = base.source_name
    source_icon = base.source_icon
    can_next = base.can_next
    can_prev = base.can_prev
    current_uri = base.current_uri

    has_meta = (track_meta is not None) or (uri_meta is not None)
    has_track_uri = "CurrentTrackURI" in vals
    has_avt_uri = "AVTransportURI" in vals
    has_uri = has_track_uri or has_avt_uri
    if has_meta or has_uri:
        track_uri = (vals.get("CurrentTrackURI") or "").strip()
        if has_avt_uri:
            current_uri = (vals.get("AVTransportURI") or "").strip()
        elif track_uri.startswith(_NON_QUEUE_URI_PREFIXES) and current_uri.startswith(
            "x-rincon-queue:"
        ):
            current_uri = ""
        if track_meta is not None:
            t_title, t_artist, t_album, t_art = track_meta
            m_title, m_artist, m_album, m_art = uri_meta or ("", "", "", "")
            title = t_title or m_title
            artist = t_artist or m_artist
            album = t_album or m_album
            art_url = t_art or m_art
        elif uri_meta is not None:
            # Partial AVTransportURIMetaData-only events must not wipe existing track metadata.
            m_title, m_artist, m_album, m_art = uri_meta
            title = title or m_title
            artist = artist or m_artist
            album = album or m_album
            art_url = art_url or m_art
        if has_uri:
            can_next = can_prev = True
            is_tv_or_linein = track_uri.startswith(
                ("x-sonos-htastream:", "x-rincon-stream:")
            ) or current_uri.startswith(("x-sonos-htastream:", "x-rincon-stream:"))
            if is_tv_or_linein:
                can_next = False
                can_prev = False
                if not title:
                    title = (
                        "TV Audio"
                        if ("htastream" in track_uri or "htastream" in current_uri)
                        else "Line-In"
                    )
            src_name, src_icon = detect_audio_source(
                ip,
                track_uri,
                current_uri,
                services=(
                    services if services is not None else _cached_music_services()
                ),
            )
            # Avoid clobbering a specific source (e.g. Spotify) with generic "Sonos"
            # when a partial event only carries AVTransportURI (e.g. x-rincon-queue:).
            if has_track_uri:
                if src_name or not (track_uri or current_uri):
                    source_name, source_icon = src_name, src_icon
            elif src_name and (src_name != "Sonos" or not base.source_name):
                source_name, source_icon = src_name, src_icon
            elif not current_uri:
                source_name, source_icon = "", ""

    track_changed = (title != base.title) or (track_num != base.track_num)
    status_changed = status != base.playback_status
    finished_transition = base.was_transitioning and raw_tstate == "PLAYING"
    if track_changed:
        position_sec = 0
        position_at = cur_now
    elif base.playback_status == "Playing" and status != "Playing":
        position_sec = (
            0 if status == "Stopped" else extrapolate_position_sec(base, cur_now)
        )
        position_at = cur_now
    elif (base.playback_status != "Playing" and status == "Playing") or finished_transition:
        position_sec = base.position_sec
        position_at = cur_now
    else:
        position_sec = base.position_sec
        position_at = base.position_at

    updated = TrackState(
        playback_status=status,
        title=title[:256],
        artist=artist[:256],
        album=album[:256],
        art_url=art_url[:1024],
        source_name=source_name[:80],
        source_icon=source_icon,
        duration_sec=duration_sec,
        position_sec=position_sec,
        track_num=track_num,
        shuffle=shuffle,
        repeat=repeat,
        group_volume=base.group_volume,
        group_muted=base.group_muted,
        can_next=can_next,
        can_prev=can_prev,
        position_at=position_at,
        current_uri=current_uri[:1024],
        was_transitioning=was_transitioning,
    )
    need_pos_sync = track_changed or status_changed or finished_transition
    queue_changed = ("NumberOfTracks" in vals) or ("AVTransportURI" in vals)
    return updated, need_pos_sync, queue_changed


def parse_gena_avtransport_event(
    ip: str,
    xml_body: str,
    prev: TrackState | None,
    *,
    now: float | None = None,
) -> tuple[TrackState | None, bool, bool]:
    """Parse an AVTransport GENA LastChange event onto prev TrackState.

    Returns (updated_state, need_position_sync, queue_maybe_changed).
    """
    extracted = _extract_gena_avtransport_payload(ip, xml_body)
    if extracted is None:
        return None, False, False
    vals, track_meta, uri_meta, services = extracted
    return _apply_gena_avtransport_payload(
        ip, vals, track_meta, services, prev, uri_meta=uri_meta, now=now
    )


def _read_chunked_body(
    rfile, max_bytes: int = MAX_HTTP_BYTES
) -> tuple[bytes | None, int]:
    """Read a strictly bounded HTTP/1.1 Transfer-Encoding: chunked body."""
    chunks: list[bytes] = []
    total = 0
    max_line = 4096
    while True:
        size_line = rfile.readline(max_line + 1)
        if not size_line or len(size_line) > max_line:
            return None, 400
        size_token = size_line.split(b";", 1)[0].strip()
        if not size_token:
            return None, 400
        try:
            chunk_size = int(size_token, 16)
        except ValueError:
            return None, 400
        if chunk_size < 0:
            return None, 400
        if chunk_size == 0:
            for _ in range(64):
                trailer = rfile.readline(max_line + 1)
                if not trailer or trailer in (b"\r\n", b"\n"):
                    break
                if len(trailer) > max_line:
                    return None, 400
            return b"".join(chunks), 200
        if total + chunk_size > max_bytes:
            return None, 413
        data = rfile.read(chunk_size)
        if len(data) != chunk_size:
            return None, 400
        term = rfile.readline(3)
        if term not in (b"\r\n", b"\n"):
            return None, 400
        chunks.append(data)
        total += chunk_size


@dataclass
class _GenaSub:
    sid: str
    ip: str
    path: str
    kind: str  # topology | content_directory | avtransport | group_rc | rc
    target_uuid: str
    expires_at: float
    gen: int = 0
    last_seq: int = -1
    seq_lock: threading.Lock = field(
        default_factory=threading.Lock, compare=False, repr=False
    )

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.kind, self.ip, self.path, self.target_uuid)


def _parse_gena_seq(header_val: str | None) -> int | None:
    if not header_val:
        return None
    try:
        seq = int(header_val.strip())
        return seq if seq >= 0 else None
    except ValueError:
        return None


def _is_gena_seq_newer(prev_seq: int, new_seq: int) -> bool:
    if prev_seq < 0:
        return True
    if new_seq == 0:
        return False
    if new_seq > prev_seq:
        return True
    # Handle UPnP 32-bit unsigned SEQ wraparound (4294967295 -> 1).
    return prev_seq >= 0xFFFF0000 and 1 <= new_seq <= 0xFFFF


def _parse_gena_timeout(header_val: str | None) -> int:
    if not header_val:
        return GENA_SUB_TIMEOUT_SEC
    m = re.search(r"Second-(\d+)", header_val, re.I)
    if m:
        return max(120, min(7200, int(m.group(1))))
    return GENA_SUB_TIMEOUT_SEC


class _GenaManager:
    """Auto-detecting UPnP GENA event subscription listener.

    Attempts a single ZoneGroupTopology SUBSCRIBE probe on startup. If the initial
    NOTIFY reaches our LAN-bound HTTP listener (not blocked by UFW), GENA becomes
    active and subscribes to all groups/speakers for real-time push updates. If UFW
    drops the inbound NOTIFY, the listener shuts down cleanly and the daemon stays
    on fast SOAP polling with zero open listening ports.
    """

    def __init__(self, on_event) -> None:
        self._on_event = on_event
        self._lock = threading.Lock()
        self._sub_cond = threading.Condition(self._lock)
        self._sync_lock = threading.Lock()
        self._gen = 0
        self._shutdown = False
        self._inflight_subscribes = 0
        self._closing_servers = 0
        self._state = "unprobed"  # unprobed | probing | active | resetting | unavailable
        self._subs: dict[str, _GenaSub] = {}
        self._server: http.server.ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._local_ip = ""
        self._callback_url = ""
        self._first_notify = threading.Event()
        self._last_notify_at = 0.0

    @property
    def active(self) -> bool:
        with self._lock:
            return self._state == "active"

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def local_ip(self) -> str:
        with self._lock:
            return self._local_ip

    @property
    def callback_url(self) -> str:
        with self._lock:
            return self._callback_url

    def has_kind(self, kind: str) -> bool:
        with self._lock:
            if self._state != "active":
                return False
            return any(sub.kind == kind for sub in self._subs.values())

    def is_sub_current(self, sub: _GenaSub) -> bool:
        with self._lock:
            return (
                not self._shutdown
                and self._gen == sub.gen
                and self._subs.get(sub.sid) is sub
            )

    def _detach_server_locked(self) -> http.server.ThreadingHTTPServer | None:
        srv = self._server
        self._server = None
        self._server_thread = None
        self._local_ip = ""
        self._callback_url = ""
        if srv is not None:
            self._closing_servers += 1
        return srv

    def _close_server(self, srv: http.server.ThreadingHTTPServer | None) -> None:
        if srv is None:
            return
        try:
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass
        finally:
            with self._lock:
                self._closing_servers = max(0, self._closing_servers - 1)
                self._sub_cond.notify_all()

    def _wait_closing_servers_locked(self, timeout: float = 2.0) -> None:
        if self._closing_servers <= 0:
            return
        deadline = time.monotonic() + timeout
        while self._closing_servers > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            self._sub_cond.wait(timeout=remaining)

    def diagnostics(self, now: float | None = None) -> dict:
        cur_now = time.monotonic() if now is None else now
        with self._lock:
            by_kind: dict[str, int] = {}
            for sub in self._subs.values():
                by_kind[sub.kind] = by_kind.get(sub.kind, 0) + 1
            last_age = (
                round(cur_now - self._last_notify_at, 1)
                if self._last_notify_at > 0.0
                else None
            )
            return {
                "state": self._state,
                "active": self._state == "active",
                "local_ip": self._local_ip,
                "callback_url": self._callback_url,
                "subscriptions": len(self._subs),
                "subscriptions_by_kind": by_kind,
                "last_notify_age_sec": last_age,
                "sub_timeout_sec": GENA_SUB_TIMEOUT_SEC,
            }

    def _start_server(
        self, local_ip: str, *, expected_gen: int | None = None
    ) -> bool:
        manager = self

        class _GenaHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 3.0

            def log_message(self, _format: str, *_args) -> None:
                return

            def do_NOTIFY(self) -> None:  # noqa: N802
                peer_ip = self.client_address[0] if self.client_address else ""
                bound_ip = self.server.server_address[0]
                loopback_test = bound_ip == "127.0.0.1" and peer_ip == "127.0.0.1"
                if not loopback_test and not is_valid_private_ipv4(peer_ip):
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return

                sid = (self.headers.get("SID") or "").strip()
                sub = manager.lookup_sid(sid)
                if sub is None:
                    self.send_response(412)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return

                if peer_ip != sub.ip:
                    _debug_log(
                        f"GENA rejected spoofed NOTIFY for {sid} from {peer_ip} (expected {sub.ip})"
                    )
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return

                te = (self.headers.get("Transfer-Encoding") or "").lower()
                if "chunked" in te:
                    body_bytes, err_status = _read_chunked_body(
                        self.rfile, MAX_HTTP_BYTES
                    )
                    if body_bytes is None:
                        _debug_log(
                            f"GENA rejected chunked NOTIFY from {peer_ip} ({err_status})"
                        )
                        self.send_response(err_status)
                        self.send_header("Content-Length", "0")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        return
                else:
                    raw_len = self.headers.get("Content-Length")
                    try:
                        length = int(raw_len) if raw_len is not None else 0
                    except ValueError:
                        length = -1
                    if length < 0 or length > MAX_HTTP_BYTES:
                        self.send_response(413 if length > MAX_HTTP_BYTES else 400)
                        self.send_header("Content-Length", "0")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        return
                    body_bytes = self.rfile.read(length) if length > 0 else b""
                    if len(body_bytes) != length:
                        _debug_log(
                            f"GENA rejected truncated NOTIFY from {peer_ip} ({len(body_bytes)}/{length} bytes)"
                        )
                        self.send_response(400)
                        self.send_header("Content-Length", "0")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        return

                seq = _parse_gena_seq(self.headers.get("SEQ"))
                body_text = body_bytes.decode("utf-8", "replace")
                status_code = 200
                with sub.seq_lock:
                    if not manager.is_sub_current(sub):
                        status_code = 412
                    elif seq is not None and not _is_gena_seq_newer(sub.last_seq, seq):
                        _debug_log(
                            f"GENA dropped stale SEQ={seq} <= {sub.last_seq} for {sub.kind} ({sub.ip})"
                        )
                    else:
                        if seq is not None:
                            sub.last_seq = seq
                        try:
                            manager._on_event(sub, body_text)
                        except Exception as exc:
                            _debug_log(f"GENA on_event({sub.kind}) failed", exc)
                        manager._mark_notify(sub.gen)
                self.send_response(status_code)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

        class _GenaHTTPServer(http.server.ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True
            request_queue_size = 64

            def __init__(self, server_address, handler_cls) -> None:
                self._req_sem = threading.BoundedSemaphore(64)
                super().__init__(server_address, handler_cls)

            def process_request(self, request, client_address) -> None:
                if not self._req_sem.acquire(blocking=False):
                    try:
                        self.shutdown_request(request)
                    except Exception:
                        pass
                    return
                try:
                    super().process_request(request, client_address)
                except Exception:
                    self._req_sem.release()
                    raise

            def process_request_thread(self, request, client_address) -> None:
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    self._req_sem.release()

        try:
            pref_port = int(os.environ.get("MC_SONOS_GENA_PORT", str(GENA_DEFAULT_PORT)))
        except ValueError:
            pref_port = GENA_DEFAULT_PORT
        pref_port = pref_port if 0 <= pref_port <= 65535 else GENA_DEFAULT_PORT

        with self._lock:
            self._wait_closing_servers_locked()
            if (
                self._shutdown
                or self._server is not None
                or (expected_gen is not None and self._gen != expected_gen)
            ):
                return False

        srv: http.server.ThreadingHTTPServer | None = None
        for port in (pref_port, 0) if pref_port != 0 else (0,):
            try:
                srv = _GenaHTTPServer((local_ip, port), _GenaHandler)
                break
            except OSError:
                continue
        if srv is None:
            return False

        bound_port = srv.server_address[1]
        with self._lock:
            if (
                self._shutdown
                or self._server is not None
                or (expected_gen is not None and self._gen != expected_gen)
            ):
                aborted = True
            else:
                aborted = False
                self._server = srv
                self._local_ip = local_ip
                self._callback_url = f"http://{local_ip}:{bound_port}/notify"
        if aborted:
            try:
                srv.server_close()
            except Exception:
                pass
            return False

        t = threading.Thread(
            target=srv.serve_forever,
            kwargs={"poll_interval": 0.5},
            daemon=True,
            name="sonos-gena-http",
        )
        t.start()
        with self._lock:
            if self._server is srv:
                self._server_thread = t
        return True

    def _stop_server(self) -> None:
        with self._lock:
            srv = self._detach_server_locked()
        self._close_server(srv)

    def lookup_sid(self, sid: str, wait_timeout: float = 1.0) -> _GenaSub | None:
        if not sid:
            return None
        with self._lock:
            sub = self._subs.get(sid)
            if sub is not None or self._inflight_subscribes <= 0 or wait_timeout <= 0.0:
                return sub
            # Sonos often sends initial SEQ:0 NOTIFY before Python finishes reading the
            # SUBSCRIBE 200 response; wait briefly for in-flight SUBSCRIBE calls to register SIDs.
            deadline = time.monotonic() + wait_timeout
            while sid not in self._subs and self._inflight_subscribes > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                self._sub_cond.wait(timeout=remaining)
            return self._subs.get(sid)

    def _mark_notify(self, expected_gen: int | None = None) -> None:
        with self._lock:
            if self._shutdown or (
                expected_gen is not None and self._gen != expected_gen
            ):
                return
            self._last_notify_at = time.monotonic()
            self._first_notify.set()

    def _subscribe(
        self,
        ip: str,
        path: str,
        kind: str,
        target_uuid: str,
        *,
        expected_gen: int | None = None,
    ) -> _GenaSub | None:
        with self._lock:
            gen = self._gen if expected_gen is None else expected_gen
            if self._shutdown or gen != self._gen:
                return None
            cb_url = self._callback_url
            if not cb_url or not is_valid_private_ipv4(ip):
                return None
            self._inflight_subscribes += 1
        conn = http.client.HTTPConnection(ip, 1400, timeout=HTTP_TIMEOUT)
        t0 = time.monotonic()
        try:
            conn.request(
                "SUBSCRIBE",
                path,
                headers={
                    "Host": f"{ip}:1400",
                    "CALLBACK": f"<{cb_url}>",
                    "NT": "upnp:event",
                    "TIMEOUT": f"Second-{GENA_SUB_TIMEOUT_SEC}",
                    "Connection": "close",
                },
            )
            resp = conn.getresponse()
            resp.read(4096)
            if resp.status != 200:
                _RTT.record_failure(ip)
                _debug_log(f"GENA SUBSCRIBE {kind} on {ip}{path} returned HTTP {resp.status}")
                return None
            _RTT.record(ip, (time.monotonic() - t0) * 1000.0)
            sid = (resp.getheader("SID") or "").strip()
            if not sid:
                return None
            ttl = _parse_gena_timeout(resp.getheader("TIMEOUT"))
            sub = _GenaSub(
                sid=sid,
                ip=ip,
                path=path,
                kind=kind,
                target_uuid=target_uuid,
                expires_at=time.monotonic() + ttl,
                gen=gen,
            )
            with self._lock:
                if not self._shutdown and self._gen == gen:
                    self._subs[sid] = sub
                    self._sub_cond.notify_all()
                    return sub
            # Generation advanced during reset/shutdown while SUBSCRIBE was in flight.
            self._unsubscribe(sub, timeout=0.35)
            return None
        except Exception as exc:
            _RTT.record_failure(ip)
            _debug_log(f"GENA SUBSCRIBE {kind} on {ip}{path} failed", exc)
            return None
        finally:
            with self._lock:
                self._inflight_subscribes = max(0, self._inflight_subscribes - 1)
                self._sub_cond.notify_all()
            try:
                conn.close()
            except Exception:
                pass

    def _renew(self, sub: _GenaSub) -> bool:
        conn = http.client.HTTPConnection(sub.ip, 1400, timeout=HTTP_TIMEOUT)
        t0 = time.monotonic()
        try:
            conn.request(
                "SUBSCRIBE",
                sub.path,
                headers={
                    "Host": f"{sub.ip}:1400",
                    "SID": sub.sid,
                    "TIMEOUT": f"Second-{GENA_SUB_TIMEOUT_SEC}",
                    "Connection": "close",
                },
            )
            resp = conn.getresponse()
            resp.read(4096)
            if resp.status != 200:
                _RTT.record_failure(sub.ip)
                _debug_log(
                    f"GENA renew {sub.kind} on {sub.ip} returned HTTP {resp.status}"
                )
                return False
            _RTT.record(sub.ip, (time.monotonic() - t0) * 1000.0)
            ttl = _parse_gena_timeout(resp.getheader("TIMEOUT"))
            with self._lock:
                if sub.sid in self._subs:
                    self._subs[sub.sid].expires_at = time.monotonic() + ttl
            return True
        except Exception as exc:
            _RTT.record_failure(sub.ip)
            _debug_log(f"GENA renew {sub.kind} on {sub.ip} failed", exc)
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _unsubscribe(self, sub: _GenaSub, timeout: float = 1.0) -> None:
        with self._lock:
            if self._subs.get(sub.sid) is sub:
                self._subs.pop(sub.sid, None)
        conn = http.client.HTTPConnection(sub.ip, 1400, timeout=timeout)
        try:
            conn.request(
                "UNSUBSCRIBE",
                sub.path,
                headers={
                    "Host": f"{sub.ip}:1400",
                    "SID": sub.sid,
                    "Connection": "close",
                },
            )
            resp = conn.getresponse()
            resp.read(1024)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def probe(self, seed_ip: str, wait_sec: float = GENA_PROBE_WAIT_SEC) -> bool:
        with self._lock:
            if self._shutdown or self._state != "unprobed":
                return self._state == "active"
            self._state = "probing"
            gen = self._gen
            self._first_notify.clear()

        local_ip = detect_local_ip_for(seed_ip)
        if not local_ip or not self._start_server(local_ip, expected_gen=gen):
            _debug_log(f"GENA probe could not bind local listener for {seed_ip}")
            with self._lock:
                if self._gen == gen and self._state == "probing":
                    self._state = "unavailable"
            return False

        sub = self._subscribe(
            seed_ip,
            "/ZoneGroupTopology/Event",
            "topology",
            seed_ip,
            expected_gen=gen,
        )
        if sub is None:
            srv: http.server.ThreadingHTTPServer | None = None
            with self._lock:
                if self._gen == gen and self._state == "probing":
                    self._state = "unavailable"
                    srv = self._detach_server_locked()
            self._close_server(srv)
            return False

        got_notify = self._first_notify.wait(timeout=wait_sec)
        srv_to_close: http.server.ThreadingHTTPServer | None = None
        with self._lock:
            if self._shutdown or self._gen != gen or self._state != "probing":
                return False
            cb = self._callback_url
            if got_notify:
                self._state = "active"
            else:
                self._state = "unavailable"
                srv_to_close = self._detach_server_locked()
        if got_notify:
            _debug_log(f"GENA active on {cb}")
            return True

        # Initial NOTIFY did not arrive (likely blocked by UFW); tear down cleanly.
        _debug_log(f"GENA probe timed out on {cb}; falling back to polling")
        self._close_server(srv_to_close)
        self._unsubscribe(sub, timeout=0.8)
        return False

    def sync_subscriptions(
        self,
        groups: list[ZoneGroup],
        speakers: list[SpeakerInfo],
        topo_ip: str,
        pool: concurrent.futures.ThreadPoolExecutor,
    ) -> bool:
        """Synchronize GENA subscriptions. Returns True if GENA remains active."""
        with self._sync_lock:
            with self._lock:
                if self._shutdown or self._state != "active" or not groups:
                    return self._state == "active"
                gen = self._gen
                current_subs = list(self._subs.values())

            desired: set[tuple[str, str, str, str]] = set()
            if topo_ip:
                desired.add(("topology", topo_ip, "/ZoneGroupTopology/Event", topo_ip))
                desired.add(
                    (
                        "content_directory",
                        topo_ip,
                        "/MediaServer/ContentDirectory/Event",
                        topo_ip,
                    )
                )
            for g in groups:
                desired.add(
                    (
                        "avtransport",
                        g.coord_ip,
                        "/MediaRenderer/AVTransport/Event",
                        g.coord_uuid,
                    )
                )
                if len(g.member_uuids) > 1:
                    desired.add(
                        (
                            "group_rc",
                            g.coord_ip,
                            "/MediaRenderer/GroupRenderingControl/Event",
                            g.coord_uuid,
                        )
                    )
            for s in speakers:
                desired.add(("rc", s.ip, "/MediaRenderer/RenderingControl/Event", s.uuid))

            now = time.monotonic()
            existing_by_key: dict[tuple[str, str, str, str], _GenaSub] = {}
            to_unsub: list[_GenaSub] = []
            to_renew: list[_GenaSub] = []
            for sub in current_subs:
                if sub.key not in desired or sub.key in existing_by_key:
                    to_unsub.append(sub)
                else:
                    existing_by_key[sub.key] = sub
                    if sub.expires_at - now < GENA_RENEW_BEFORE_SEC:
                        to_renew.append(sub)

            to_sub = [k for k in desired if k not in existing_by_key]

            futs: list[concurrent.futures.Future] = []
            for sub in to_unsub:
                futs.append(pool.submit(self._unsubscribe, sub))
            for kind, ip, path, target_uuid in to_sub:
                futs.append(
                    pool.submit(
                        self._subscribe,
                        ip,
                        path,
                        kind,
                        target_uuid,
                        expected_gen=gen,
                    )
                )

            renew_futs = [(sub, pool.submit(self._renew, sub)) for sub in to_renew]
            for fut in futs:
                try:
                    fut.result()
                except Exception:
                    pass
            resub_futs: list[concurrent.futures.Future] = []
            for sub, fut in renew_futs:
                try:
                    ok = fut.result()
                except Exception:
                    ok = False
                if not ok:
                    with self._lock:
                        if self._subs.get(sub.sid) is sub:
                            self._subs.pop(sub.sid, None)
                    resub_futs.append(
                        pool.submit(
                            self._subscribe,
                            sub.ip,
                            sub.path,
                            sub.kind,
                            sub.target_uuid,
                            expected_gen=gen,
                        )
                    )
            for fut in resub_futs:
                try:
                    fut.result()
                except Exception:
                    pass

            srv_to_close: http.server.ThreadingHTTPServer | None = None
            my_gen = -1
            with self._lock:
                if self._shutdown or self._gen != gen:
                    return self._state == "active"
                if not self._subs and self._state == "active":
                    self._gen += 1
                    my_gen = self._gen
                    self._state = "resetting"
                    srv_to_close = self._detach_server_locked()
                    self._first_notify.set()
                    self._sub_cond.notify_all()
            if my_gen >= 0:
                _debug_log("GENA lost all subscriptions; stopping listener for re-probe")
                self._close_server(srv_to_close)
                with self._lock:
                    self._wait_closing_servers_locked()
                    if self._gen == my_gen and not self._shutdown:
                        self._state = "unprobed"
                return False
            return self.active

    def reset(self) -> None:
        """Tear down active listener & subscriptions and return to unprobed state."""
        with self._lock:
            if self._shutdown:
                return
            self._gen += 1
            my_gen = self._gen
            subs = list(self._subs.values())
            self._subs.clear()
            self._state = "resetting"
            srv = self._detach_server_locked()
            self._first_notify.set()
            self._sub_cond.notify_all()
        # Close the HTTP server FIRST so port 14001 is released before re-probing.
        self._close_server(srv)
        if subs:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(8, len(subs))
            ) as ex:
                list(ex.map(lambda s: self._unsubscribe(s, 0.35), subs))
        with self._lock:
            self._wait_closing_servers_locked()
            if self._gen == my_gen and not self._shutdown:
                self._state = "unprobed"

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            self._gen += 1
            subs = list(self._subs.values())
            self._subs.clear()
            self._state = "unavailable"
            srv = self._detach_server_locked()
            self._first_notify.set()
            self._sub_cond.notify_all()
        self._close_server(srv)
        if subs:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(8, len(subs))
            ) as ex:
                list(ex.map(lambda s: self._unsubscribe(s, 0.5), subs))


def acquire_instance_lock() -> int | None:
    path = lock_file_path()
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None
    except OSError:
        os.close(fd)
        return None


def run_daemon() -> int:
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    lock_fd = acquire_instance_lock()
    if lock_fd is None:
        return 0

    # Clean up any legacy state file from older versions; state is served on demand over RPC.
    try:
        legacy = legacy_state_file_path()
        if os.path.exists(legacy):
            os.unlink(legacy)
    except OSError:
        pass

    def loop_status_for(repeat: str) -> str:
        if repeat == "one":
            return "Track"
        if repeat == "all":
            return "Playlist"
        return "None"

    class SonosMprisPlayer(dbus.service.Object):
        def __init__(
            self,
            group: ZoneGroup,
            state: TrackState,
            on_action_cb,
            action_pool: concurrent.futures.ThreadPoolExecutor,
        ) -> None:
            self.group = group
            self.state = state
            self._state_at = time.monotonic()
            self._on_action_cb = on_action_cb
            self._action_pool = action_pool
            self._vol_lock = threading.Lock()
            self._pending_volume: int | None = None
            self._vol_worker_running = False
            self._closed = False
            self.bus_name_str = f"org.mpris.MediaPlayer2.sonos_{group.bus_suffix}"
            self._bus = dbus.SessionBus(private=True)
            try:
                self._bus_name: dbus.service.BusName | None = dbus.service.BusName(
                    self.bus_name_str, bus=self._bus
                )
                super().__init__(self._bus, MPRIS_PATH)
            except Exception:
                self._bus_name = None
                try:
                    self._bus.close()
                except Exception:
                    pass
                raise

        def close(self) -> None:
            self._closed = True
            try:
                self.remove_from_connection()
            except Exception:
                pass
            self._bus_name = None
            try:
                self._bus.close()
            except Exception:
                pass

        def _identity(self) -> str:
            return f"Sonos · {self.group.room_label}"

        def _current_position_us(self) -> int:
            st = self.state
            if st.playback_status == "Playing" and st.duration_sec > 0:
                anchor = st.position_at if st.position_at > 0.0 else self._state_at
                elapsed_us = max(0, int((time.monotonic() - anchor) * 1_000_000))
                return min(st.duration_us, st.position_us + elapsed_us)
            return st.position_us

        def _metadata_dict(self) -> dbus.Dictionary:
            st = self.state
            track_id = dbus.ObjectPath(
                f"/org/mpris/MediaPlayer2/sonos/{self.group.bus_suffix}"
            )
            album_display = (
                f"{st.album} — Sonos {self.group.room_label}"
                if st.album
                else f"Sonos · {self.group.room_label}"
            )
            meta: dict[str, object] = {
                "mpris:trackid": track_id,
                "xesam:title": dbus.String(st.title),
                "xesam:album": dbus.String(album_display),
                "xesam:artist": dbus.Array(
                    [dbus.String(st.artist or f"Sonos · {self.group.room_label}")],
                    signature="s",
                ),
            }
            if st.art_url:
                meta["mpris:artUrl"] = dbus.String(st.art_url)
            if st.duration_us > 0:
                meta["mpris:length"] = dbus.Int64(st.duration_us)
            return dbus.Dictionary(meta, signature="sv")

        def _root_props(self) -> dict[str, object]:
            return {
                "CanQuit": dbus.Boolean(False),
                "CanRaise": dbus.Boolean(False),
                "HasTrackList": dbus.Boolean(False),
                "Identity": dbus.String(self._identity()),
                "DesktopEntry": dbus.String("sonos"),
                "SupportedUriSchemes": dbus.Array([], signature="s"),
                "SupportedMimeTypes": dbus.Array([], signature="s"),
            }

        def _player_props(self) -> dict[str, object]:
            st = self.state
            return {
                "PlaybackStatus": dbus.String(st.playback_status),
                "LoopStatus": dbus.String(loop_status_for(st.repeat)),
                "Rate": dbus.Double(1.0),
                "Shuffle": dbus.Boolean(st.shuffle),
                "Metadata": self._metadata_dict(),
                "Volume": dbus.Double(st.group_volume / 100.0),
                "Position": dbus.Int64(self._current_position_us()),
                "MinimumRate": dbus.Double(1.0),
                "MaximumRate": dbus.Double(1.0),
                "CanGoNext": dbus.Boolean(st.can_next),
                "CanGoPrevious": dbus.Boolean(st.can_prev),
                "CanPlay": dbus.Boolean(True),
                "CanPause": dbus.Boolean(True),
                "CanSeek": dbus.Boolean(st.duration_sec > 0),
                "CanControl": dbus.Boolean(True),
            }

        def update(self, group: ZoneGroup, state: TrackState) -> None:
            old_group = self.group
            old_state = self.state
            self.group = group
            self.state = state
            self._state_at = time.monotonic()

            if old_group.room_label != group.room_label:
                self.PropertiesChanged(
                    MPRIS_IFACE,
                    dbus.Dictionary(
                        {"Identity": dbus.String(self._identity())}, signature="sv"
                    ),
                    dbus.Array([], signature="s"),
                )

            changed: dict[str, object] = {}
            if old_state.playback_status != state.playback_status:
                changed["PlaybackStatus"] = dbus.String(state.playback_status)
            if old_state.repeat != state.repeat:
                changed["LoopStatus"] = dbus.String(loop_status_for(state.repeat))
            if (
                old_state.title != state.title
                or old_state.artist != state.artist
                or old_state.album != state.album
                or old_state.art_url != state.art_url
                or old_state.duration_sec != state.duration_sec
                or old_group.room_label != group.room_label
            ):
                changed["Metadata"] = self._metadata_dict()
            if old_state.group_volume != state.group_volume:
                changed["Volume"] = dbus.Double(state.group_volume / 100.0)
            if old_state.shuffle != state.shuffle:
                changed["Shuffle"] = dbus.Boolean(state.shuffle)
            if old_state.can_next != state.can_next:
                changed["CanGoNext"] = dbus.Boolean(state.can_next)
            if old_state.can_prev != state.can_prev:
                changed["CanGoPrevious"] = dbus.Boolean(state.can_prev)
            if (old_state.duration_sec > 0) != (state.duration_sec > 0):
                changed["CanSeek"] = dbus.Boolean(state.duration_sec > 0)

            if changed:
                self.PropertiesChanged(
                    PLAYER_IFACE,
                    dbus.Dictionary(changed, signature="sv"),
                    dbus.Array([], signature="s"),
                )

        def _run_soap_async(self, path: str, service: str, action: str, body: str) -> None:
            ip = self.group.coord_ip

            def worker() -> None:
                try:
                    soap_call(ip, path, service, action, body)
                except Exception:
                    pass
                self._on_action_cb()

            try:
                self._action_pool.submit(worker)
            except RuntimeError:
                pass

        def _drain_volume_writes(self) -> None:
            while True:
                with self._vol_lock:
                    vol = self._pending_volume
                    self._pending_volume = None
                    if vol is None or self._closed:
                        self._vol_worker_running = False
                        break
                group = self.group
                try:
                    if len(group.member_uuids) > 1:
                        soap_call(
                            group.coord_ip,
                            "/MediaRenderer/GroupRenderingControl/Control",
                            "GroupRenderingControl",
                            "SetGroupVolume",
                            f"<InstanceID>0</InstanceID><DesiredVolume>{vol}</DesiredVolume>",
                        )
                    else:
                        soap_call(
                            group.coord_ip,
                            "/MediaRenderer/RenderingControl/Control",
                            "RenderingControl",
                            "SetVolume",
                            f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>{vol}</DesiredVolume>",
                        )
                except Exception:
                    pass
            if not self._closed:
                self._on_action_cb()

        @dbus.service.method(MPRIS_IFACE)
        def Raise(self) -> None:
            pass

        @dbus.service.method(MPRIS_IFACE)
        def Quit(self) -> None:
            pass

        def _emit_playback_status(self, prev_status: str) -> None:
            if self.state.playback_status != prev_status:
                self.PropertiesChanged(
                    PLAYER_IFACE,
                    dbus.Dictionary(
                        {"PlaybackStatus": dbus.String(self.state.playback_status)},
                        signature="sv",
                    ),
                    dbus.Array([], signature="s"),
                )

        @dbus.service.method(PLAYER_IFACE)
        def Play(self) -> None:
            now = time.monotonic()
            prev_status = self.state.playback_status
            self.state = replace(self.state, playback_status="Playing", position_at=now)
            self._state_at = now
            self._emit_playback_status(prev_status)
            self._run_soap_async(
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "Play",
                "<InstanceID>0</InstanceID><Speed>1</Speed>",
            )

        @dbus.service.method(PLAYER_IFACE)
        def Pause(self) -> None:
            now = time.monotonic()
            prev_status = self.state.playback_status
            self.state = replace(
                self.state,
                playback_status="Paused",
                position_sec=extrapolate_position_sec(self.state, now),
                position_at=now,
            )
            self._state_at = now
            self._emit_playback_status(prev_status)
            self._run_soap_async(
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "Pause",
                "<InstanceID>0</InstanceID>",
            )

        @dbus.service.method(PLAYER_IFACE)
        def PlayPause(self) -> None:
            if self.state.playback_status == "Playing":
                self.Pause()
            else:
                self.Play()

        @dbus.service.method(PLAYER_IFACE)
        def Stop(self) -> None:
            now = time.monotonic()
            prev_status = self.state.playback_status
            self.state = replace(
                self.state,
                playback_status="Stopped",
                position_sec=0,
                position_at=now,
            )
            self._state_at = now
            self._emit_playback_status(prev_status)
            self._run_soap_async(
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "Stop",
                "<InstanceID>0</InstanceID>",
            )

        @dbus.service.method(PLAYER_IFACE)
        def Next(self) -> None:
            self._run_soap_async(
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "Next",
                "<InstanceID>0</InstanceID>",
            )

        @dbus.service.method(PLAYER_IFACE)
        def Previous(self) -> None:
            self._run_soap_async(
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "Previous",
                "<InstanceID>0</InstanceID>",
            )

        @dbus.service.method(PLAYER_IFACE, in_signature="ox")
        def SetPosition(self, _track_id, position_us) -> None:
            sec = max(0, int(int(position_us) // 1_000_000))
            now = time.monotonic()
            self.state = replace(self.state, position_sec=sec, position_at=now)
            self._state_at = now
            self._run_soap_async(
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "Seek",
                f"<InstanceID>0</InstanceID><Unit>REL_TIME</Unit><Target>{format_sec_to_hms(sec)}</Target>",
            )

        @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
        def Get(self, interface_name: str, property_name: str):
            props = self.GetAll(interface_name)
            if property_name not in props:
                raise dbus.exceptions.DBusException(
                    "org.freedesktop.DBus.Error.InvalidArgs",
                    f"Unknown property {property_name}",
                )
            return props[property_name]

        @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface_name: str):
            if interface_name == MPRIS_IFACE:
                return dbus.Dictionary(self._root_props(), signature="sv")
            if interface_name == PLAYER_IFACE:
                return dbus.Dictionary(self._player_props(), signature="sv")
            return dbus.Dictionary({}, signature="sv")

        @dbus.service.method(PROPS_IFACE, in_signature="ssv")
        def Set(self, interface_name: str, property_name: str, value) -> None:
            if interface_name == PLAYER_IFACE and property_name == "Volume":
                vol = max(0, min(100, int(round(float(value) * 100))))
                if vol != self.state.group_volume:
                    self.state = replace(self.state, group_volume=vol)
                    self.PropertiesChanged(
                        PLAYER_IFACE,
                        dbus.Dictionary(
                            {"Volume": dbus.Double(vol / 100.0)}, signature="sv"
                        ),
                        dbus.Array([], signature="s"),
                    )
                should_start = False
                with self._vol_lock:
                    self._pending_volume = vol
                    if not self._vol_worker_running:
                        self._vol_worker_running = True
                        should_start = True
                if should_start:
                    try:
                        self._action_pool.submit(self._drain_volume_writes)
                    except RuntimeError:
                        with self._vol_lock:
                            self._vol_worker_running = False

        @dbus.service.signal(PROPS_IFACE, signature="sa{sv}as")
        def PropertiesChanged(
            self, interface_name, changed_properties, invalidated_properties
        ):
            pass

    class BridgeController:
        def __init__(self, loop: GLib.MainLoop) -> None:
            self.loop = loop
            self.stop_event = threading.Event()
            self.wake_event = threading.Event()
            self.lock = threading.Lock()
            self._topo_lock = threading.Lock()
            self._library_lock = threading.Lock()
            self._recovery_lock = threading.Lock()
            self._event_lock = threading.Lock()
            self._event_clients: dict[
                socket.socket, tuple[threading.Lock, list[int]]
            ] = {}
            self.players: dict[str, SonosMprisPlayer] = {}
            cached_ips, cached_rtts = load_cached_seeds()
            _RTT.seed(cached_rtts)
            self._seed_ips: list[str] = _RTT.rank(cached_ips)
            self._preferred_ip: str = self._seed_ips[0] if self._seed_ips else ""
            self._last_saved_seeds: tuple[str, ...] = tuple(self._seed_ips)
            self._groups: list[ZoneGroup] = []
            self._speakers: list[SpeakerInfo] = []
            self._group_states: dict[str, TrackState] = {}
            self._speaker_audio: dict[str, tuple[int, bool]] = {}
            self._topo_seq = 0
            self._item_seq = 0
            self._group_state_seq: dict[str, int] = {}
            self._group_audio_seq: dict[str, int] = {}
            self._speaker_audio_seq: dict[str, int] = {}
            self._active_uuids: set[str] = set()
            self._media_info_hints: set[str] = set()
            self._started_at = time.monotonic()
            self._last_mono = self._started_at
            self._last_boot = _boottime()
            self._last_local_ip = ""
            self._recovery_epoch = 0
            self._last_discovery = 0.0
            self._last_topology = 0.0
            self._last_full_sweep = 0.0
            self._last_settings_refresh = 0.0
            self._last_snapshot_at = 0.0
            self._snapshot_seq = 0
            self._explicit_wake = False
            self._library_cache: list[dict] = []
            self._library_cache_at = 0.0
            self._library_version = 0
            self._library_gena_versions: dict[str, str] = {}
            self._library_prewarm_started = False
            self._queue_cache: dict[str, tuple[int, float, list[dict]]] = {}
            self._mpris_flush_scheduled = False
            self._rpc_sock: socket.socket | None = None
            self._gena = _GenaManager(on_event=self._on_gena_event)
            self._gena_probe_started = False
            # _soap_pool only executes leaf SOAP/HTTP calls so tasks never nest or deadlock.
            self._soap_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=36, thread_name_prefix="sonos-soap"
            )
            self._rpc_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=8, thread_name_prefix="sonos-rpc"
            )
            self._action_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="sonos-act"
            )

        def _next_item_seq_locked(self) -> int:
            self._item_seq += 1
            return self._item_seq

        def _bump_group_seq_locked(self, coord_uuid: str) -> None:
            self._group_state_seq[coord_uuid] = self._next_item_seq_locked()

        def _bump_group_audio_seq_locked(self, coord_uuid: str) -> None:
            self._group_audio_seq[coord_uuid] = self._next_item_seq_locked()

        def _bump_speaker_seq_locked(self, speaker_uuid: str) -> None:
            self._speaker_audio_seq[speaker_uuid] = self._next_item_seq_locked()

        def _commit_group_state_locked(
            self,
            states: dict[str, TrackState],
            uuid: str,
            st: TrackState,
            fetch_seq: int,
            *,
            has_audio: bool,
            has_settings: bool,
        ) -> tuple[bool, bool]:
            cur = states.get(uuid)
            state_ok = self._group_state_seq.get(uuid, 0) < fetch_seq
            audio_ok = has_audio and self._group_audio_seq.get(uuid, 0) < fetch_seq
            if state_ok:
                merged_st = st
                if cur is not None:
                    if not audio_ok:
                        merged_st = replace(
                            merged_st,
                            group_volume=cur.group_volume,
                            group_muted=cur.group_muted,
                        )
                    if not has_settings:
                        merged_st = replace(
                            merged_st,
                            shuffle=cur.shuffle,
                            repeat=cur.repeat,
                        )
                states[uuid] = merged_st
                self._group_state_seq[uuid] = fetch_seq
                if audio_ok:
                    self._group_audio_seq[uuid] = fetch_seq
                return True, audio_ok
            if audio_ok and cur is not None:
                states[uuid] = replace(
                    cur,
                    group_volume=st.group_volume,
                    group_muted=st.group_muted,
                )
                self._group_audio_seq[uuid] = fetch_seq
                return True, True
            return False, False

        def _commit_topology_locked(
            self,
            groups: list[ZoneGroup],
            speakers: list[SpeakerInfo],
            ranked: list[str],
            *,
            clear_queue_always: bool = False,
        ) -> bool:
            """Commit topology, prune stale state/seq maps, and sync newly ungrouped single rooms."""
            prev_groups = {g.coord_uuid: g for g in self._groups}
            prev_shape = [(g.coord_uuid, g.member_uuids) for g in self._groups]
            new_shape = [(g.coord_uuid, g.member_uuids) for g in groups]

            self._groups = groups
            self._speakers = speakers
            self._seed_ips = ranked
            self._preferred_ip = ranked[0] if ranked else ""
            self._topo_seq += 1
            self._last_topology = time.monotonic()
            if clear_queue_always or new_shape != prev_shape:
                self._queue_cache.clear()

            valid_groups = {g.coord_uuid for g in groups}
            valid_speakers = {s.uuid for s in speakers}
            prev_state_keys = set(self._group_states.keys())
            self._group_states = {
                k: v for k, v in self._group_states.items() if k in valid_groups
            }
            state_changed = set(self._group_states.keys()) != prev_state_keys
            self._speaker_audio = {
                k: v for k, v in self._speaker_audio.items() if k in valid_speakers
            }
            self._group_state_seq = {
                k: v for k, v in self._group_state_seq.items() if k in valid_groups
            }
            self._group_audio_seq = {
                k: v for k, v in self._group_audio_seq.items() if k in valid_groups
            }
            self._speaker_audio_seq = {
                k: v for k, v in self._speaker_audio_seq.items() if k in valid_speakers
            }
            # Sync standalone single-room coordinators from per-speaker audio when they
            # transitioned from multi-room -> single-room OR _speaker_audio is at least
            # as fresh as _group_audio_seq (preventing stale _speaker_audio from reverting).
            for g in groups:
                if len(g.member_uuids) != 1 or g.coord_uuid not in self._group_states:
                    continue
                sp_aud = self._speaker_audio.get(g.coord_uuid)
                if sp_aud is None:
                    continue
                prev_g = prev_groups.get(g.coord_uuid)
                was_multi = prev_g is not None and len(prev_g.member_uuids) > 1
                sp_seq = self._speaker_audio_seq.get(g.coord_uuid, 0)
                ga_seq = self._group_audio_seq.get(g.coord_uuid, 0)
                if was_multi or sp_seq >= ga_seq:
                    cur_st = self._group_states[g.coord_uuid]
                    if (
                        cur_st.group_volume != sp_aud[0]
                        or cur_st.group_muted != sp_aud[1]
                    ):
                        self._group_states[g.coord_uuid] = replace(
                            cur_st,
                            group_volume=sp_aud[0],
                            group_muted=sp_aud[1],
                        )
                        self._bump_group_audio_seq_locked(g.coord_uuid)
                        state_changed = True
            self._active_uuids &= valid_groups
            return state_changed

        def _check_network_recovery(self) -> bool:
            """Detect suspend/resume, route loss, or host LAN IP changes and reset cleanly."""
            with self.lock:
                entry_epoch = self._recovery_epoch
            with self._recovery_lock:
                with self.lock:
                    if self._recovery_epoch != entry_epoch:
                        return True
                    now_mono = time.monotonic()
                    now_boot = _boottime()
                    drift = (now_boot - self._last_boot) - (now_mono - self._last_mono)
                    self._last_mono = now_mono
                    self._last_boot = now_boot
                    seed_ip = self._preferred_ip or (
                        self._seed_ips[0] if self._seed_ips else ""
                    )
                    prev_local_ip = self._last_local_ip

                reason = ""
                cur_local_ip = detect_local_ip_for(seed_ip) if seed_ip else ""
                if drift > SUSPEND_DRIFT_SEC:
                    reason = f"suspend/resume detected (drift={drift:.1f}s)"
                elif seed_ip:
                    if prev_local_ip and not cur_local_ip:
                        reason = f"LAN route lost (was {prev_local_ip})"
                    elif cur_local_ip and prev_local_ip and cur_local_ip != prev_local_ip:
                        reason = f"local IP changed ({prev_local_ip} -> {cur_local_ip})"
                    elif cur_local_ip and not prev_local_ip:
                        with self.lock:
                            self._last_local_ip = cur_local_ip

                if not reason:
                    return False

                _debug_log(f"network recovery triggered: {reason}")
                _HTTP_POOL.close_all()
                self._gena.reset()
                new_local = detect_local_ip_for(seed_ip) if seed_ip else ""
                with self.lock:
                    self._recovery_epoch += 1
                    self._gena_probe_started = False
                    self._last_discovery = 0.0
                    self._last_topology = 0.0
                    self._last_full_sweep = 0.0
                    self._last_settings_refresh = 0.0
                    self._last_snapshot_at = 0.0
                    self._queue_cache.clear()
                    self._library_cache = []
                    self._library_cache_at = 0.0
                    self._library_version += 1
                    self._library_gena_versions.clear()
                    self._library_prewarm_started = False
                    self._last_local_ip = new_local
                    self._explicit_wake = True
                self.wake_event.set()
                return True

        def _load_library(
            self, force: bool = False, fallback_group: ZoneGroup | None = None
        ) -> tuple[bool, list[dict], str]:
            with self._library_lock:
                now = time.monotonic()
                ttl = (
                    LIBRARY_CACHE_TTL_GENA
                    if self._gena.has_kind("content_directory")
                    else LIBRARY_CACHE_TTL
                )
                with self.lock:
                    if (
                        not force
                        and self._library_cache
                        and (now - self._library_cache_at) < ttl
                    ):
                        return True, list(self._library_cache), ""
                    ver_before = self._library_version
                    seeds = list(self._seed_ips)
                    cur_pref = self._preferred_ip

                candidates = _RTT.rank_with_preferred(seeds, current=cur_pref)
                if fallback_group and fallback_group.coord_ip not in candidates:
                    candidates.append(fallback_group.coord_ip)
                if not candidates:
                    groups, _ = self._ensure_topology(force=False)
                    if groups:
                        candidates = _RTT.rank_with_preferred(
                            [g.coord_ip for g in groups], current=cur_pref
                        )
                if not candidates:
                    return False, [], "no Sonos speakers discovered"

                last_err = ""
                items: list[dict] | None = None
                used_ip = ""
                for ip in candidates[:2]:
                    fav_fut = self._soap_pool.submit(
                        browse_content_directory,
                        ip,
                        "FV:2",
                        100,
                        raise_on_error=True,
                    )
                    pl_fut = self._soap_pool.submit(
                        browse_content_directory,
                        ip,
                        "SQ:",
                        100,
                        raise_on_error=True,
                    )
                    try:
                        items = fav_fut.result() + pl_fut.result()
                        used_ip = ip
                        break
                    except Exception as exc:
                        last_err = str(exc)
                        _debug_log(f"_load_library({ip}) failed; trying next speaker", exc)
                        continue
                if items is None:
                    return False, [], last_err or "failed to load Sonos library"

                with self.lock:
                    if used_ip:
                        self._preferred_ip = used_ip
                    if self._library_version == ver_before:
                        self._library_cache = items
                        self._library_cache_at = time.monotonic()
                return True, items, ""

        def _maybe_prewarm_library(self) -> None:
            if self.stop_event.is_set():
                return
            with self.lock:
                if (
                    self._library_prewarm_started
                    or self._library_cache
                    or not self._groups
                ):
                    return
                self._library_prewarm_started = True

            def _prewarm_worker() -> None:
                if self.stop_event.is_set():
                    return
                try:
                    ok, items, _ = self._load_library(force=False)
                    if ok:
                        _debug_log(f"pre-warmed library cache ({len(items)} items)")
                except Exception as exc:
                    _debug_log("library pre-warm failed", exc)

            try:
                self._action_pool.submit(_prewarm_worker)
            except RuntimeError:
                pass

        def _build_diagnostics(self) -> dict:
            now = time.monotonic()
            gena_info = self._gena.diagnostics(now)
            cd_active = self._gena.has_kind("content_directory")
            with self.lock:
                groups_count = len(self._groups)
                speakers_count = len(self._speakers)
                speakers_ready = all(
                    s.uuid in self._speaker_audio for s in self._speakers
                )
                active_rooms = [
                    g.room_label
                    for g in self._groups
                    if g.coord_uuid in self._active_uuids
                ]
                preferred_ip = self._preferred_ip
                seed_ips = list(self._seed_ips)
                lib_count = len(self._library_cache)
                lib_age = (
                    round(now - self._library_cache_at, 1)
                    if self._library_cache_at > 0.0
                    else None
                )
                queue_cached_groups = len(self._queue_cache)
                snapshot_seq = self._snapshot_seq
                last_snapshot_age = (
                    round(now - self._last_snapshot_at, 1)
                    if self._last_snapshot_at > 0.0
                    else None
                )
            with self._event_lock:
                event_clients = len(self._event_clients)
            rtts = {
                ip: round(ms, 1)
                for ip, ms in sorted(_RTT.snapshot().items(), key=lambda kv: kv[1])
            }
            return {
                "ok": True,
                "pid": os.getpid(),
                "uptime_sec": round(now - self._started_at, 1),
                "groups": groups_count,
                "speakers": speakers_count,
                "speakers_ready": speakers_ready,
                "active_rooms": active_rooms,
                "preferred_ip": preferred_ip,
                "seed_ips": seed_ips,
                "rtts_ms": rtts,
                "gena": gena_info,
                "caches": {
                    "library_items": lib_count,
                    "library_age_sec": lib_age,
                    "library_ttl_sec": (
                        LIBRARY_CACHE_TTL_GENA if cd_active else LIBRARY_CACHE_TTL
                    ),
                    "queue_groups": queue_cached_groups,
                },
                "rpc": {
                    "socket_path": socket_file_path(),
                    "event_clients": event_clients,
                    "snapshot_seq": snapshot_seq,
                    "last_snapshot_age_sec": last_snapshot_age,
                },
                "debug": _debug_enabled(),
            }

        def trigger_poll(
            self, force_topology: bool = False, full_sweep: bool = False
        ) -> None:
            with self.lock:
                if force_topology:
                    self._last_topology = 0.0
                if full_sweep or force_topology:
                    self._last_full_sweep = 0.0
                self._explicit_wake = True
            self.wake_event.set()

        def _schedule_mpris_update(self) -> None:
            schedule = False
            with self.lock:
                if not self._mpris_flush_scheduled:
                    self._mpris_flush_scheduled = True
                    schedule = True
            if schedule:
                GLib.idle_add(self._flush_mpris_on_main)

        def _flush_mpris_on_main(self) -> bool:
            snapshot: dict[str, tuple[ZoneGroup, TrackState]] = {}
            with self.lock:
                self._mpris_flush_scheduled = False
                for g in self._groups:
                    gst = self._group_states.get(g.coord_uuid)
                    if gst and (gst.playback_status == "Playing" or gst.has_track):
                        snapshot[g.coord_uuid] = (g, gst)
            self._apply_mpris_on_main(snapshot)
            return False

        def _prune_dead_event_clients_locked(self) -> list[socket.socket]:
            dead: list[socket.socket] = []
            for client in list(self._event_clients.keys()):
                try:
                    peek = client.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                    if peek == b"":
                        dead.append(client)
                except BlockingIOError:
                    continue
                except OSError:
                    dead.append(client)
            for client in dead:
                self._event_clients.pop(client, None)
            return dead

        def _add_event_client(self, conn: socket.socket) -> None:
            self._check_network_recovery()
            write_lock = threading.Lock()
            last_seq = [-1]
            dead: list[socket.socket] = []
            send_failed = False
            registered = False
            with write_lock:
                # Register BEFORE reading self.lock so no concurrent broadcast between
                # snapshot and registration can be missed; broadcasts block on write_lock
                # until initial_line completes and skip anything older than initial_seq.
                with self._event_lock:
                    dead = self._prune_dead_event_clients_locked()
                    if not self.stop_event.is_set():
                        self._event_clients[conn] = (write_lock, last_seq)
                        registered = True
                if registered:
                    initial_line: bytes | None = None
                    initial_seq = -1
                    with self.lock:
                        if (
                            self._groups
                            and self._group_states
                            and self._last_snapshot_at > 0.0
                        ):
                            initial_seq = self._snapshot_seq
                            payload = build_state_payload(
                                self._groups,
                                self._speakers,
                                self._group_states,
                                self._speaker_audio,
                                gena_active=self._gena.active,
                                cached=True,
                                library_version=self._library_version,
                            )
                            initial_line = (json.dumps(payload) + "\n").encode("utf-8")
                    if initial_line is not None:
                        try:
                            conn.settimeout(0.5)
                            conn.sendall(initial_line)
                            last_seq[0] = initial_seq
                        except OSError:
                            send_failed = True
                            with self._event_lock:
                                self._event_clients.pop(conn, None)
            for stale in dead:
                try:
                    stale.close()
                except OSError:
                    pass
            if send_failed or not registered:
                try:
                    conn.close()
                except OSError:
                    pass

        def _broadcast_state(
            self, payload: dict | None = None, *, seq: int | None = None
        ) -> None:
            with self._event_lock:
                if not self._event_clients:
                    return
                clients = list(self._event_clients.items())
            if payload is None:
                with self.lock:
                    if not self._groups or not self._group_states:
                        return
                    seq = self._snapshot_seq
                    payload = build_state_payload(
                        self._groups,
                        self._speakers,
                        self._group_states,
                        self._speaker_audio,
                        gena_active=self._gena.active,
                        cached=False,
                        library_version=self._library_version,
                    )
            line = (json.dumps(payload) + "\n").encode("utf-8")
            dead: list[socket.socket] = []
            for conn, (write_lock, last_seq) in clients:
                with write_lock:
                    if seq is not None and seq < last_seq[0]:
                        continue
                    try:
                        conn.settimeout(0.5)
                        conn.sendall(line)
                        if seq is not None:
                            last_seq[0] = seq
                    except OSError:
                        dead.append(conn)
            if dead:
                with self._event_lock:
                    for conn in dead:
                        self._event_clients.pop(conn, None)
                for conn in dead:
                    try:
                        conn.close()
                    except OSError:
                        pass

        def _maybe_start_gena(self, seed_ip: str) -> None:
            if not seed_ip or self.stop_event.is_set():
                return
            with self.lock:
                if self._gena_probe_started:
                    return
                self._gena_probe_started = True

            def _probe_worker() -> None:
                if self.stop_event.is_set():
                    return
                if self._gena.probe(seed_ip):
                    local_ip = self._gena.local_ip
                    with self.lock:
                        if local_ip:
                            self._last_local_ip = local_ip
                        groups = list(self._groups)
                        speakers = list(self._speakers)
                        topo_ip = (
                            _RTT.pick_preferred(
                                list(self._seed_ips), current=self._preferred_ip
                            )
                            or seed_ip
                        )
                        self._preferred_ip = topo_ip
                    still_active = self._gena.sync_subscriptions(
                        groups, speakers, topo_ip, self._soap_pool
                    )
                    if not still_active:
                        with self.lock:
                            self._gena_probe_started = False
                            self._snapshot_seq += 1
                        self._broadcast_state()
                        self.trigger_poll(force_topology=True)
                    else:
                        with self.lock:
                            self._snapshot_seq += 1
                        self._broadcast_state()
                else:
                    local_ip = detect_local_ip_for(seed_ip)
                    if local_ip:
                        with self.lock:
                            self._last_local_ip = local_ip
                self._maybe_prewarm_library()

            try:
                self._action_pool.submit(_probe_worker)
            except RuntimeError:
                pass

        def _sync_position_for_group(
            self,
            group: ZoneGroup,
            expected_track_key: tuple[int, str] | None = None,
        ) -> None:
            with self.lock:
                fetch_seq = self._next_item_seq_locked()
            pos_xml = _soap_safe(
                group.coord_ip,
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "GetPositionInfo",
                "<InstanceID>0</InstanceID>",
            )
            if not pos_xml:
                return
            _, _, _, _, _, duration_sec, position_sec, track_num = _parse_position_info(
                group.coord_ip, pos_xml
            )
            now = time.monotonic()
            with self.lock:
                if self._group_state_seq.get(group.coord_uuid, 0) >= fetch_seq:
                    return
                st = self._group_states.get(group.coord_uuid)
                if st is None:
                    return
                if (
                    expected_track_key is not None
                    and (st.track_num, st.title) != expected_track_key
                ):
                    return
                self._group_states[group.coord_uuid] = replace(
                    st,
                    position_sec=position_sec,
                    position_at=now,
                    duration_sec=duration_sec or st.duration_sec,
                    track_num=track_num or st.track_num,
                )
                self._group_state_seq[group.coord_uuid] = fetch_seq
                self._snapshot_seq += 1
                self._last_snapshot_at = now
            self._schedule_mpris_update()
            self._broadcast_state()

        def _on_gena_event(self, sub: _GenaSub, body: str) -> None:
            if self.stop_event.is_set():
                return
            if sub.kind == "content_directory":
                updates = parse_gena_content_directory_event(body)
                if not updates:
                    return
                invalidated = False
                with self.lock:
                    for key, val in updates.items():
                        prev_val = self._library_gena_versions.get(key)
                        if prev_val is not None:
                            if prev_val != val:
                                invalidated = True
                        elif key not in ("fav", "sq") and self._library_cache_at > 0.0:
                            invalidated = True
                        self._library_gena_versions[key] = val
                    if invalidated:
                        self._library_cache = []
                        self._library_cache_at = 0.0
                        self._library_version += 1
                        self._snapshot_seq += 1
                if invalidated:
                    _debug_log(
                        f"GENA ContentDirectory invalidated library cache: {updates}"
                    )
                    self._broadcast_state()
                return

            if sub.kind == "topology":
                parsed = parse_gena_topology_event(body)
                if not parsed:
                    return
                groups, speakers = parsed
                all_ips = [s.ip for s in speakers] + [g.coord_ip for g in groups]
                unique_ips = list(dict.fromkeys(all_ips))
                with self.lock:
                    ranked = _RTT.rank_with_preferred(
                        unique_ips, current=self._preferred_ip
                    )
                    self._commit_topology_locked(
                        groups, speakers, ranked, clear_queue_always=True
                    )
                    self._snapshot_seq += 1
                if self._gena.active:
                    def _sync_after_topo() -> None:
                        with self.lock:
                            cur_groups = list(self._groups)
                            cur_speakers = list(self._speakers)
                            cur_topo_ip = _RTT.pick_preferred(
                                list(self._seed_ips), current=self._preferred_ip
                            ) or (self._seed_ips[0] if self._seed_ips else sub.ip)
                            self._preferred_ip = cur_topo_ip
                        still_active = self._gena.sync_subscriptions(
                            cur_groups, cur_speakers, cur_topo_ip, self._soap_pool
                        )
                        if not still_active:
                            with self.lock:
                                self._gena_probe_started = False
                                self._snapshot_seq += 1
                            self._broadcast_state()
                            self.trigger_poll(force_topology=True)

                    try:
                        self._action_pool.submit(_sync_after_topo)
                    except RuntimeError:
                        pass
                self._schedule_mpris_update()
                self._broadcast_state()
                return

            sync_group: ZoneGroup | None = None
            sync_track_key: tuple[int, str] | None = None
            changed = False

            if sub.kind == "avtransport":
                extracted = _extract_gena_avtransport_payload(sub.ip, body)
                if extracted is not None:
                    vals, track_meta, uri_meta, services = extracted
                    with self.lock:
                        group = find_group(self._groups, sub.target_uuid)
                        if group is not None:
                            prev = self._group_states.get(sub.target_uuid)
                            sp_audio = self._speaker_audio.get(sub.target_uuid)
                            updated, need_pos_sync, queue_changed = (
                                _apply_gena_avtransport_payload(
                                    sub.ip,
                                    vals,
                                    track_meta,
                                    services,
                                    prev,
                                    uri_meta=uri_meta,
                                )
                            )
                            if (
                                (
                                    prev is None
                                    or self._group_audio_seq.get(sub.target_uuid, 0) == 0
                                )
                                and len(group.member_uuids) == 1
                                and sp_audio is not None
                            ):
                                updated = replace(
                                    updated,
                                    group_volume=sp_audio[0],
                                    group_muted=sp_audio[1],
                                )
                                self._bump_group_audio_seq_locked(sub.target_uuid)
                            self._group_states[sub.target_uuid] = updated
                            self._bump_group_seq_locked(sub.target_uuid)
                            if queue_changed:
                                self._queue_cache.pop(sub.target_uuid, None)
                            if updated.playback_status == "Playing" or updated.has_track:
                                self._active_uuids.add(sub.target_uuid)
                            else:
                                self._active_uuids.discard(sub.target_uuid)
                            self._snapshot_seq += 1
                            self._last_snapshot_at = time.monotonic()
                            changed = True
                            if need_pos_sync:
                                sync_group = group
                                sync_track_key = (updated.track_num, updated.title)

            elif sub.kind == "group_rc":
                vol, muted = parse_gena_group_rc_event(body)
                if vol is not None or muted is not None:
                    with self.lock:
                        g = find_group(self._groups, sub.target_uuid)
                        if g is not None and len(g.member_uuids) > 1:
                            prev = (
                                self._group_states.get(sub.target_uuid)
                                or _stopped_track_state()
                            )
                            self._group_states[sub.target_uuid] = replace(
                                prev,
                                group_volume=vol if vol is not None else prev.group_volume,
                                group_muted=muted if muted is not None else prev.group_muted,
                            )
                            self._bump_group_audio_seq_locked(sub.target_uuid)
                            self._snapshot_seq += 1
                            self._last_snapshot_at = time.monotonic()
                            changed = True

            elif sub.kind == "rc":
                vol, muted = parse_gena_rc_event(body)
                if vol is not None or muted is not None:
                    with self.lock:
                        old_vol, old_muted = self._speaker_audio.get(
                            sub.target_uuid, (0, False)
                        )
                        new_vol = vol if vol is not None else old_vol
                        new_muted = muted if muted is not None else old_muted
                        self._speaker_audio[sub.target_uuid] = (new_vol, new_muted)
                        self._bump_speaker_seq_locked(sub.target_uuid)
                        # Single-room coordinators reflect RenderingControl directly on group volume/mute.
                        g = find_group(self._groups, sub.target_uuid)
                        if g is not None and len(g.member_uuids) == 1:
                            prev = self._group_states.get(g.coord_uuid)
                            if prev is not None:
                                self._group_states[g.coord_uuid] = replace(
                                    prev, group_volume=new_vol, group_muted=new_muted
                                )
                                self._bump_group_audio_seq_locked(g.coord_uuid)
                        self._snapshot_seq += 1
                        self._last_snapshot_at = time.monotonic()
                        changed = True

            if changed:
                self._schedule_mpris_update()
                self._broadcast_state()
            if sync_group is not None:
                try:
                    self._soap_pool.submit(
                        self._sync_position_for_group, sync_group, sync_track_key
                    )
                except RuntimeError:
                    pass

        def _ensure_topology(
            self, force: bool = False, interval: float = TOPOLOGY_ACTIVE_INTERVAL
        ) -> tuple[list[ZoneGroup], list[SpeakerInfo]]:
            off_lan_payload: dict | None = None
            off_lan_seq: int | None = None
            gena_reset_raced = False
            raced_seed = ""
            topo_state_changed = False
            with self._topo_lock:
                now = time.monotonic()
                with self.lock:
                    seed_ips = list(self._seed_ips)
                    cur_pref = self._preferred_ip
                    has_groups = bool(self._groups)
                    last_discovery = self._last_discovery
                    last_topology = self._last_topology
                    topo_seq_before = self._topo_seq

                # Only run mDNS/SSDP discovery when we have no known seeds (or explicit
                # forced recovery with empty groups); GetZoneGroupState already discovers
                # every speaker in the house once any single seed IP answers.
                discovered_now = False
                if not seed_ips and (
                    force
                    or last_discovery == 0.0
                    or (now - last_discovery) >= FORCE_DISCOVERY_MIN_INTERVAL
                ):
                    found = discover_sonos_ips()
                    discovered_now = True
                    last_discovery = time.monotonic()
                    if found:
                        seed_ips = _RTT.rank_with_preferred(found, current=cur_pref)
                    with self.lock:
                        self._seed_ips = list(seed_ips)
                        self._last_discovery = last_discovery

                if seed_ips and (
                    force
                    or not has_groups
                    or last_topology == 0.0
                    or (now - last_topology) >= interval
                ):
                    ranked_seeds = _RTT.rank_with_preferred(seed_ips, current=cur_pref)
                    groups, speakers = fetch_topology(
                        ranked_seeds, pool=self._soap_pool
                    )
                    if not groups and not discovered_now:
                        found = discover_sonos_ips()
                        last_discovery = time.monotonic()
                        with self.lock:
                            self._last_discovery = last_discovery
                        if found:
                            ranked_seeds = _RTT.rank_with_preferred(
                                found, current=cur_pref
                            )
                            groups, speakers = fetch_topology(
                                ranked_seeds, pool=self._soap_pool
                            )

                    if groups:
                        all_ips = list(
                            dict.fromkeys(
                                [s.ip for s in speakers] + [g.coord_ip for g in groups]
                            )
                        )
                        new_seeds = _RTT.rank_with_preferred(all_ips, current=cur_pref)
                        returned_to_lan = not has_groups
                        if returned_to_lan and self._gena.state == "unavailable":
                            self._gena.reset()
                        local_ip = detect_local_ip_for(new_seeds[0])
                        save_now = False
                        committed = False
                        with self.lock:
                            # Do not clobber a fresher GENA ZoneGroupTopology event that
                            # arrived while our SOAP GetZoneGroupState was in flight.
                            if self._topo_seq == topo_seq_before:
                                topo_state_changed = self._commit_topology_locked(
                                    groups, speakers, new_seeds, clear_queue_always=False
                                )
                                committed = True
                                if topo_state_changed:
                                    self._snapshot_seq += 1
                            else:
                                self._last_topology = time.monotonic()
                            if local_ip and not self._last_local_ip:
                                self._last_local_ip = local_ip
                            if returned_to_lan and self._gena.state == "unprobed":
                                self._gena_probe_started = False
                            if committed:
                                seed_tuple = tuple(new_seeds)
                                if seed_tuple != self._last_saved_seeds:
                                    self._last_saved_seeds = seed_tuple
                                    save_now = True
                            probe_seed = self._preferred_ip or new_seeds[0]
                        if save_now:
                            save_cached_seeds(new_seeds, _RTT.snapshot())
                        self._maybe_start_gena(probe_seed)
                    else:
                        with self.lock:
                            stale_miss = self._topo_seq != topo_seq_before
                        if not stale_miss:
                            did_reset_gena = False
                            if has_groups or self._gena.state != "unprobed":
                                _debug_log(
                                    "no Sonos speakers reachable; resetting off-LAN state"
                                )
                                self._gena.reset()
                                did_reset_gena = True
                            with self.lock:
                                if self._topo_seq == topo_seq_before:
                                    had_state = bool(self._groups or self._group_states)
                                    self._groups = []
                                    self._speakers = []
                                    self._seed_ips = []
                                    self._preferred_ip = ""
                                    self._topo_seq += 1
                                    self._group_states = {}
                                    self._speaker_audio = {}
                                    self._group_state_seq.clear()
                                    self._group_audio_seq.clear()
                                    self._speaker_audio_seq.clear()
                                    self._active_uuids = set()
                                    self._queue_cache.clear()
                                    self._library_cache = []
                                    self._library_cache_at = 0.0
                                    self._library_version += 1
                                    self._library_gena_versions.clear()
                                    self._library_prewarm_started = False
                                    self._gena_probe_started = False
                                    self._last_local_ip = ""
                                    if had_state:
                                        self._snapshot_seq += 1
                                        off_lan_seq = self._snapshot_seq
                                        off_lan_payload = build_state_payload(
                                            [],
                                            [],
                                            {},
                                            {},
                                            gena_active=False,
                                            cached=False,
                                            library_version=self._library_version,
                                        )
                                elif did_reset_gena:
                                    # A live GENA topology event raced our reset(); clear
                                    # _gena_probe_started and re-probe so GENA is not wedged.
                                    self._gena_probe_started = False
                                    self._snapshot_seq += 1
                                    self._explicit_wake = True
                                    raced_seed = self._preferred_ip or (
                                        self._seed_ips[0] if self._seed_ips else ""
                                    )
                                    gena_reset_raced = True

                with self.lock:
                    res_groups, res_speakers = list(self._groups), list(self._speakers)
            if off_lan_payload is not None:
                self._schedule_mpris_update()
                self._broadcast_state(off_lan_payload, seq=off_lan_seq)
            elif gena_reset_raced:
                self.wake_event.set()
                self._broadcast_state()
                if raced_seed:
                    self._maybe_start_gena(raced_seed)
            elif topo_state_changed:
                self._schedule_mpris_update()
            return res_groups, res_speakers

        def _fetch_group_snapshot(
            self, group: ZoneGroup, include_speakers: bool = True
        ) -> dict:
            with self.lock:
                fetch_seq = self._next_item_seq_locked()
                speakers = list(self._speakers)
                prev_states = dict(self._group_states)
                prev_audio = dict(self._speaker_audio)
                hints = set(self._media_info_hints)

            member_speakers = (
                [s for s in speakers if s.coord_uuid == group.coord_uuid]
                if include_speakers
                else []
            )
            polled_states, polled_audio, failed_any = fetch_groups_batch(
                [group],
                member_speakers,
                self._soap_pool,
                include_speakers=include_speakers,
                known_active_uuids=None,
                prev_states=prev_states,
                prev_speaker_audio=prev_audio,
                media_info_hints=hints,
                selected_coord_uuid=group.coord_uuid,
                refresh_all_settings=True,
            )

            updated_any = False
            pub_seq: int | None = None
            with self.lock:
                if failed_any:
                    self._last_topology = 0.0
                valid_groups = {g.coord_uuid for g in self._groups}
                valid_speakers = {s.uuid for s in self._speakers}
                single_coords = {
                    g.coord_uuid for g in self._groups if len(g.member_uuids) == 1
                }
                for uuid, st in polled_states.items():
                    if uuid in valid_groups:
                        applied, audio_applied = self._commit_group_state_locked(
                            self._group_states,
                            uuid,
                            st,
                            fetch_seq,
                            has_audio=True,
                            has_settings=True,
                        )
                        if applied:
                            updated_any = True
                        if (
                            not include_speakers
                            and audio_applied
                            and uuid in single_coords
                            and uuid in self._speaker_audio
                            and self._speaker_audio_seq.get(uuid, 0) < fetch_seq
                        ):
                            cur_st = self._group_states[uuid]
                            self._speaker_audio[uuid] = (
                                cur_st.group_volume,
                                cur_st.group_muted,
                            )
                            self._speaker_audio_seq[uuid] = fetch_seq
                            updated_any = True
                if include_speakers:
                    for uuid, audio in polled_audio.items():
                        if (
                            uuid in valid_speakers
                            and self._speaker_audio_seq.get(uuid, 0) < fetch_seq
                        ):
                            self._speaker_audio[uuid] = audio
                            self._speaker_audio_seq[uuid] = fetch_seq
                            updated_any = True
                self._media_info_hints = hints
                self._active_uuids = {
                    g.coord_uuid
                    for g in self._groups
                    if (st := self._group_states.get(g.coord_uuid)) is not None
                    and (st.playback_status == "Playing" or st.has_track)
                }
                if polled_states or not failed_any:
                    self._last_snapshot_at = time.monotonic()
                    if updated_any:
                        self._snapshot_seq += 1
                        pub_seq = self._snapshot_seq
                payload = build_state_payload(
                    self._groups,
                    self._speakers,
                    self._group_states,
                    self._speaker_audio,
                    gena_active=self._gena.active,
                    cached=bool(failed_any and not polled_states),
                    library_version=self._library_version,
                )

            if updated_any and pub_seq is not None:
                self._schedule_mpris_update()
                self._broadcast_state(payload, seq=pub_seq)
            return payload

        def _fetch_full_snapshot(
            self,
            force_topology: bool = False,
            include_speakers: bool = True,
            selected_coord_uuid: str = "",
        ) -> dict:
            groups, speakers = self._ensure_topology(
                force=force_topology, interval=10.0
            )
            now = time.monotonic()
            with self.lock:
                fetch_seq = self._next_item_seq_locked()
                prev_states = dict(self._group_states)
                prev_state_keys = set(prev_states.keys())
                prev_audio = dict(self._speaker_audio)
                hints = set(self._media_info_hints)
                refresh_settings = (
                    force_topology
                    or not prev_states
                    or self._last_settings_refresh == 0.0
                    or (now - self._last_settings_refresh) >= SETTINGS_REFRESH_INTERVAL
                )
                if refresh_settings:
                    self._last_settings_refresh = now
                # Always ensure speaker audio is populated on the first snapshot.
                need_speakers = include_speakers or not prev_audio

            group_states, speaker_audio, failed_any = fetch_groups_batch(
                groups,
                speakers,
                self._soap_pool,
                include_speakers=need_speakers,
                known_active_uuids=None,
                prev_states=prev_states,
                prev_speaker_audio=prev_audio,
                media_info_hints=hints,
                selected_coord_uuid=selected_coord_uuid,
                refresh_all_settings=refresh_settings,
            )

            with self.lock:
                if failed_any:
                    self._last_topology = 0.0
                cur_groups = self._groups
                cur_speakers = self._speakers
                valid_group_uuids = {g.coord_uuid for g in cur_groups}
                valid_speaker_uuids = {s.uuid for s in cur_speakers}
                # Merge polled states onto existing valid groups using monotonic
                # fetch_seq so older slow polls never overwrite newer polls, GENA, or RPC.
                merged_states = {
                    k: v
                    for k, v in self._group_states.items()
                    if k in valid_group_uuids
                }
                audio_applied_uuids: set[str] = set()
                for uuid, st in group_states.items():
                    if uuid not in valid_group_uuids:
                        continue
                    fetched_settings = (
                        refresh_settings
                        or not selected_coord_uuid
                        or uuid == selected_coord_uuid
                        or uuid not in prev_state_keys
                    )
                    _, audio_applied = self._commit_group_state_locked(
                        merged_states,
                        uuid,
                        st,
                        fetch_seq,
                        has_audio=True,
                        has_settings=fetched_settings,
                    )
                    if audio_applied:
                        audio_applied_uuids.add(uuid)
                self._group_states = merged_states
                self._group_state_seq = {
                    k: v
                    for k, v in self._group_state_seq.items()
                    if k in valid_group_uuids
                }
                self._group_audio_seq = {
                    k: v
                    for k, v in self._group_audio_seq.items()
                    if k in valid_group_uuids
                }
                if need_speakers:
                    merged_audio = {
                        k: v
                        for k, v in self._speaker_audio.items()
                        if k in valid_speaker_uuids
                    }
                    for uuid, audio in speaker_audio.items():
                        if (
                            uuid in valid_speaker_uuids
                            and self._speaker_audio_seq.get(uuid, 0) < fetch_seq
                        ):
                            merged_audio[uuid] = audio
                            self._speaker_audio_seq[uuid] = fetch_seq
                    self._speaker_audio = merged_audio
                else:
                    # Keep cached per-member audio, updating single-room coordinators from group_states.
                    single_coords = {
                        g.coord_uuid for g in cur_groups if len(g.member_uuids) == 1
                    }
                    merged_audio = {
                        k: v
                        for k, v in self._speaker_audio.items()
                        if k in valid_speaker_uuids
                    }
                    for s in cur_speakers:
                        if (
                            s.is_coordinator
                            and s.coord_uuid in single_coords
                            and s.coord_uuid in audio_applied_uuids
                            and s.coord_uuid in merged_states
                            and self._speaker_audio_seq.get(s.uuid, 0) < fetch_seq
                        ):
                            st = merged_states[s.coord_uuid]
                            merged_audio[s.uuid] = (st.group_volume, st.group_muted)
                            self._speaker_audio_seq[s.uuid] = fetch_seq
                    self._speaker_audio = merged_audio
                self._speaker_audio_seq = {
                    k: v
                    for k, v in self._speaker_audio_seq.items()
                    if k in valid_speaker_uuids
                }
                self._media_info_hints = hints
                self._active_uuids = {
                    g.coord_uuid
                    for g in cur_groups
                    if (st := self._group_states.get(g.coord_uuid)) is not None
                    and (st.playback_status == "Playing" or st.has_track)
                }
                self._last_snapshot_at = time.monotonic()
                self._snapshot_seq += 1
                pub_seq = self._snapshot_seq
                payload = build_state_payload(
                    cur_groups,
                    cur_speakers,
                    self._group_states,
                    self._speaker_audio,
                    gena_active=self._gena.active,
                    cached=False,
                    library_version=self._library_version,
                )

            self._schedule_mpris_update()
            self._broadcast_state(payload, seq=pub_seq)
            return payload

        def _apply_mpris_on_main(
            self, snapshot: dict[str, tuple[ZoneGroup, TrackState]]
        ) -> bool:
            if self.stop_event.is_set():
                return False
            for uuid in list(self.players.keys()):
                if uuid not in snapshot:
                    self.players.pop(uuid).close()
            for uuid, (group, state) in snapshot.items():
                if uuid in self.players:
                    try:
                        self.players[uuid].update(group, state)
                    except Exception as exc:
                        _debug_log(f"SonosMprisPlayer({uuid}) update failed", exc)
                elif len(self.players) < MAX_MPRIS_PLAYERS:
                    try:
                        self.players[uuid] = SonosMprisPlayer(
                            group,
                            state,
                            on_action_cb=self.trigger_poll,
                            action_pool=self._action_pool,
                        )
                    except Exception as exc:
                        _debug_log(f"SonosMprisPlayer({uuid}) init failed", exc)
            return False

        def _find_group(self, coord_uuid: str) -> ZoneGroup | None:
            with self.lock:
                return find_group(self._groups, coord_uuid)

        def _find_speaker(self, speaker_uuid: str) -> SpeakerInfo | None:
            with self.lock:
                for s in self._speakers:
                    if s.uuid == speaker_uuid:
                        return s
                return None

        def _handle_rpc(self, req: dict) -> dict:
            cmd = str(req.get("cmd") or "").strip()
            coord_uuid = str(req.get("coord_uuid") or "").strip()

            if cmd == "diagnostics":
                return self._build_diagnostics()

            if cmd == "get_state":
                recovered = self._check_network_recovery()
                force_topo = bool(req.get("force_topology", False)) or recovered
                want_cached = bool(req.get("cached", False)) and not recovered
                include_speakers = bool(req.get("include_speakers", True))
                selected_coord = str(
                    req.get("selected_coord_uuid") or coord_uuid
                ).strip()
                if want_cached and not force_topo:
                    with self.lock:
                        if (
                            self._groups
                            and self._group_states
                            and self._last_snapshot_at > 0.0
                        ):
                            return build_state_payload(
                                self._groups,
                                self._speakers,
                                self._group_states,
                                self._speaker_audio,
                                gena_active=self._gena.active,
                                cached=True,
                                library_version=self._library_version,
                            )
                if coord_uuid and not force_topo:
                    with self.lock:
                        topo_valid = self._last_topology > 0.0
                        target_group = (
                            find_group(self._groups, coord_uuid)
                            if topo_valid
                            else None
                        )
                    if target_group is not None:
                        return self._fetch_group_snapshot(
                            target_group, include_speakers=include_speakers
                        )
                    if not topo_valid:
                        force_topo = True
                return self._fetch_full_snapshot(
                    force_topology=force_topo,
                    include_speakers=include_speakers,
                    selected_coord_uuid=selected_coord,
                )

            group = self._find_group(coord_uuid)

            if cmd == "refresh":
                with self.lock:
                    self._queue_cache.clear()
                self.trigger_poll(force_topology=True, full_sweep=True)
                return self._fetch_full_snapshot(force_topology=True)

            if cmd == "browse_library":
                force = bool(req.get("force", False))
                ok, items, err = self._load_library(
                    force=force, fallback_group=group
                )
                if not ok:
                    return {"ok": False, "error": err}
                return {"ok": True, "items": items}

            if cmd == "browse_queue":
                if not group:
                    return {"ok": False, "error": "no Sonos group"}
                limit = max(1, min(300, int(req.get("limit", 150))))
                force = bool(req.get("force", False))
                now = time.monotonic()
                if not force:
                    with self.lock:
                        cached_q = self._queue_cache.get(group.coord_uuid)
                        if (
                            cached_q is not None
                            and cached_q[0] >= limit
                            and (now - cached_q[1]) < QUEUE_CACHE_TTL
                        ):
                            return {
                                "ok": True,
                                "items": list(cached_q[2][:limit]),
                                "coord_uuid": group.coord_uuid,
                            }
                items = browse_content_directory(
                    group.coord_ip,
                    "Q:0",
                    limit=limit,
                    minimal=True,
                    raise_on_error=True,
                )
                with self.lock:
                    self._queue_cache[group.coord_uuid] = (
                        limit,
                        time.monotonic(),
                        items,
                    )
                return {"ok": True, "items": items, "coord_uuid": group.coord_uuid}

            if cmd in ("play", "pause", "play_pause", "next", "previous", "stop"):
                if not group:
                    return {"ok": False, "error": "no Sonos group"}
                if cmd == "play_pause":
                    with self.lock:
                        st = self._group_states.get(group.coord_uuid)
                    cmd = "pause" if (st and st.playback_status == "Playing") else "play"
                if cmd == "play":
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "Play",
                        "<InstanceID>0</InstanceID><Speed>1</Speed>",
                    )
                elif cmd == "pause":
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "Pause",
                        "<InstanceID>0</InstanceID>",
                    )
                elif cmd == "next":
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "Next",
                        "<InstanceID>0</InstanceID>",
                    )
                    with self.lock:
                        self._bump_group_seq_locked(group.coord_uuid)
                elif cmd == "previous":
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "Previous",
                        "<InstanceID>0</InstanceID>",
                    )
                    with self.lock:
                        self._bump_group_seq_locked(group.coord_uuid)
                elif cmd == "stop":
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "Stop",
                        "<InstanceID>0</InstanceID>",
                    )
                if cmd in ("play", "pause", "stop"):
                    now = time.monotonic()
                    new_status = (
                        "Playing"
                        if cmd == "play"
                        else ("Paused" if cmd == "pause" else "Stopped")
                    )
                    with self.lock:
                        st = self._group_states.get(group.coord_uuid)
                        if st is not None:
                            if new_status == "Playing":
                                next_pos = (
                                    st.position_sec
                                    if st.playback_status != "Playing"
                                    else extrapolate_position_sec(st, now)
                                )
                            elif new_status == "Paused":
                                next_pos = extrapolate_position_sec(st, now)
                            else:
                                next_pos = 0
                            self._group_states[group.coord_uuid] = replace(
                                st,
                                playback_status=new_status,
                                position_sec=next_pos,
                                position_at=now,
                            )
                            self._bump_group_seq_locked(group.coord_uuid)
                            self._snapshot_seq += 1
                            self._last_snapshot_at = now
                if not self._gena.active:
                    self.trigger_poll()
                return {"ok": True}

            if cmd == "seek":
                if not group:
                    return {"ok": False, "error": "no Sonos group"}
                sec = min(MAX_DURATION_SEC, max(0, int(req.get("seconds", 0))))
                soap_call(
                    group.coord_ip,
                    "/MediaRenderer/AVTransport/Control",
                    "AVTransport",
                    "Seek",
                    f"<InstanceID>0</InstanceID><Unit>REL_TIME</Unit><Target>{format_sec_to_hms(sec)}</Target>",
                )
                now = time.monotonic()
                with self.lock:
                    st = self._group_states.get(group.coord_uuid)
                    if st is not None:
                        self._group_states[group.coord_uuid] = replace(
                            st, position_sec=sec, position_at=now
                        )
                        self._bump_group_seq_locked(group.coord_uuid)
                        self._snapshot_seq += 1
                        self._last_snapshot_at = now
                self._schedule_mpris_update()
                self._broadcast_state()
                if not self._gena.active:
                    self.trigger_poll()
                return {"ok": True}

            if cmd == "set_play_mode":
                if not group:
                    return {"ok": False, "error": "no Sonos group"}
                shuf = bool(req.get("shuffle", False))
                rep = str(req.get("repeat", "none"))
                mode = encode_play_mode(shuf, rep)
                soap_call(
                    group.coord_ip,
                    "/MediaRenderer/AVTransport/Control",
                    "AVTransport",
                    "SetPlayMode",
                    f"<InstanceID>0</InstanceID><NewPlayMode>{mode}</NewPlayMode>",
                )
                with self.lock:
                    cur_st = self._group_states.get(group.coord_uuid)
                    if cur_st is not None:
                        dec_shuf, dec_rep = decode_play_mode(mode)
                        self._group_states[group.coord_uuid] = replace(
                            cur_st, shuffle=dec_shuf, repeat=dec_rep
                        )
                        self._snapshot_seq += 1
                        self._last_snapshot_at = time.monotonic()
                    self._bump_group_seq_locked(group.coord_uuid)
                self._schedule_mpris_update()
                self._broadcast_state()
                if not self._gena.active:
                    self.trigger_poll()
                return {"ok": True, "mode": mode}

            if cmd == "set_group_volume":
                if not group:
                    return {"ok": False, "error": "no Sonos group"}
                vol = max(0, min(100, int(req.get("volume", 0))))
                if len(group.member_uuids) > 1:
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/GroupRenderingControl/Control",
                        "GroupRenderingControl",
                        "SetGroupVolume",
                        f"<InstanceID>0</InstanceID><DesiredVolume>{vol}</DesiredVolume>",
                    )
                else:
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/RenderingControl/Control",
                        "RenderingControl",
                        "SetVolume",
                        f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>{vol}</DesiredVolume>",
                    )
                with self.lock:
                    self._bump_group_audio_seq_locked(group.coord_uuid)
                    for muuid in group.member_uuids:
                        self._bump_speaker_seq_locked(muuid)
                if not self._gena.active:
                    self.trigger_poll()
                return {"ok": True}

            if cmd == "set_group_mute":
                if not group:
                    return {"ok": False, "error": "no Sonos group"}
                muted = 1 if req.get("muted") else 0
                if len(group.member_uuids) > 1:
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/GroupRenderingControl/Control",
                        "GroupRenderingControl",
                        "SetGroupMute",
                        f"<InstanceID>0</InstanceID><DesiredMute>{muted}</DesiredMute>",
                    )
                else:
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/RenderingControl/Control",
                        "RenderingControl",
                        "SetMute",
                        f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredMute>{muted}</DesiredMute>",
                    )
                with self.lock:
                    self._bump_group_audio_seq_locked(group.coord_uuid)
                    for muuid in group.member_uuids:
                        self._bump_speaker_seq_locked(muuid)
                if not self._gena.active:
                    self.trigger_poll()
                return {"ok": True}

            if cmd == "set_speaker_volume":
                sp = self._find_speaker(str(req.get("speaker_uuid") or ""))
                if not sp:
                    return {"ok": False, "error": "unknown speaker"}
                vol = max(0, min(100, int(req.get("volume", 0))))
                soap_call(
                    sp.ip,
                    "/MediaRenderer/RenderingControl/Control",
                    "RenderingControl",
                    "SetVolume",
                    f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>{vol}</DesiredVolume>",
                )
                with self.lock:
                    self._bump_speaker_seq_locked(sp.uuid)
                    if sp.coord_uuid:
                        self._bump_group_audio_seq_locked(sp.coord_uuid)
                if not self._gena.active:
                    self.trigger_poll()
                return {"ok": True}

            if cmd == "set_speaker_mute":
                sp = self._find_speaker(str(req.get("speaker_uuid") or ""))
                if not sp:
                    return {"ok": False, "error": "unknown speaker"}
                muted = 1 if req.get("muted") else 0
                soap_call(
                    sp.ip,
                    "/MediaRenderer/RenderingControl/Control",
                    "RenderingControl",
                    "SetMute",
                    f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredMute>{muted}</DesiredMute>",
                )
                with self.lock:
                    self._bump_speaker_seq_locked(sp.uuid)
                    if sp.coord_uuid:
                        self._bump_group_audio_seq_locked(sp.coord_uuid)
                if not self._gena.active:
                    self.trigger_poll()
                return {"ok": True}

            if cmd == "join_group":
                sp = self._find_speaker(str(req.get("speaker_uuid") or ""))
                target = self._find_group(str(req.get("target_coord_uuid") or coord_uuid))
                if not sp or not target:
                    return {"ok": False, "error": "invalid speaker or target"}
                if sp.uuid != target.coord_uuid:
                    soap_call(
                        sp.ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "SetAVTransportURI",
                        "<InstanceID>0</InstanceID>"
                        f"<CurrentURI>{xml_escape(f'x-rincon:{target.coord_uuid}')}</CurrentURI>"
                        "<CurrentURIMetaData></CurrentURIMetaData>",
                    )
                with self.lock:
                    self._queue_cache.clear()
                self.trigger_poll(force_topology=True, full_sweep=True)
                return {"ok": True}

            if cmd == "leave_group":
                sp = self._find_speaker(str(req.get("speaker_uuid") or ""))
                if not sp:
                    return {"ok": False, "error": "unknown speaker"}
                soap_call(
                    sp.ip,
                    "/MediaRenderer/AVTransport/Control",
                    "AVTransport",
                    "BecomeCoordinatorOfStandaloneGroup",
                    "<InstanceID>0</InstanceID>",
                )
                with self.lock:
                    self._queue_cache.clear()
                self.trigger_poll(force_topology=True, full_sweep=True)
                return {"ok": True}

            if cmd == "play_item":
                if not group:
                    return {"ok": False, "error": "no Sonos group"}
                uri = str(req.get("uri") or "").strip()
                metadata = str(req.get("metadata") or "")
                as_next = bool(req.get("as_next", False))
                if not uri:
                    return {"ok": False, "error": "missing uri"}
                gena_live = self._gena.active
                with self.lock:
                    st = self._group_states.get(group.coord_uuid)
                    # Only trust cached current_uri to elide SetAVTransportURI when GENA
                    # keeps AVTransportURI authoritative; polling skips GetMediaInfo.
                    cur_uri = (st.current_uri if st is not None else "") if gena_live else ""
                    already_playing = bool(gena_live and st and st.playback_status == "Playing")
                new_uri = play_uri_on_coordinator(
                    group,
                    uri,
                    metadata,
                    as_next=as_next,
                    current_uri=cur_uri,
                    already_playing=already_playing,
                )
                now = time.monotonic()
                with self.lock:
                    self._queue_cache.pop(group.coord_uuid, None)
                    if not as_next and new_uri:
                        cur_st = self._group_states.get(group.coord_uuid)
                        if cur_st is not None:
                            self._group_states[group.coord_uuid] = replace(
                                cur_st,
                                current_uri=new_uri[:1024],
                                playback_status="Playing",
                                position_at=now,
                            )
                            self._bump_group_seq_locked(group.coord_uuid)
                            self._snapshot_seq += 1
                            self._last_snapshot_at = now
                if not self._gena.active:
                    self.trigger_poll()
                return {"ok": True}

            if cmd == "play_queue_track":
                if not group:
                    return {"ok": False, "error": "no Sonos group"}
                track_num = max(1, int(req.get("track_num", 1)))
                queue_uri = f"x-rincon-queue:{group.coord_uuid}#0"
                gena_live = self._gena.active
                with self.lock:
                    st = self._group_states.get(group.coord_uuid)
                    already_on_queue = bool(gena_live and st and st.current_uri == queue_uri)
                    already_playing = bool(gena_live and st and st.playback_status == "Playing")
                if not already_on_queue:
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "SetAVTransportURI",
                        "<InstanceID>0</InstanceID>"
                        f"<CurrentURI>{xml_escape(queue_uri)}</CurrentURI>"
                        "<CurrentURIMetaData></CurrentURIMetaData>",
                    )
                try:
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "Seek",
                        f"<InstanceID>0</InstanceID><Unit>TRACK_NR</Unit><Target>{track_num}</Target>",
                    )
                except Exception:
                    if not already_on_queue:
                        raise
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "SetAVTransportURI",
                        "<InstanceID>0</InstanceID>"
                        f"<CurrentURI>{xml_escape(queue_uri)}</CurrentURI>"
                        "<CurrentURIMetaData></CurrentURIMetaData>",
                    )
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "Seek",
                        f"<InstanceID>0</InstanceID><Unit>TRACK_NR</Unit><Target>{track_num}</Target>",
                    )
                    already_on_queue = False
                if not already_on_queue or not already_playing:
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "Play",
                        "<InstanceID>0</InstanceID><Speed>1</Speed>",
                    )
                now = time.monotonic()
                with self.lock:
                    cur_st = self._group_states.get(group.coord_uuid)
                    if cur_st is not None:
                        self._group_states[group.coord_uuid] = replace(
                            cur_st,
                            current_uri=queue_uri,
                            playback_status="Playing",
                            track_num=track_num,
                            position_sec=0,
                            position_at=now,
                        )
                        self._bump_group_seq_locked(group.coord_uuid)
                        self._snapshot_seq += 1
                        self._last_snapshot_at = now
                self._schedule_mpris_update()
                self._broadcast_state()
                if not self._gena.active:
                    self.trigger_poll()
                return {"ok": True}

            return {"ok": False, "error": f"unknown command: {cmd}"}

        def _rpc_server_loop(self) -> None:
            sock_path = socket_file_path()
            try:
                if os.path.exists(sock_path):
                    os.unlink(sock_path)
            except OSError:
                pass
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._rpc_sock = srv
            try:
                srv.bind(sock_path)
                os.chmod(sock_path, 0o600)
                srv.listen(16)
                srv.settimeout(1.0)
                while not self.stop_event.is_set():
                    try:
                        conn, _ = srv.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    try:
                        self._rpc_pool.submit(self._serve_client, conn)
                    except RuntimeError:
                        try:
                            conn.close()
                        except OSError:
                            pass
            finally:
                try:
                    srv.close()
                except OSError:
                    pass
                try:
                    if os.path.exists(sock_path):
                        os.unlink(sock_path)
                except OSError:
                    pass

        def _serve_client(self, conn: socket.socket) -> None:
            keep_open = False
            try:
                conn.settimeout(4.0)
                data = b""
                while b"\n" not in data and len(data) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                req = json.loads(data.decode("utf-8", "replace").strip() or "{}")
                if str(req.get("cmd") or "").strip() == "subscribe":
                    keep_open = True
                    self._add_event_client(conn)
                    return
                try:
                    resp = self._handle_rpc(req)
                except Exception as exc:
                    _debug_log(f"RPC {req.get('cmd')} failed", exc)
                    resp = {"ok": False, "error": str(exc)}
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
            except Exception as exc:
                _debug_log("RPC client error", exc)
            finally:
                if not keep_open:
                    try:
                        conn.close()
                    except OSError:
                        pass

        def _poll_loop(self) -> None:
            while not self.stop_event.is_set():
                recovered = self._check_network_recovery()
                now = time.monotonic()
                with self.lock:
                    had_active = bool(self._active_uuids)
                    last_snapshot_at = self._last_snapshot_at
                    explicit_wake = self._explicit_wake or recovered
                    self._explicit_wake = False

                # When UPnP GENA push is active, renew/sync subscriptions on a quiet
                # heartbeat instead of polling speakers every 2 seconds.
                force_topo_poll = recovered
                if self._gena.active and not explicit_wake:
                    with self.lock:
                        groups = list(self._groups)
                        speakers = list(self._speakers)
                        topo_ip = _RTT.pick_preferred(
                            list(self._seed_ips), current=self._preferred_ip
                        ) or (self._seed_ips[0] if self._seed_ips else "")
                        if topo_ip:
                            self._preferred_ip = topo_ip
                    if groups:
                        still_active = self._gena.sync_subscriptions(
                            groups, speakers, topo_ip, self._soap_pool
                        )
                        if still_active and self._gena.has_kind("topology"):
                            self.wake_event.wait(timeout=GENA_HEARTBEAT_INTERVAL)
                            self.wake_event.clear()
                            continue
                        if not still_active:
                            with self.lock:
                                self._gena_probe_started = False
                                self._snapshot_seq += 1
                            force_topo_poll = True
                            explicit_wake = True
                            self._broadcast_state()
                        else:
                            _debug_log(
                                "GENA topology subscription missing; forcing topology failover"
                            )
                            force_topo_poll = True
                            explicit_wake = True

                # If the open panel just fetched a full snapshot, avoid duplicating LAN polls.
                recent_interval = compute_poll_interval(had_active, True)
                grace = 0.2 if had_active else 0.0
                if (
                    not explicit_wake
                    and last_snapshot_at > 0.0
                    and (now - last_snapshot_at) < (recent_interval + grace)
                ):
                    wait_s = max(0.2, (recent_interval + grace) - (now - last_snapshot_at))
                    self.wake_event.wait(timeout=wait_s)
                    self.wake_event.clear()
                    continue

                topo_interval = (
                    TOPOLOGY_ACTIVE_INTERVAL if had_active else TOPOLOGY_IDLE_INTERVAL
                )
                groups, speakers = self._ensure_topology(
                    force=force_topo_poll, interval=topo_interval
                )

                if not groups:
                    self._schedule_mpris_update()
                    self.wake_event.wait(
                        timeout=compute_poll_interval(False, False)
                    )
                    self.wake_event.clear()
                    continue

                with self.lock:
                    fetch_seq = self._next_item_seq_locked()
                    full_sweep = (
                        self._last_full_sweep == 0.0
                        or (now - self._last_full_sweep) >= IDLE_SWEEP_INTERVAL
                    )
                    if full_sweep:
                        self._last_full_sweep = now
                    active_uuids = set(self._active_uuids)
                    prev_states = dict(self._group_states)
                    hints = set(self._media_info_hints)

                if full_sweep or not active_uuids:
                    targets = groups
                else:
                    targets = [
                        g
                        for g in groups
                        if g.coord_uuid in active_uuids
                        or g.coord_uuid not in prev_states
                    ]
                    if not targets:
                        targets = groups

                # Closed-panel loop: poll target coordinators in parallel (skipping
                # per-speaker volumes and stopped-speaker metadata) with zero state files.
                polled_states, _, failed_any = fetch_groups_batch(
                    targets,
                    [],
                    self._soap_pool,
                    include_speakers=False,
                    known_active_uuids=active_uuids,
                    prev_states=prev_states,
                    media_info_hints=hints,
                )

                next_active: set[str] = set()
                any_playing = False
                should_publish = False

                with self.lock:
                    if failed_any:
                        self._last_topology = 0.0
                    self._media_info_hints = hints
                    cur_groups = self._groups
                    valid_uuids = {g.coord_uuid for g in cur_groups}
                    valid_speaker_uuids = {s.uuid for s in self._speakers}
                    single_uuids = {
                        g.coord_uuid for g in cur_groups if len(g.member_uuids) == 1
                    }
                    self._group_state_seq = {
                        k: v for k, v in self._group_state_seq.items() if k in valid_uuids
                    }
                    self._group_audio_seq = {
                        k: v for k, v in self._group_audio_seq.items() if k in valid_uuids
                    }
                    self._speaker_audio_seq = {
                        k: v
                        for k, v in self._speaker_audio_seq.items()
                        if k in valid_speaker_uuids
                    }
                    merged = {
                        k: v
                        for k, v in self._group_states.items()
                        if k in valid_uuids
                    }
                    applied_any = False
                    for uuid, st in polled_states.items():
                        if uuid not in valid_uuids:
                            continue
                        fetched_detail = (
                            (uuid in active_uuids) or (st.playback_status != "Stopped")
                        )
                        applied, audio_applied = self._commit_group_state_locked(
                            merged,
                            uuid,
                            st,
                            fetch_seq,
                            has_audio=fetched_detail,
                            has_settings=fetched_detail,
                        )
                        if applied:
                            if (
                                not fetched_detail
                                and uuid in single_uuids
                                and self._group_audio_seq.get(uuid, 0) == 0
                                and (sp_aud := self._speaker_audio.get(uuid)) is not None
                            ):
                                merged[uuid] = replace(
                                    merged[uuid],
                                    group_volume=sp_aud[0],
                                    group_muted=sp_aud[1],
                                )
                                self._bump_group_audio_seq_locked(uuid)
                            elif (
                                audio_applied
                                and uuid in single_uuids
                                and uuid in self._speaker_audio
                                and self._speaker_audio_seq.get(uuid, 0) < fetch_seq
                            ):
                                self._speaker_audio[uuid] = (
                                    merged[uuid].group_volume,
                                    merged[uuid].group_muted,
                                )
                                self._speaker_audio_seq[uuid] = fetch_seq
                            applied_any = True
                    for g in cur_groups:
                        st = merged.get(g.coord_uuid)
                        if not st:
                            continue
                        if st.playback_status == "Playing":
                            any_playing = True
                        if st.playback_status == "Playing" or st.has_track:
                            next_active.add(g.coord_uuid)
                    if applied_any or set(merged.keys()) != set(self._group_states.keys()):
                        self._group_states = merged
                        self._active_uuids = next_active
                        self._snapshot_seq += 1
                        self._last_snapshot_at = time.monotonic()
                        should_publish = True

                if should_publish:
                    self._schedule_mpris_update()
                    self._broadcast_state()

                self._maybe_prewarm_library()

                if self._gena.active:
                    with self.lock:
                        cur_groups = list(self._groups)
                        cur_speakers = list(self._speakers)
                        topo_ip = _RTT.pick_preferred(
                            list(self._seed_ips), current=self._preferred_ip
                        ) or (self._seed_ips[0] if self._seed_ips else "")
                        if topo_ip:
                            self._preferred_ip = topo_ip
                    still_active = self._gena.sync_subscriptions(
                        cur_groups, cur_speakers, topo_ip, self._soap_pool
                    )
                    if not still_active:
                        with self.lock:
                            self._gena_probe_started = False
                            self._snapshot_seq += 1
                        self._broadcast_state()
                        interval = compute_poll_interval(any_playing, bool(cur_groups))
                    elif not self._gena.has_kind("topology"):
                        interval = compute_poll_interval(any_playing, bool(cur_groups))
                    else:
                        interval = GENA_HEARTBEAT_INTERVAL
                else:
                    interval = compute_poll_interval(any_playing, bool(groups))
                self.wake_event.wait(timeout=interval)
                self.wake_event.clear()

        def start(self) -> None:
            threading.Thread(target=self._rpc_server_loop, daemon=True).start()
            threading.Thread(target=self._poll_loop, daemon=True).start()

        def shutdown(self) -> None:
            self.stop_event.set()
            self.wake_event.set()
            self._gena.shutdown()
            with self._event_lock:
                clients = list(self._event_clients.keys())
                self._event_clients.clear()
            for conn in clients:
                try:
                    conn.close()
                except OSError:
                    pass
            if self._rpc_sock is not None:
                try:
                    self._rpc_sock.close()
                except OSError:
                    pass
            for pool in (self._soap_pool, self._rpc_pool, self._action_pool):
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
            _HTTP_POOL.close_all()
            for player in list(self.players.values()):
                player.close()
            self.players.clear()
            if self.loop.is_running():
                self.loop.quit()

    set_parent_death_signal()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    loop = GLib.MainLoop()
    controller = BridgeController(loop)

    def handle_signal(_signum, _frame) -> None:
        GLib.idle_add(controller.shutdown)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    controller.start()
    try:
        loop.run()
    finally:
        controller.shutdown()
        try:
            os.close(lock_fd)
        except OSError:
            pass
    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--status":
        return send_rpc_client('{"cmd":"diagnostics"}', pretty=True)
    if len(sys.argv) >= 3 and sys.argv[1] == "--rpc":
        return send_rpc_client(sys.argv[2])
    return run_daemon()


if __name__ == "__main__":
    sys.exit(main())
