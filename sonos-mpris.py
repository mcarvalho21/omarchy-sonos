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
import html
import ipaddress
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

NS_DIDL = "{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}"
NS_DC = "{http://purl.org/dc/elements/1.1/}"
NS_UPNP = "{urn:schemas-upnp-org:metadata-1-0/upnp/}"
NS_R = "{urn:schemas-rinconnetworks-com:metadata-1-0/}"

MAX_HTTP_BYTES = 1024 * 1024
HTTP_TIMEOUT = 2.5
DISCOVERY_INTERVAL = 60.0
TOPOLOGY_ACTIVE_INTERVAL = 20.0
TOPOLOGY_IDLE_INTERVAL = 60.0
POLL_ACTIVE_INTERVAL = 2.0
POLL_IDLE_INTERVAL = 30.0
IDLE_SWEEP_INTERVAL = 30.0
LIBRARY_CACHE_TTL = 45.0


def runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def legacy_state_file_path() -> str:
    return os.path.join(runtime_dir(), "mc-sonos-state.json")


def socket_file_path() -> str:
    return os.path.join(runtime_dir(), "mc-sonos.sock")


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


def soap_call(ip: str, path: str, service: str, action: str, body: str = "") -> str:
    payload = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}:1">'
        f"{body}</u:{action}></s:Body></s:Envelope>"
    ).encode("utf-8")
    req = urllib.request.Request(
        f"http://{ip}:1400{path}",
        data=payload,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:schemas-upnp-org:service:{service}:1#{action}"',
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read(MAX_HTTP_BYTES).decode("utf-8", "replace")


def discover_via_avahi() -> list[str]:
    ips: list[str] = []
    seen: set[str] = set()
    try:
        proc = subprocess.run(
            ["avahi-browse", "-rtpk", "_sonos._tcp"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ips

    for line in proc.stdout.splitlines():
        if not line.startswith("=;"):
            continue
        fields = line.split(";")
        if len(fields) < 8:
            continue
        ip = fields[7].strip()
        if is_valid_private_ipv4(ip) and ip not in seen:
            seen.add(ip)
            ips.append(ip)
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
        sock.settimeout(2.2)
        sock.sendto(msg, ("239.255.255.250", 1900))
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            try:
                _, addr = sock.recvfrom(4096)
            except socket.timeout:
                break
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


_SERVICE_NAMES_CACHE: dict[str, str] = {}

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
    global _SERVICE_NAMES_CACHE
    if _SERVICE_NAMES_CACHE:
        return _SERVICE_NAMES_CACHE
    result = dict(KNOWN_SERVICE_NAMES)
    if ip:
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
        except Exception:
            pass
    _SERVICE_NAMES_CACHE = result
    return result


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


def fetch_speaker_audio(ip: str) -> tuple[int, bool]:
    vol = 0
    muted = False
    try:
        vresp = soap_call(
            ip,
            "/MediaRenderer/RenderingControl/Control",
            "RenderingControl",
            "GetVolume",
            "<InstanceID>0</InstanceID><Channel>Master</Channel>",
        )
        raw = ET.fromstring(vresp).findtext(".//CurrentVolume")
        if raw is not None:
            vol = max(0, min(100, int(raw)))
    except Exception:
        pass
    try:
        mresp = soap_call(
            ip,
            "/MediaRenderer/RenderingControl/Control",
            "RenderingControl",
            "GetMute",
            "<InstanceID>0</InstanceID><Channel>Master</Channel>",
        )
        raw = ET.fromstring(mresp).findtext(".//CurrentMute")
        if raw is not None:
            muted = raw.strip() in ("1", "true", "True")
    except Exception:
        pass
    return vol, muted


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
        tstate = (ET.fromstring(tresp).findtext(".//CurrentTransportState") or "").strip()
    except Exception:
        return None

    if tstate in ("PLAYING", "TRANSITIONING"):
        status = "Playing"
    elif tstate == "PAUSED_PLAYBACK":
        status = "Paused"
    else:
        status = "Stopped"

    if skip_stopped_details and status == "Stopped":
        return TrackState(
            playback_status=status,
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

    title = artist = album = art_url = ""
    track_uri = current_uri = ""
    duration_sec = position_sec = track_num = 0
    can_next = can_prev = True

    try:
        presp = soap_call(
            ip,
            "/MediaRenderer/AVTransport/Control",
            "AVTransport",
            "GetPositionInfo",
            "<InstanceID>0</InstanceID>",
        )
        proot = ET.fromstring(presp)
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

        if not title or not track_uri:
            mresp = soap_call(
                ip,
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "GetMediaInfo",
                "<InstanceID>0</InstanceID>",
            )
            mroot = ET.fromstring(mresp)
            current_uri = (mroot.findtext(".//CurrentURI") or "").strip()
            uri_meta = mroot.findtext(".//CurrentURIMetaData") or ""
            m_title, m_artist, m_album, m_art = parse_didl_metadata(ip, uri_meta)
            title = title or m_title
            artist = artist or m_artist
            album = album or m_album
            art_url = art_url or m_art

        if track_uri.startswith(("x-sonos-htastream:", "x-rincon-stream:")):
            can_next = False
            can_prev = False
            if not title:
                title = "TV Audio" if "htastream" in track_uri else "Line-In"
    except Exception:
        pass

    source_name, source_icon = detect_audio_source(ip, track_uri, current_uri)

    shuffle = False
    repeat = "none"
    if include_audio or status == "Playing":
        try:
            sresp = soap_call(
                ip,
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "GetTransportSettings",
                "<InstanceID>0</InstanceID>",
            )
            play_mode = ET.fromstring(sresp).findtext(".//PlayMode") or "NORMAL"
            shuffle, repeat = decode_play_mode(play_mode)
        except Exception:
            pass

    group_vol = 0
    group_muted = False
    if include_audio:
        if len(group.member_uuids) > 1:
            try:
                gv = soap_call(
                    ip,
                    "/MediaRenderer/GroupRenderingControl/Control",
                    "GroupRenderingControl",
                    "GetGroupVolume",
                    "<InstanceID>0</InstanceID>",
                )
                raw = ET.fromstring(gv).findtext(".//CurrentVolume")
                if raw is not None:
                    group_vol = max(0, min(100, int(raw)))
            except Exception:
                pass
            try:
                gm = soap_call(
                    ip,
                    "/MediaRenderer/GroupRenderingControl/Control",
                    "GroupRenderingControl",
                    "GetGroupMute",
                    "<InstanceID>0</InstanceID>",
                )
                raw = ET.fromstring(gm).findtext(".//CurrentMute")
                if raw is not None:
                    group_muted = raw.strip() in ("1", "true", "True")
            except Exception:
                pass
        else:
            group_vol, group_muted = fetch_speaker_audio(ip)

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


def browse_content_directory(ip: str, object_id: str, limit: int = 100) -> list[dict]:
    items: list[dict] = []
    start = 0
    batch = min(100, max(1, limit))
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
                art = resolve_art_url(ip, el.findtext(f"{NS_UPNP}albumArtURI") or "")
                uri = (el.findtext(f"{NS_DIDL}res") or "").strip()
                res_md = (el.findtext(f"{NS_R}resMD") or "").strip()
                desc = (el.findtext(f"{NS_R}description") or "").strip()
                upnp_class = (el.findtext(f"{NS_UPNP}class") or "").strip()
                dur_attr = ""
                res_el = el.find(f"{NS_DIDL}res")
                if res_el is not None:
                    dur_attr = res_el.attrib.get("duration", "")
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
                        "metadata": res_md[:4096],
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

    if as_next and not is_stream:
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


def run_daemon() -> int:
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    # Clean up any legacy state file from older versions; state is served on demand over RPC.
    try:
        legacy = legacy_state_file_path()
        if os.path.exists(legacy):
            os.unlink(legacy)
    except OSError:
        pass

    class SonosMprisPlayer(dbus.service.Object):
        def __init__(self, group: ZoneGroup, state: TrackState, on_action_cb) -> None:
            self.group = group
            self.state = state
            self._on_action_cb = on_action_cb
            self.bus_name_str = f"org.mpris.MediaPlayer2.sonos_{group.bus_suffix}"
            self._bus = dbus.SessionBus(private=True)
            self._bus_name = dbus.service.BusName(self.bus_name_str, bus=self._bus)
            super().__init__(self._bus, MPRIS_PATH)

        def close(self) -> None:
            try:
                self.remove_from_connection()
            except Exception:
                pass
            try:
                self._bus.close()
            except Exception:
                pass

        def _identity(self) -> str:
            return f"Sonos · {self.group.room_label}"

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
                "LoopStatus": dbus.String(
                    "Track"
                    if st.repeat == "one"
                    else ("Playlist" if st.repeat == "all" else "None")
                ),
                "Rate": dbus.Double(1.0),
                "Shuffle": dbus.Boolean(st.shuffle),
                "Metadata": self._metadata_dict(),
                "Volume": dbus.Double(st.group_volume / 100.0),
                "Position": dbus.Int64(st.position_us),
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

            threading.Thread(target=worker, daemon=True).start()

        @dbus.service.method(MPRIS_IFACE)
        def Raise(self) -> None:
            pass

        @dbus.service.method(MPRIS_IFACE)
        def Quit(self) -> None:
            pass

        @dbus.service.method(PLAYER_IFACE)
        def Play(self) -> None:
            self._run_soap_async(
                "/MediaRenderer/AVTransport/Control",
                "AVTransport",
                "Play",
                "<InstanceID>0</InstanceID><Speed>1</Speed>",
            )

        @dbus.service.method(PLAYER_IFACE)
        def Pause(self) -> None:
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
                if len(self.group.member_uuids) > 1:
                    self._run_soap_async(
                        "/MediaRenderer/GroupRenderingControl/Control",
                        "GroupRenderingControl",
                        "SetGroupVolume",
                        f"<InstanceID>0</InstanceID><DesiredVolume>{vol}</DesiredVolume>",
                    )
                else:
                    self._run_soap_async(
                        "/MediaRenderer/RenderingControl/Control",
                        "RenderingControl",
                        "SetVolume",
                        f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>{vol}</DesiredVolume>",
                    )

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
            self.players: dict[str, SonosMprisPlayer] = {}
            self._seed_ips: list[str] = []
            self._groups: list[ZoneGroup] = []
            self._speakers: list[SpeakerInfo] = []
            self._group_states: dict[str, TrackState] = {}
            self._speaker_audio: dict[str, tuple[int, bool]] = {}
            self._active_uuids: set[str] = set()
            self._last_discovery = 0.0
            self._last_topology = 0.0
            self._last_full_sweep = 0.0
            self._library_cache: list[dict] = []
            self._library_cache_at = 0.0
            self._rpc_sock: socket.socket | None = None

        def trigger_poll(self, force_topology: bool = False) -> None:
            if force_topology:
                self._last_topology = 0.0
            self._last_full_sweep = 0.0
            self.wake_event.set()

        def _ensure_topology(self, force: bool = False, interval: float = TOPOLOGY_ACTIVE_INTERVAL) -> tuple[list[ZoneGroup], list[SpeakerInfo]]:
            now = time.monotonic()
            if not self._seed_ips or (force and not self._groups) or (now - self._last_discovery) >= DISCOVERY_INTERVAL:
                if force or not self._seed_ips or (now - self._last_discovery) >= DISCOVERY_INTERVAL:
                    found = discover_sonos_ips()
                    if found:
                        self._seed_ips = found
                    self._last_discovery = now

            if self._seed_ips and (force or not self._groups or (now - self._last_topology) >= interval):
                groups, speakers = fetch_topology(self._seed_ips)
                if groups:
                    with self.lock:
                        self._groups = groups
                        self._speakers = speakers
                    self._seed_ips = [g.coord_ip for g in groups] + [
                        ip
                        for ip in self._seed_ips
                        if ip not in {g.coord_ip for g in groups}
                    ]
                    self._last_topology = now
                else:
                    with self.lock:
                        self._groups = []
                        self._speakers = []
                    self._seed_ips = []

            with self.lock:
                return list(self._groups), list(self._speakers)

        def _fetch_full_snapshot(self, force_topology: bool = False) -> dict:
            groups, speakers = self._ensure_topology(
                force=force_topology, interval=10.0
            )
            group_states: dict[str, TrackState] = {}
            speaker_audio: dict[str, tuple[int, bool]] = {}
            mpris_snapshot: dict[str, tuple[ZoneGroup, TrackState]] = {}

            if groups:
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(groups) + len(speakers)))) as ex:
                    g_futures = {
                        ex.submit(fetch_group_state, g, True, False): g for g in groups
                    }
                    single_coords = {
                        g.coord_uuid for g in groups if len(g.member_uuids) == 1
                    }
                    s_futures = {
                        ex.submit(fetch_speaker_audio, s.ip): s
                        for s in speakers
                        if not (s.is_coordinator and s.coord_uuid in single_coords)
                    }

                    for fut, g in g_futures.items():
                        try:
                            st = fut.result()
                        except Exception:
                            st = None
                        if st is None:
                            self._last_topology = 0.0
                            continue
                        group_states[g.coord_uuid] = st
                        if st.playback_status == "Playing" or st.has_track:
                            mpris_snapshot[g.coord_uuid] = (g, st)

                    for s in speakers:
                        if s.is_coordinator and s.coord_uuid in single_coords and s.coord_uuid in group_states:
                            st = group_states[s.coord_uuid]
                            speaker_audio[s.uuid] = (st.group_volume, st.group_muted)

                    for fut, s in s_futures.items():
                        try:
                            speaker_audio[s.uuid] = fut.result()
                        except Exception:
                            speaker_audio[s.uuid] = (0, False)

            with self.lock:
                self._group_states = group_states
                self._speaker_audio = speaker_audio
                self._active_uuids = {
                    uuid
                    for uuid, st in group_states.items()
                    if st.playback_status == "Playing" or st.has_track
                }

            GLib.idle_add(self._apply_mpris_on_main, mpris_snapshot)
            return build_state_payload(groups, speakers, group_states, speaker_audio)

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
                            group, state, on_action_cb=self.trigger_poll
                        )
                    except Exception:
                        pass
            return False

        def _find_group(self, coord_uuid: str) -> ZoneGroup | None:
            with self.lock:
                for g in self._groups:
                    if g.coord_uuid == coord_uuid:
                        return g
                return self._groups[0] if self._groups else None

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
                return self._fetch_full_snapshot(
                    force_topology=bool(req.get("force_topology", False))
                )

            group = self._find_group(coord_uuid)

            if cmd == "refresh":
                self.trigger_poll(force_topology=True)
                return self._fetch_full_snapshot(force_topology=True)

            if cmd == "browse_library":
                force = bool(req.get("force", False))
                now = time.monotonic()
                if (
                    not force
                    and self._library_cache
                    and (now - self._library_cache_at) < LIBRARY_CACHE_TTL
                ):
                    return {"ok": True, "items": self._library_cache}
                ip = group.coord_ip if group else (self._seed_ips[0] if self._seed_ips else "")
                if not ip:
                    return {"ok": False, "error": "no Sonos speakers discovered"}
                favs = browse_content_directory(ip, "FV:2", limit=100)
                playlists = browse_content_directory(ip, "SQ:", limit=100)
                items = favs + playlists
                self._library_cache = items
                self._library_cache_at = now
                return {"ok": True, "items": items}

            if cmd == "browse_queue":
                if not group:
                    return {"ok": False, "error": "no Sonos group"}
                limit = max(1, min(300, int(req.get("limit", 150))))
                items = browse_content_directory(group.coord_ip, "Q:0", limit=limit)
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
                self.trigger_poll(force_topology=True)
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
                self.trigger_poll(force_topology=True)
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
                    threading.Thread(
                        target=self._serve_client, args=(conn,), daemon=True
                    ).start()
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
            try:
                conn.settimeout(4.0)
                data = b""
                while b"\n" not in data and len(data) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                req = json.loads(data.decode("utf-8", "replace").strip() or "{}")
                try:
                    resp = self._handle_rpc(req)
                except Exception as exc:
                    resp = {"ok": False, "error": str(exc)}
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

        def _poll_loop(self) -> None:
            while not self.stop_event.is_set():
                now = time.monotonic()
                with self.lock:
                    had_active = bool(self._active_uuids)
                topo_interval = (
                    TOPOLOGY_ACTIVE_INTERVAL if had_active else TOPOLOGY_IDLE_INTERVAL
                )
                groups, _ = self._ensure_topology(force=False, interval=topo_interval)

                if not groups:
                    GLib.idle_add(self._apply_mpris_on_main, {})
                    self.wake_event.wait(
                        timeout=compute_poll_interval(False, False)
                    )
                    self.wake_event.clear()
                    continue

                full_sweep = (now - self._last_full_sweep) >= IDLE_SWEEP_INTERVAL
                if full_sweep:
                    self._last_full_sweep = now

                with self.lock:
                    active_uuids = set(self._active_uuids)
                    group_states = dict(self._group_states)

                targets = (
                    groups
                    if (full_sweep or not active_uuids)
                    else [g for g in groups if g.coord_uuid in active_uuids]
                )

                # Closed-panel loop: poll only target coordinators (skipping per-speaker
                # volumes and skipping stopped-speaker metadata) and write zero state files.
                for group in targets:
                    if self.stop_event.is_set():
                        break
                    is_known_active = group.coord_uuid in active_uuids
                    st = fetch_group_state(
                        group,
                        include_audio=is_known_active,
                        skip_stopped_details=not is_known_active,
                    )
                    if st is None:
                        self._last_topology = 0.0
                        continue
                    group_states[group.coord_uuid] = st

                mpris_snapshot: dict[str, tuple[ZoneGroup, TrackState]] = {}
                next_active: set[str] = set()
                any_playing = False
                valid_uuids = {g.coord_uuid for g in groups}

                for g in groups:
                    st = group_states.get(g.coord_uuid)
                    if not st:
                        continue
                    if st.playback_status == "Playing":
                        any_playing = True
                    if st.playback_status == "Playing" or st.has_track:
                        next_active.add(g.coord_uuid)
                        mpris_snapshot[g.coord_uuid] = (g, st)

                group_states = {
                    k: v for k, v in group_states.items() if k in valid_uuids
                }

                with self.lock:
                    self._group_states = group_states
                    self._active_uuids = next_active

                GLib.idle_add(self._apply_mpris_on_main, mpris_snapshot)

                interval = compute_poll_interval(any_playing, bool(groups))
                self.wake_event.wait(timeout=interval)
                self.wake_event.clear()

        def start(self) -> None:
            threading.Thread(target=self._rpc_server_loop, daemon=True).start()
            threading.Thread(target=self._poll_loop, daemon=True).start()

        def shutdown(self) -> None:
            self.stop_event.set()
            self.wake_event.set()
            if self._rpc_sock is not None:
                try:
                    self._rpc_sock.close()
                except OSError:
                    pass
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
    return 0


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--rpc":
        return send_rpc_client(sys.argv[2])
    return run_daemon()


if __name__ == "__main__":
    sys.exit(main())
