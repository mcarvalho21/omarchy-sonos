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
from dataclasses import dataclass, replace

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
QUEUE_CACHE_TTL = 15.0
SERVICE_CACHE_RETRY_INTERVAL = 60.0
GENA_DEFAULT_PORT = 14001
GENA_SUB_TIMEOUT_SEC = 1800
GENA_PROBE_WAIT_SEC = 2.5
GENA_HEARTBEAT_INTERVAL = 60.0


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
        with self._lock:
            snap = dict(self._ewma)
        return sorted(ips, key=lambda ip: snap.get(ip, 500.0))

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


def is_valid_private_ipv4(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value)
        return isinstance(addr, ipaddress.IPv4Address) and (
            addr.is_private or addr.is_link_local
        )
    except ValueError:
        return False


def parse_hms_to_sec(text: str) -> int:
    if not text or text in ("NOT_IMPLEMENTED", "0:00:00", "00:00:00"):
        return 0
    parts = text.strip().split(":")
    if len(parts) != 3:
        return 0
    try:
        h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
        if h < 0 or m < 0 or s < 0:
            return 0
        return int(h * 3600 + m * 60 + s)
    except ValueError:
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
            except (
                http.client.HTTPException,
                BrokenPipeError,
                ConnectionResetError,
                OSError,
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

    @property
    def duration_us(self) -> int:
        return self.duration_sec * 1_000_000

    @property
    def position_us(self) -> int:
        return self.position_sec * 1_000_000

    @property
    def has_track(self) -> bool:
        return bool(self.title or self.artist)


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
        desc = ET.fromstring(resp).findtext(".//AvailableServiceDescriptorList") or ""
        if desc:
            sroot = ET.fromstring(desc)
            for el in sroot.findall(".//Service"):
                sid = el.attrib.get("Id")
                name = (el.attrib.get("Name") or "").strip()
                if sid and name:
                    result[sid] = name
            fetched = True
    except Exception:
        pass

    with _SERVICE_NAMES_LOCK:
        if fetched:
            _SERVICE_NAMES_CACHE = result
            _SERVICE_NAMES_FETCHED = True
            return dict(_SERVICE_NAMES_CACHE)
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
    if "radio" in low or track_uri.startswith(("x-sonosapi-stream:", "x-rincon-mp3radio:", "aac:", "hls-radio:")):
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
        zroot = ET.fromstring(state_xml)
    except ET.ParseError:
        return [], []

    groups: list[ZoneGroup] = []
    speakers: list[SpeakerInfo] = []
    for zg in zroot.findall(".//ZoneGroup"):
        coord_uuid = zg.attrib.get("Coordinator", "")
        coord_ip = ""
        coord_name = ""
        member_uuids: list[str] = []
        member_names: list[str] = []
        for m in zg.findall("ZoneGroupMember"):
            if m.attrib.get("Invisible") == "1":
                continue
            uuid = (m.attrib.get("UUID") or "").strip()
            name = (m.attrib.get("ZoneName") or "").strip()
            loc = m.attrib.get("Location") or ""
            mip = urllib.parse.urlparse(loc).hostname or ""
            if not uuid or not is_valid_private_ipv4(mip):
                continue
            is_coord = uuid == coord_uuid
            member_uuids.append(uuid)
            member_names.append(name or mip)
            speakers.append(
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


def fetch_topology(seed_ips: list[str]) -> tuple[list[ZoneGroup], list[SpeakerInfo]]:
    for ip in seed_ips:
        try:
            resp = soap_call(
                ip,
                "/ZoneGroupTopology/Control",
                "ZoneGroupTopology",
                "GetZoneGroupState",
            )
            root = ET.fromstring(resp)
            state_xml = root.findtext(".//ZoneGroupState")
            if not state_xml:
                continue
            groups, speakers = parse_zone_group_state(state_xml)
            if groups:
                return groups, speakers
        except Exception:
            continue
    return [], []


def resolve_art_url(ip: str, raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        return f"http://{ip}:1400{raw}"
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme in ("http", "https"):
        return raw
    return ""


def parse_didl_metadata(ip: str, meta_xml: str) -> tuple[str, str, str, str]:
    if not meta_xml or meta_xml == "NOT_IMPLEMENTED":
        return "", "", "", ""
    try:
        mroot = ET.fromstring(meta_xml)
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


def _parse_int_tag(xml_text: str | None, tag: str, min_val: int = 0, max_val: int = 100) -> int:
    if not xml_text:
        return 0
    try:
        raw = ET.fromstring(xml_text).findtext(f".//{tag}")
        if raw is not None:
            return max(min_val, min(max_val, int(raw)))
    except Exception:
        pass
    return 0


def _parse_bool_tag(xml_text: str | None, tag: str) -> bool:
    if not xml_text:
        return False
    try:
        raw = ET.fromstring(xml_text).findtext(f".//{tag}")
        if raw is not None:
            return raw.strip() in ("1", "true", "True")
    except Exception:
        pass
    return False


def _parse_transport_status(xml_text: str) -> str:
    tstate = (ET.fromstring(xml_text).findtext(".//CurrentTransportState") or "").strip()
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
        proot = ET.fromstring(xml_text)
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
        mroot = ET.fromstring(xml_text)
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
        play_mode = ET.fromstring(xml_text).findtext(".//PlayMode") or "NORMAL"
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

    can_next = can_prev = True
    if track_uri.startswith(("x-sonos-htastream:", "x-rincon-stream:")):
        can_next = False
        can_prev = False
        if not title:
            title = "TV Audio" if "htastream" in track_uri else "Line-In"

    source_name, source_icon = detect_audio_source(ip, track_uri, current_uri)
    if settings_xml is not None:
        shuffle, repeat = _parse_play_mode_xml(settings_xml)
    elif fallback_play_mode is not None:
        shuffle, repeat = fallback_play_mode
    else:
        shuffle, repeat = False, "none"
    group_vol = _parse_int_tag(vol_xml, "CurrentVolume") if include_audio else 0
    group_muted = _parse_bool_tag(mute_xml, "CurrentMute") if include_audio else False

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
    )


def _soap_safe(ip: str, path: str, service: str, action: str, body: str = "") -> str | None:
    try:
        return soap_call(ip, path, service, action, body)
    except Exception:
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
        )

    if include_speakers:
        for s in speakers:
            if s.is_coordinator and s.coord_uuid in single_coords and s.coord_uuid in group_states:
                st = group_states[s.coord_uuid]
                speaker_audio[s.uuid] = (st.group_volume, st.group_muted)
            elif s.uuid in speaker_futs:
                vfut, mfut = speaker_futs[s.uuid]
                speaker_audio[s.uuid] = (
                    _parse_int_tag(vfut.result(), "CurrentVolume"),
                    _parse_bool_tag(mfut.result(), "CurrentMute"),
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
    ip: str, object_id: str, limit: int = 100, *, minimal: bool = False
) -> list[dict]:
    items: list[dict] = []
    start = 0
    batch = min(200, max(1, limit))
    while len(items) < limit:
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
            root = ET.fromstring(resp)
            returned = int(root.findtext(".//NumberReturned") or "0")
            total = int(root.findtext(".//TotalMatches") or "0")
            res_xml = root.findtext(".//Result") or ""
            if returned <= 0 or not res_xml:
                break
            rroot = ET.fromstring(res_xml)
            for idx, el in enumerate(list(rroot)):
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
            start += returned
            if start >= total:
                break
        except Exception:
            break
    return items


def play_uri_on_coordinator(
    group: ZoneGroup, uri: str, metadata: str, as_next: bool = False
) -> None:
    ip = group.coord_ip
    is_container = uri.startswith(
        (
            "x-rincon-cpcontainer:",
            "x-rincon-playlist:",
            "file:///jffs/settings/savedqueues.rsq",
        )
    )
    is_stream = uri.startswith(
        (
            "x-sonosapi-stream:",
            "x-sonosapi-radio:",
            "x-rincon-mp3radio:",
            "aac:",
            "hls-radio:",
        )
    )

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
        return

    if is_container or not is_stream:
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
            soap_call(
                ip,
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "SetAVTransportURI",
                "<InstanceID>0</InstanceID>"
                f"<CurrentURI>{xml_escape(f'x-rincon-queue:{group.coord_uuid}#0')}</CurrentURI>"
                "<CurrentURIMetaData></CurrentURIMetaData>",
            )
            soap_call(
                ip,
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "Play",
                "<InstanceID>0</InstanceID><Speed>1</Speed>",
            )
            return
        except Exception:
            pass

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


def build_state_payload(
    groups: list[ZoneGroup],
    speakers: list[SpeakerInfo],
    group_states: dict[str, TrackState],
    speaker_audio: dict[str, tuple[int, bool]],
    *,
    gena_active: bool = False,
    cached: bool = False,
) -> dict:
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
                "position_sec": st.position_sec,
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
    return {
        "ok": True,
        "timestamp": int(time.time()),
        "gena_active": gena_active,
        "cached": cached,
        "groups": g_list,
        "speakers": s_list,
    }


def send_rpc_client(payload_str: str) -> int:
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
        root = ET.fromstring(xml_body)
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
        lroot = ET.fromstring(lc)
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


def parse_gena_avtransport_event(
    ip: str, xml_body: str, prev: TrackState | None
) -> tuple[TrackState | None, bool, bool]:
    """Parse an AVTransport GENA LastChange event onto prev TrackState.

    Returns (updated_state, need_position_sync, queue_maybe_changed).
    """
    props = _parse_gena_properties(xml_body)
    lc = props.get("LastChange", "")
    if not lc:
        return None, False, False
    try:
        lroot = ET.fromstring(lc)
    except ET.ParseError:
        return None, False, False

    vals: dict[str, str] = {}
    for el in lroot.iter():
        if "val" in el.attrib:
            vals[_xml_local(el.tag)] = el.attrib["val"]
    if not vals:
        return None, False, False

    base = prev if prev is not None else _stopped_track_state()
    status = base.playback_status
    if "TransportState" in vals:
        tstate = vals["TransportState"].strip()
        if tstate in ("PLAYING", "TRANSITIONING"):
            status = "Playing"
        elif tstate == "PAUSED_PLAYBACK":
            status = "Paused"
        else:
            status = "Stopped"

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

    has_meta = (
        "CurrentTrackMetaData" in vals or "AVTransportURIMetaData" in vals
    )
    has_uri = ("CurrentTrackURI" in vals) or ("AVTransportURI" in vals)
    if has_meta or has_uri:
        track_uri = (vals.get("CurrentTrackURI") or "").strip()
        current_uri = (vals.get("AVTransportURI") or "").strip()
        if has_meta:
            t_title, t_artist, t_album, t_art = parse_didl_metadata(
                ip, vals.get("CurrentTrackMetaData") or ""
            )
            m_title, m_artist, m_album, m_art = parse_didl_metadata(
                ip, vals.get("AVTransportURIMetaData") or ""
            )
            title = t_title or m_title
            artist = t_artist or m_artist
            album = t_album or m_album
            art_url = t_art or m_art
        if has_uri:
            can_next = can_prev = True
            if track_uri.startswith(("x-sonos-htastream:", "x-rincon-stream:")):
                can_next = False
                can_prev = False
                if not title:
                    title = "TV Audio" if "htastream" in track_uri else "Line-In"
            src_name, src_icon = detect_audio_source(ip, track_uri, current_uri)
            if src_name or not (track_uri or current_uri):
                source_name, source_icon = src_name, src_icon

    track_changed = (title != base.title) or (track_num != base.track_num)
    status_changed = status != base.playback_status
    position_sec = 0 if track_changed else base.position_sec

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
    )
    need_pos_sync = track_changed or status_changed
    queue_changed = ("NumberOfTracks" in vals) or ("AVTransportURI" in vals)
    return updated, need_pos_sync, queue_changed


@dataclass
class _GenaSub:
    sid: str
    ip: str
    path: str
    kind: str  # topology | avtransport | group_rc | rc
    target_uuid: str
    expires_at: float

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.kind, self.ip, self.path, self.target_uuid)


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
        self._state = "unprobed"  # unprobed | probing | active | unavailable
        self._subs: dict[str, _GenaSub] = {}
        self._server: http.server.ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
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
    def callback_url(self) -> str:
        with self._lock:
            return self._callback_url

    def _start_server(self, local_ip: str) -> bool:
        manager = self

        class _GenaHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 3.0

            def log_message(self, _format: str, *_args) -> None:
                return

            def do_NOTIFY(self) -> None:  # noqa: N802
                peer_ip = self.client_address[0] if self.client_address else ""
                if not is_valid_private_ipv4(peer_ip):
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
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

                manager._mark_notify()
                try:
                    manager._on_event(sub, body_bytes.decode("utf-8", "replace"))
                except Exception:
                    pass

        class _GenaHTTPServer(http.server.ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True

        try:
            pref_port = int(os.environ.get("MC_SONOS_GENA_PORT", str(GENA_DEFAULT_PORT)))
        except ValueError:
            pref_port = GENA_DEFAULT_PORT
        pref_port = pref_port if 0 <= pref_port <= 65535 else GENA_DEFAULT_PORT

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
            self._server = srv
            self._callback_url = f"http://{local_ip}:{bound_port}/notify"

        t = threading.Thread(
            target=srv.serve_forever,
            kwargs={"poll_interval": 0.5},
            daemon=True,
            name="sonos-gena-http",
        )
        t.start()
        with self._lock:
            self._server_thread = t
        return True

    def _stop_server(self) -> None:
        with self._lock:
            srv = self._server
            self._server = None
            self._server_thread = None
            self._callback_url = ""
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass

    def lookup_sid(self, sid: str) -> _GenaSub | None:
        if not sid:
            return None
        with self._lock:
            return self._subs.get(sid)

    def _mark_notify(self) -> None:
        with self._lock:
            self._last_notify_at = time.monotonic()
        self._first_notify.set()

    def _subscribe(
        self, ip: str, path: str, kind: str, target_uuid: str
    ) -> _GenaSub | None:
        cb_url = self.callback_url
        if not cb_url or not is_valid_private_ipv4(ip):
            return None
        conn = http.client.HTTPConnection(ip, 1400, timeout=HTTP_TIMEOUT)
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
                return None
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
            )
            with self._lock:
                self._subs[sid] = sub
            return sub
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _renew(self, sub: _GenaSub) -> bool:
        conn = http.client.HTTPConnection(sub.ip, 1400, timeout=HTTP_TIMEOUT)
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
                return False
            ttl = _parse_gena_timeout(resp.getheader("TIMEOUT"))
            with self._lock:
                if sub.sid in self._subs:
                    self._subs[sub.sid].expires_at = time.monotonic() + ttl
            return True
        except Exception:
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _unsubscribe(self, sub: _GenaSub, timeout: float = 1.0) -> None:
        with self._lock:
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
            if self._state != "unprobed":
                return self._state == "active"
            self._state = "probing"
            self._first_notify.clear()

        local_ip = detect_local_ip_for(seed_ip)
        if not local_ip or not self._start_server(local_ip):
            with self._lock:
                self._state = "unavailable"
            return False

        sub = self._subscribe(seed_ip, "/ZoneGroupTopology/Event", "topology", seed_ip)
        if sub is None:
            self._stop_server()
            with self._lock:
                self._state = "unavailable"
            return False

        if self._first_notify.wait(timeout=wait_sec):
            with self._lock:
                self._state = "active"
            return True

        # Initial NOTIFY did not arrive (likely blocked by UFW); tear down cleanly.
        self._unsubscribe(sub, timeout=0.8)
        self._stop_server()
        with self._lock:
            self._state = "unavailable"
        return False

    def sync_subscriptions(
        self,
        groups: list[ZoneGroup],
        speakers: list[SpeakerInfo],
        topo_ip: str,
        pool: concurrent.futures.ThreadPoolExecutor,
    ) -> None:
        if not self.active or not groups:
            return

        desired: set[tuple[str, str, str, str]] = set()
        if topo_ip:
            desired.add(("topology", topo_ip, "/ZoneGroupTopology/Event", topo_ip))
        for g in groups:
            desired.add(
                ("avtransport", g.coord_ip, "/MediaRenderer/AVTransport/Event", g.coord_uuid)
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
        with self._lock:
            current_subs = list(self._subs.values())

        existing_by_key: dict[tuple[str, str, str, str], _GenaSub] = {}
        to_unsub: list[_GenaSub] = []
        to_renew: list[_GenaSub] = []
        for sub in current_subs:
            if sub.key not in desired or sub.key in existing_by_key:
                to_unsub.append(sub)
            else:
                existing_by_key[sub.key] = sub
                if sub.expires_at - now < 300.0:
                    to_renew.append(sub)

        to_sub = [k for k in desired if k not in existing_by_key]

        futs: list[concurrent.futures.Future] = []
        for sub in to_unsub:
            futs.append(pool.submit(self._unsubscribe, sub))
        for kind, ip, path, target_uuid in to_sub:
            futs.append(pool.submit(self._subscribe, ip, path, kind, target_uuid))

        renew_futs = [(sub, pool.submit(self._renew, sub)) for sub in to_renew]
        for fut in futs:
            try:
                fut.result()
            except Exception:
                pass
        for sub, fut in renew_futs:
            try:
                ok = fut.result()
            except Exception:
                ok = False
            if not ok:
                with self._lock:
                    self._subs.pop(sub.sid, None)
                self._subscribe(sub.ip, sub.path, sub.kind, sub.target_uuid)

        with self._lock:
            if not self._subs and self._state == "active":
                self._state = "unprobed"

    def shutdown(self) -> None:
        with self._lock:
            subs = list(self._subs.values())
            self._subs.clear()
            self._state = "unavailable"
        if subs:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(8, len(subs))
            ) as ex:
                list(ex.map(lambda s: self._unsubscribe(s, 0.5), subs))
        self._stop_server()


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
            self._bus_name: dbus.service.BusName | None = dbus.service.BusName(
                self.bus_name_str, bus=self._bus
            )
            super().__init__(self._bus, MPRIS_PATH)

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
                elapsed_us = max(0, int((time.monotonic() - self._state_at) * 1_000_000))
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

        @dbus.service.method(PLAYER_IFACE)
        def Play(self) -> None:
            self.state = replace(self.state, playback_status="Playing")
            self._state_at = time.monotonic()
            self._run_soap_async(
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "Play",
                "<InstanceID>0</InstanceID><Speed>1</Speed>",
            )

        @dbus.service.method(PLAYER_IFACE)
        def Pause(self) -> None:
            self.state = replace(self.state, playback_status="Paused")
            self._state_at = time.monotonic()
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
            self.state = replace(self.state, playback_status="Stopped")
            self._state_at = time.monotonic()
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
            self.state = replace(self.state, position_sec=sec)
            self._state_at = time.monotonic()
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
            self._event_lock = threading.Lock()
            self._event_clients: set[socket.socket] = set()
            self.players: dict[str, SonosMprisPlayer] = {}
            cached_ips, cached_rtts = load_cached_seeds()
            _RTT.seed(cached_rtts)
            self._seed_ips: list[str] = _RTT.rank(cached_ips)
            self._last_saved_seeds: tuple[str, ...] = tuple(self._seed_ips)
            self._groups: list[ZoneGroup] = []
            self._speakers: list[SpeakerInfo] = []
            self._group_states: dict[str, TrackState] = {}
            self._speaker_audio: dict[str, tuple[int, bool]] = {}
            self._active_uuids: set[str] = set()
            self._media_info_hints: set[str] = set()
            self._last_discovery = 0.0
            self._last_topology = 0.0
            self._last_full_sweep = 0.0
            self._last_settings_refresh = 0.0
            self._last_snapshot_at = 0.0
            self._snapshot_seq = 0
            self._explicit_wake = False
            self._library_cache: list[dict] = []
            self._library_cache_at = 0.0
            self._queue_cache: dict[str, tuple[int, float, list[dict]]] = {}
            self._pending_mpris_snapshot: dict[str, tuple[ZoneGroup, TrackState]] | None = None
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

        def _schedule_mpris_update(
            self, snapshot: dict[str, tuple[ZoneGroup, TrackState]]
        ) -> None:
            schedule = False
            with self.lock:
                self._pending_mpris_snapshot = snapshot
                if not self._mpris_flush_scheduled:
                    self._mpris_flush_scheduled = True
                    schedule = True
            if schedule:
                GLib.idle_add(self._flush_mpris_on_main)

        def _flush_mpris_on_main(self) -> bool:
            with self.lock:
                snapshot = self._pending_mpris_snapshot
                self._pending_mpris_snapshot = None
                self._mpris_flush_scheduled = False
            if snapshot is not None:
                self._apply_mpris_on_main(snapshot)
            return False

        def _prune_dead_event_clients_locked(self) -> list[socket.socket]:
            dead: list[socket.socket] = []
            for client in list(self._event_clients):
                try:
                    peek = client.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                    if peek == b"":
                        dead.append(client)
                except BlockingIOError:
                    continue
                except OSError:
                    dead.append(client)
            for client in dead:
                self._event_clients.discard(client)
            return dead

        def _add_event_client(self, conn: socket.socket) -> None:
            initial_line: bytes | None = None
            with self.lock:
                if self._groups and self._group_states:
                    payload = build_state_payload(
                        self._groups,
                        self._speakers,
                        self._group_states,
                        self._speaker_audio,
                        gena_active=self._gena.active,
                        cached=True,
                    )
                    initial_line = (json.dumps(payload) + "\n").encode("utf-8")
            with self._event_lock:
                dead = self._prune_dead_event_clients_locked()
                self._event_clients.add(conn)
            for stale in dead:
                try:
                    stale.close()
                except OSError:
                    pass
            if initial_line is not None:
                try:
                    conn.settimeout(0.5)
                    conn.sendall(initial_line)
                except OSError:
                    with self._event_lock:
                        self._event_clients.discard(conn)
                    try:
                        conn.close()
                    except OSError:
                        pass

        def _broadcast_state(self, payload: dict | None = None) -> None:
            with self._event_lock:
                if not self._event_clients:
                    return
                clients = list(self._event_clients)
            if payload is None:
                with self.lock:
                    if not self._groups or not self._group_states:
                        return
                    payload = build_state_payload(
                        self._groups,
                        self._speakers,
                        self._group_states,
                        self._speaker_audio,
                        gena_active=self._gena.active,
                        cached=False,
                    )
            line = (json.dumps(payload) + "\n").encode("utf-8")
            dead: list[socket.socket] = []
            for conn in clients:
                try:
                    conn.settimeout(0.5)
                    conn.sendall(line)
                except OSError:
                    dead.append(conn)
            if dead:
                with self._event_lock:
                    for conn in dead:
                        self._event_clients.discard(conn)
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
                    with self.lock:
                        groups = list(self._groups)
                        speakers = list(self._speakers)
                        topo_ip = self._seed_ips[0] if self._seed_ips else seed_ip
                    self._gena.sync_subscriptions(
                        groups, speakers, topo_ip, self._soap_pool
                    )
                    self._broadcast_state()

            try:
                self._action_pool.submit(_probe_worker)
            except RuntimeError:
                pass

        def _sync_position_for_group(self, group: ZoneGroup) -> None:
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
            mpris_snapshot: dict[str, tuple[ZoneGroup, TrackState]] = {}
            with self.lock:
                st = self._group_states.get(group.coord_uuid)
                if st is None:
                    return
                self._group_states[group.coord_uuid] = replace(
                    st,
                    position_sec=position_sec,
                    duration_sec=duration_sec or st.duration_sec,
                    track_num=track_num or st.track_num,
                )
                self._snapshot_seq += 1
                self._last_snapshot_at = time.monotonic()
                for g in self._groups:
                    gst = self._group_states.get(g.coord_uuid)
                    if gst and (gst.playback_status == "Playing" or gst.has_track):
                        mpris_snapshot[g.coord_uuid] = (g, gst)
            self._schedule_mpris_update(mpris_snapshot)
            self._broadcast_state()

        def _on_gena_event(self, sub: _GenaSub, body: str) -> None:
            if self.stop_event.is_set():
                return
            if sub.kind == "topology":
                parsed = parse_gena_topology_event(body)
                if not parsed:
                    return
                groups, speakers = parsed
                all_ips = [s.ip for s in speakers] + [g.coord_ip for g in groups]
                unique_ips = list(dict.fromkeys(all_ips))
                ranked = _RTT.rank(unique_ips)
                with self.lock:
                    self._groups = groups
                    self._speakers = speakers
                    self._seed_ips = ranked
                    self._last_topology = time.monotonic()
                    self._queue_cache.clear()
                    valid = {g.coord_uuid for g in groups}
                    self._group_states = {
                        k: v for k, v in self._group_states.items() if k in valid
                    }
                    self._snapshot_seq += 1
                    topo_ip = ranked[0] if ranked else sub.ip
                if self._gena.active:
                    try:
                        self._action_pool.submit(
                            self._gena.sync_subscriptions,
                            groups,
                            speakers,
                            topo_ip,
                            self._soap_pool,
                        )
                    except RuntimeError:
                        pass
                self._broadcast_state()
                return

            mpris_snapshot: dict[str, tuple[ZoneGroup, TrackState]] = {}
            sync_group: ZoneGroup | None = None
            changed = False

            if sub.kind == "avtransport":
                with self.lock:
                    prev = self._group_states.get(sub.target_uuid)
                    group = find_group(self._groups, sub.target_uuid)
                    sp_audio = self._speaker_audio.get(sub.target_uuid)
                updated, need_pos_sync, queue_changed = parse_gena_avtransport_event(
                    sub.ip, body, prev
                )
                if updated is not None:
                    if (
                        prev is None
                        and group is not None
                        and len(group.member_uuids) == 1
                        and sp_audio is not None
                    ):
                        updated = replace(
                            updated,
                            group_volume=sp_audio[0],
                            group_muted=sp_audio[1],
                        )
                    with self.lock:
                        self._group_states[sub.target_uuid] = updated
                        if queue_changed:
                            self._queue_cache.pop(sub.target_uuid, None)
                        if updated.playback_status == "Playing" or updated.has_track:
                            self._active_uuids.add(sub.target_uuid)
                        else:
                            self._active_uuids.discard(sub.target_uuid)
                        self._snapshot_seq += 1
                        self._last_snapshot_at = time.monotonic()
                        for g in self._groups:
                            gst = self._group_states.get(g.coord_uuid)
                            if gst and (gst.playback_status == "Playing" or gst.has_track):
                                mpris_snapshot[g.coord_uuid] = (g, gst)
                    changed = True
                    if need_pos_sync and group is not None:
                        sync_group = group

            elif sub.kind == "group_rc":
                vol, muted = parse_gena_group_rc_event(body)
                if vol is not None or muted is not None:
                    with self.lock:
                        prev = self._group_states.get(sub.target_uuid)
                        if prev is not None:
                            self._group_states[sub.target_uuid] = replace(
                                prev,
                                group_volume=vol if vol is not None else prev.group_volume,
                                group_muted=muted if muted is not None else prev.group_muted,
                            )
                            self._snapshot_seq += 1
                            self._last_snapshot_at = time.monotonic()
                            for g in self._groups:
                                gst = self._group_states.get(g.coord_uuid)
                                if gst and (gst.playback_status == "Playing" or gst.has_track):
                                    mpris_snapshot[g.coord_uuid] = (g, gst)
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
                        # Single-room coordinators reflect RenderingControl directly on group volume/mute.
                        g = find_group(self._groups, sub.target_uuid)
                        if g is not None and len(g.member_uuids) == 1:
                            prev = self._group_states.get(g.coord_uuid)
                            if prev is not None:
                                self._group_states[g.coord_uuid] = replace(
                                    prev, group_volume=new_vol, group_muted=new_muted
                                )
                        self._snapshot_seq += 1
                        self._last_snapshot_at = time.monotonic()
                        for grp in self._groups:
                            gst = self._group_states.get(grp.coord_uuid)
                            if gst and (gst.playback_status == "Playing" or gst.has_track):
                                mpris_snapshot[grp.coord_uuid] = (grp, gst)
                        changed = True

            if changed:
                self._schedule_mpris_update(mpris_snapshot)
                self._broadcast_state()
            if sync_group is not None:
                try:
                    self._soap_pool.submit(self._sync_position_for_group, sync_group)
                except RuntimeError:
                    pass

        def _ensure_topology(
            self, force: bool = False, interval: float = TOPOLOGY_ACTIVE_INTERVAL
        ) -> tuple[list[ZoneGroup], list[SpeakerInfo]]:
            with self._topo_lock:
                now = time.monotonic()
                with self.lock:
                    seed_ips = list(self._seed_ips)
                    has_groups = bool(self._groups)
                    last_discovery = self._last_discovery
                    last_topology = self._last_topology

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
                        seed_ips = _RTT.rank(found)
                    with self.lock:
                        self._seed_ips = list(seed_ips)
                        self._last_discovery = last_discovery

                if seed_ips and (
                    force or not has_groups or (now - last_topology) >= interval
                ):
                    ranked_seeds = _RTT.rank(seed_ips)
                    groups, speakers = fetch_topology(ranked_seeds)
                    if not groups and not discovered_now:
                        found = discover_sonos_ips()
                        last_discovery = time.monotonic()
                        with self.lock:
                            self._last_discovery = last_discovery
                        if found:
                            ranked_seeds = _RTT.rank(found)
                            groups, speakers = fetch_topology(ranked_seeds)

                    if groups:
                        all_ips = list(
                            dict.fromkeys(
                                [s.ip for s in speakers]
                                + [g.coord_ip for g in groups]
                                + ranked_seeds
                            )
                        )
                        new_seeds = _RTT.rank(all_ips)
                        save_now = False
                        with self.lock:
                            self._groups = groups
                            self._speakers = speakers
                            self._seed_ips = new_seeds
                            self._last_topology = time.monotonic()
                            seed_tuple = tuple(new_seeds)
                            if seed_tuple != self._last_saved_seeds:
                                self._last_saved_seeds = seed_tuple
                                save_now = True
                        if save_now:
                            save_cached_seeds(new_seeds, _RTT.snapshot())
                        self._maybe_start_gena(new_seeds[0])
                    else:
                        with self.lock:
                            self._groups = []
                            self._speakers = []
                            self._seed_ips = []

                with self.lock:
                    return list(self._groups), list(self._speakers)

        def _fetch_group_snapshot(
            self, group: ZoneGroup, include_speakers: bool = True
        ) -> dict:
            with self.lock:
                groups = list(self._groups)
                speakers = list(self._speakers)
                prev_states = dict(self._group_states)
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
                media_info_hints=hints,
                selected_coord_uuid=group.coord_uuid,
                refresh_all_settings=True,
            )

            mpris_snapshot: dict[str, tuple[ZoneGroup, TrackState]] = {}
            with self.lock:
                if failed_any:
                    self._last_topology = 0.0
                self._group_states.update(polled_states)
                if include_speakers:
                    self._speaker_audio.update(polled_audio)
                self._media_info_hints = hints
                for g in self._groups:
                    st = self._group_states.get(g.coord_uuid)
                    if st and (st.playback_status == "Playing" or st.has_track):
                        mpris_snapshot[g.coord_uuid] = (g, st)
                self._active_uuids = set(mpris_snapshot.keys())
                self._last_snapshot_at = time.monotonic()
                self._snapshot_seq += 1
                payload = build_state_payload(
                    self._groups,
                    self._speakers,
                    self._group_states,
                    self._speaker_audio,
                    gena_active=self._gena.active,
                    cached=False,
                )

            self._schedule_mpris_update(mpris_snapshot)
            self._broadcast_state(payload)
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
                prev_states = dict(self._group_states)
                prev_audio = dict(self._speaker_audio)
                hints = set(self._media_info_hints)
                refresh_settings = (
                    force_topology
                    or not prev_states
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
                media_info_hints=hints,
                selected_coord_uuid=selected_coord_uuid,
                refresh_all_settings=refresh_settings,
            )

            mpris_snapshot: dict[str, tuple[ZoneGroup, TrackState]] = {}
            for g in groups:
                st = group_states.get(g.coord_uuid)
                if st and (st.playback_status == "Playing" or st.has_track):
                    mpris_snapshot[g.coord_uuid] = (g, st)

            with self.lock:
                if failed_any:
                    self._last_topology = 0.0
                self._group_states = group_states
                if need_speakers:
                    self._speaker_audio = speaker_audio
                else:
                    # Keep cached per-member audio, updating single-room coordinators from group_states.
                    single_coords = {
                        g.coord_uuid for g in groups if len(g.member_uuids) == 1
                    }
                    merged_audio = dict(self._speaker_audio)
                    for s in speakers:
                        if (
                            s.is_coordinator
                            and s.coord_uuid in single_coords
                            and s.coord_uuid in group_states
                        ):
                            st = group_states[s.coord_uuid]
                            merged_audio[s.uuid] = (st.group_volume, st.group_muted)
                    self._speaker_audio = merged_audio
                self._media_info_hints = hints
                self._active_uuids = set(mpris_snapshot.keys())
                self._last_snapshot_at = time.monotonic()
                self._snapshot_seq += 1
                payload = build_state_payload(
                    groups,
                    speakers,
                    self._group_states,
                    self._speaker_audio,
                    gena_active=self._gena.active,
                    cached=False,
                )

            self._schedule_mpris_update(mpris_snapshot)
            self._broadcast_state(payload)
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
                    self.players[uuid].update(group, state)
                else:
                    try:
                        self.players[uuid] = SonosMprisPlayer(
                            group,
                            state,
                            on_action_cb=self.trigger_poll,
                            action_pool=self._action_pool,
                        )
                    except Exception:
                        pass
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

            if cmd == "get_state":
                force_topo = bool(req.get("force_topology", False))
                want_cached = bool(req.get("cached", False))
                include_speakers = bool(req.get("include_speakers", True))
                selected_coord = str(
                    req.get("selected_coord_uuid") or coord_uuid
                ).strip()
                if want_cached and not force_topo:
                    with self.lock:
                        if self._groups and self._group_states:
                            return build_state_payload(
                                self._groups,
                                self._speakers,
                                self._group_states,
                                self._speaker_audio,
                                gena_active=self._gena.active,
                                cached=True,
                            )
                if coord_uuid and not force_topo:
                    target_group = self._find_group(coord_uuid)
                    if target_group is not None:
                        return self._fetch_group_snapshot(
                            target_group, include_speakers=include_speakers
                        )
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
                now = time.monotonic()
                with self.lock:
                    if (
                        not force
                        and self._library_cache
                        and (now - self._library_cache_at) < LIBRARY_CACHE_TTL
                    ):
                        return {"ok": True, "items": list(self._library_cache)}
                    ranked_seeds = _RTT.rank(list(self._seed_ips))
                # Favorites & Playlists are household-wide; query the fastest speaker on the LAN.
                ip = ranked_seeds[0] if ranked_seeds else (group.coord_ip if group else "")
                if not ip:
                    groups, _ = self._ensure_topology(force=False)
                    ip = (
                        _RTT.rank([g.coord_ip for g in groups])[0]
                        if groups
                        else ""
                    )
                if not ip:
                    return {"ok": False, "error": "no Sonos speakers discovered"}
                fav_fut = self._soap_pool.submit(
                    browse_content_directory, ip, "FV:2", 100
                )
                pl_fut = self._soap_pool.submit(
                    browse_content_directory, ip, "SQ:", 100
                )
                items = fav_fut.result() + pl_fut.result()
                with self.lock:
                    self._library_cache = items
                    self._library_cache_at = time.monotonic()
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
                    group.coord_ip, "Q:0", limit=limit, minimal=True
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
                elif cmd == "previous":
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "Previous",
                        "<InstanceID>0</InstanceID>",
                    )
                elif cmd == "stop":
                    soap_call(
                        group.coord_ip,
                        "/MediaRenderer/AVTransport/Control",
                        "AVTransport",
                        "Stop",
                        "<InstanceID>0</InstanceID>",
                    )
                if cmd in ("play", "pause", "stop"):
                    new_status = (
                        "Playing"
                        if cmd == "play"
                        else ("Paused" if cmd == "pause" else "Stopped")
                    )
                    with self.lock:
                        st = self._group_states.get(group.coord_uuid)
                        if st is not None:
                            self._group_states[group.coord_uuid] = replace(
                                st, playback_status=new_status
                            )
                if not self._gena.active:
                    self.trigger_poll()
                return {"ok": True}

            if cmd == "seek":
                if not group:
                    return {"ok": False, "error": "no Sonos group"}
                sec = max(0, int(req.get("seconds", 0)))
                soap_call(
                    group.coord_ip,
                    "/MediaRenderer/AVTransport/Control",
                    "AVTransport",
                    "Seek",
                    f"<InstanceID>0</InstanceID><Unit>REL_TIME</Unit><Target>{format_sec_to_hms(sec)}</Target>",
                )
                with self.lock:
                    st = self._group_states.get(group.coord_uuid)
                    if st is not None:
                        self._group_states[group.coord_uuid] = replace(
                            st, position_sec=sec
                        )
                if not self._gena.active:
                    self.trigger_poll()
                return {"ok": True}

            if cmd == "set_play_mode":
                if not group:
                    return {"ok": False, "error": "no Sonos group"}
                mode = encode_play_mode(
                    bool(req.get("shuffle", False)), str(req.get("repeat", "none"))
                )
                soap_call(
                    group.coord_ip,
                    "/MediaRenderer/AVTransport/Control",
                    "AVTransport",
                    "SetPlayMode",
                    f"<InstanceID>0</InstanceID><NewPlayMode>{mode}</NewPlayMode>",
                )
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
                play_uri_on_coordinator(group, uri, metadata, as_next=as_next)
                with self.lock:
                    self._queue_cache.pop(group.coord_uuid, None)
                if not self._gena.active:
                    self.trigger_poll()
                return {"ok": True}

            if cmd == "play_queue_track":
                if not group:
                    return {"ok": False, "error": "no Sonos group"}
                track_num = max(1, int(req.get("track_num", 1)))
                soap_call(
                    group.coord_ip,
                    "/MediaRenderer/AVTransport/Control",
                    "AVTransport",
                    "SetAVTransportURI",
                    "<InstanceID>0</InstanceID>"
                    f"<CurrentURI>{xml_escape(f'x-rincon-queue:{group.coord_uuid}#0')}</CurrentURI>"
                    "<CurrentURIMetaData></CurrentURIMetaData>",
                )
                soap_call(
                    group.coord_ip,
                    "/MediaRenderer/AVTransport/Control",
                    "AVTransport",
                    "Seek",
                    f"<InstanceID>0</InstanceID><Unit>TRACK_NR</Unit><Target>{track_num}</Target>",
                )
                soap_call(
                    group.coord_ip,
                    "/MediaRenderer/AVTransport/Control",
                    "AVTransport",
                    "Play",
                    "<InstanceID>0</InstanceID><Speed>1</Speed>",
                )
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
                    resp = {"ok": False, "error": str(exc)}
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
            except Exception:
                pass
            finally:
                if not keep_open:
                    try:
                        conn.close()
                    except OSError:
                        pass

        def _poll_loop(self) -> None:
            while not self.stop_event.is_set():
                now = time.monotonic()
                with self.lock:
                    had_active = bool(self._active_uuids)
                    last_snapshot_at = self._last_snapshot_at
                    explicit_wake = self._explicit_wake
                    self._explicit_wake = False

                # When UPnP GENA push is active, renew/sync subscriptions on a quiet
                # heartbeat instead of polling speakers every 2 seconds.
                if self._gena.active and not explicit_wake:
                    with self.lock:
                        groups = list(self._groups)
                        speakers = list(self._speakers)
                        topo_ip = self._seed_ips[0] if self._seed_ips else ""
                    if groups:
                        self._gena.sync_subscriptions(
                            groups, speakers, topo_ip, self._soap_pool
                        )
                        if self._gena.active:
                            self.wake_event.wait(timeout=GENA_HEARTBEAT_INTERVAL)
                            self.wake_event.clear()
                            continue

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
                groups, speakers = self._ensure_topology(force=False, interval=topo_interval)

                if not groups:
                    self._schedule_mpris_update({})
                    self.wake_event.wait(
                        timeout=compute_poll_interval(False, False)
                    )
                    self.wake_event.clear()
                    continue

                with self.lock:
                    full_sweep = (now - self._last_full_sweep) >= IDLE_SWEEP_INTERVAL
                    if full_sweep:
                        self._last_full_sweep = now
                    active_uuids = set(self._active_uuids)
                    prev_states = dict(self._group_states)
                    hints = set(self._media_info_hints)
                    seq_before = self._snapshot_seq

                targets = (
                    groups
                    if (full_sweep or not active_uuids)
                    else [g for g in groups if g.coord_uuid in active_uuids]
                )

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

                mpris_snapshot: dict[str, tuple[ZoneGroup, TrackState]] = {}
                next_active: set[str] = set()
                any_playing = False
                valid_uuids = {g.coord_uuid for g in groups}

                with self.lock:
                    if failed_any:
                        self._last_topology = 0.0
                    self._media_info_hints = hints
                    # If _fetch_full_snapshot ran while we were polling, keep its fresher data.
                    if self._snapshot_seq == seq_before:
                        merged = {
                            k: v
                            for k, v in self._group_states.items()
                            if k in valid_uuids
                        }
                        merged.update(polled_states)
                        for g in groups:
                            st = merged.get(g.coord_uuid)
                            if not st:
                                continue
                            if st.playback_status == "Playing":
                                any_playing = True
                            if st.playback_status == "Playing" or st.has_track:
                                next_active.add(g.coord_uuid)
                                mpris_snapshot[g.coord_uuid] = (g, st)
                        self._group_states = merged
                        self._active_uuids = next_active
                        should_publish = True
                    else:
                        any_playing = any(
                            st.playback_status == "Playing"
                            for st in self._group_states.values()
                        )
                        should_publish = False

                if should_publish:
                    self._schedule_mpris_update(mpris_snapshot)
                    self._broadcast_state()

                if self._gena.active:
                    with self.lock:
                        topo_ip = self._seed_ips[0] if self._seed_ips else ""
                    self._gena.sync_subscriptions(
                        groups, speakers, topo_ip, self._soap_pool
                    )
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
                clients = list(self._event_clients)
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
    if len(sys.argv) >= 3 and sys.argv[1] == "--rpc":
        return send_rpc_client(sys.argv[2])
    return run_daemon()


if __name__ == "__main__":
    sys.exit(main())
