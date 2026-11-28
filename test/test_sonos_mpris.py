#!/usr/bin/env python3
"""Unit tests for mc.sonos sonos-mpris.py parsing, source detection, play modes, and safety."""

from __future__ import annotations

import concurrent.futures
import http.client
import importlib.util
import io
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
MODULE_PATH = PLUGIN_DIR / "sonos-mpris.py"

spec = importlib.util.spec_from_file_location("sonos_mpris", MODULE_PATH)
assert spec and spec.loader
sonos = importlib.util.module_from_spec(spec)
sys.modules["sonos_mpris"] = sonos
spec.loader.exec_module(sonos)


class TestDidlParsing(unittest.TestCase):
    def test_standard_track_metadata_and_relative_art(self) -> None:
        meta = (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            '<item id="-1" parentID="-1" restricted="true">'
            "<dc:title>Blue in Green</dc:title>"
            "<dc:creator>Miles Davis</dc:creator>"
            "<upnp:album>Kind of Blue</upnp:album>"
            "<upnp:albumArtURI>/getaa?s=1&amp;u=x-sonos-spotify%3aabc</upnp:albumArtURI>"
            "</item></DIDL-Lite>"
        )
        title, artist, album, art = sonos.parse_didl_metadata("192.168.4.62", meta)
        self.assertEqual(title, "Blue in Green")
        self.assertEqual(artist, "Miles Davis")
        self.assertEqual(album, "Kind of Blue")
        self.assertEqual(
            art, "http://192.168.4.62:1400/getaa?s=1&u=x-sonos-spotify%3aabc"
        )

    def test_stream_content_splits_artist_and_title_for_radio(self) -> None:
        meta = (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            "<item><dc:title>stream.mp3</dc:title>"
            "<r:streamContent>Boards of Canada - Roygbiv</r:streamContent>"
            "</item></DIDL-Lite>"
        )
        title, artist, album, art = sonos.parse_didl_metadata("192.168.4.62", meta)
        self.assertEqual(artist, "Boards of Canada")
        self.assertEqual(title, "Roygbiv")
        self.assertEqual(album, "")
        self.assertEqual(art, "")

    def test_markup_shaped_metadata_preserved_as_plain_text_and_unsafe_art_dropped(self) -> None:
        # Malicious or markup-like titles/artists must pass through verbatim (never interpreted as XML tags
        # once unescaped) while Panel.qml enforces Text.PlainText, and non-http(s) art URLs must be rejected.
        meta = (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            "<item>"
            '<dc:title>&lt;img src="http://127.0.0.1:9999/exfil"&gt; Track</dc:title>'
            "<dc:creator>&lt;b&gt;Artist&lt;/b&gt; &amp; Friends</dc:creator>"
            "<upnp:album>&lt;a href=&quot;javascript:alert(1)&quot;&gt;Album&lt;/a&gt;</upnp:album>"
            "<upnp:albumArtURI>file:///etc/passwd</upnp:albumArtURI>"
            "</item></DIDL-Lite>"
        )
        title, artist, album, art = sonos.parse_didl_metadata("192.168.4.62", meta)
        self.assertEqual(title, '<img src="http://127.0.0.1:9999/exfil"> Track')
        self.assertEqual(artist, "<b>Artist</b> & Friends")
        self.assertEqual(album, '<a href="javascript:alert(1)">Album</a>')
        self.assertEqual(art, "")
        self.assertEqual(sonos.resolve_art_url("192.168.4.62", "javascript:alert(1)"), "")
        self.assertEqual(
            sonos.resolve_art_url("192.168.4.62", "https://i.scdn.co/image/cover.jpg"),
            "https://i.scdn.co/image/cover.jpg",
        )

    def test_malformed_or_empty_didl_returns_empty_tuple(self) -> None:
        self.assertEqual(sonos.parse_didl_metadata("192.168.4.62", ""), ("", "", "", ""))
        self.assertEqual(
            sonos.parse_didl_metadata("192.168.4.62", "NOT_IMPLEMENTED"), ("", "", "", "")
        )
        self.assertEqual(
            sonos.parse_didl_metadata("192.168.4.62", "<broken><xml"), ("", "", "", "")
        )


class TestAudioSourceDetection(unittest.TestCase):
    def test_spotify_connect_vli(self) -> None:
        name, icon = sonos.detect_audio_source(
            "192.168.4.62",
            "x-sonos-vli:RINCON_123456:2,spotify:1234",
            "",
            services={},
        )
        self.assertEqual(name, "Spotify Connect")
        self.assertEqual(icon, sonos.source_icon_for("spotify"))

    def test_airplay_tv_linein_library(self) -> None:
        self.assertEqual(
            sonos.detect_audio_source("192.168.4.62", "x-sonos-vli:RINCON_1:2,airplay:1", "", services={})[0],
            "AirPlay",
        )
        self.assertEqual(
            sonos.detect_audio_source("192.168.4.62", "x-sonos-htastream:RINCON_1:spdif", "", services={})[0],
            "TV / HDMI",
        )
        self.assertEqual(
            sonos.detect_audio_source("192.168.4.62", "x-rincon-stream:RINCON_1", "", services={})[0],
            "Line-In",
        )
        self.assertEqual(
            sonos.detect_audio_source("192.168.4.62", "x-file-cifs://nas/music/song.flac", "", services={})[0],
            "Music Library",
        )

    def test_music_service_sid_lookup(self) -> None:
        catalog = {"284": "YouTube Music", "204": "Apple Music", "160": "SoundCloud"}
        self.assertEqual(
            sonos.detect_audio_source(
                "192.168.4.62",
                "x-sonos-http:track%3a123.mp4?sid=284&flags=8224",
                "",
                services=catalog,
            )[0],
            "YouTube Music",
        )
        self.assertEqual(
            sonos.detect_audio_source(
                "192.168.4.62",
                "x-sonos-http:song%3a99.mp4?sid=204",
                "",
                services=catalog,
            )[0],
            "Apple Music",
        )
        self.assertEqual(
            sonos.detect_audio_source(
                "192.168.4.62",
                "x-sonos-http:item%3a5?sid=999",
                "",
                services=catalog,
            )[0],
            "Service #999",
        )

    def test_empty_uris_return_empty_source(self) -> None:
        self.assertEqual(sonos.detect_audio_source("192.168.4.62", "", "", services={}), ("", ""))


class TestPlayModesAndHelpers(unittest.TestCase):
    def test_play_mode_roundtrip(self) -> None:
        cases = [
            ("NORMAL", (False, "none")),
            ("REPEAT_ALL", (False, "all")),
            ("REPEAT_ONE", (False, "one")),
            ("SHUFFLE_NOREPEAT", (True, "none")),
            ("SHUFFLE", (True, "all")),
            ("SHUFFLE_REPEAT_ONE", (True, "one")),
        ]
        for mode, (shuffle, repeat) in cases:
            self.assertEqual(sonos.decode_play_mode(mode), (shuffle, repeat))
            self.assertEqual(sonos.encode_play_mode(shuffle, repeat), mode)

    def test_hms_parsing_and_formatting(self) -> None:
        self.assertEqual(sonos.parse_hms_to_sec("0:03:45"), 225)
        self.assertEqual(sonos.parse_hms_to_sec("1:02:03.9"), 3723)
        self.assertEqual(sonos.parse_hms_to_sec("NOT_IMPLEMENTED"), 0)
        self.assertEqual(sonos.parse_hms_to_sec("-1:00:00"), 0)
        self.assertEqual(sonos.parse_hms_to_sec("0:00:inf"), 0)
        self.assertEqual(sonos.parse_hms_to_sec("0:00:nan"), 0)
        self.assertEqual(sonos.parse_hms_to_sec("0:00:1e309"), 0)
        self.assertEqual(
            sonos.parse_hms_to_sec("9" * 400 + ":00:00"), sonos.MAX_DURATION_SEC
        )
        huge = sonos.parse_hms_to_sec("10000000000000:00:00")
        self.assertEqual(huge, sonos.MAX_DURATION_SEC)
        self.assertLess(huge * 1_000_000, (1 << 63) - 1)
        self.assertEqual(sonos.format_sec_to_hms(225), "0:03:45")
        self.assertEqual(sonos.format_sec_to_hms(3723), "1:02:03")
        self.assertEqual(sonos.format_sec_to_hms(-10), "0:00:00")

    def test_poll_interval_idle_backoff(self) -> None:
        self.assertEqual(sonos.compute_poll_interval(True, True), sonos.POLL_ACTIVE_INTERVAL)
        self.assertEqual(sonos.compute_poll_interval(False, True), 30.0)
        self.assertEqual(sonos.compute_poll_interval(False, False), 30.0)
        self.assertEqual(sonos.compute_poll_interval(True, False), 30.0)

    def test_topology_parsing_filters_invisible_and_public_ips(self) -> None:
        xml = (
            "<ZoneGroupState><ZoneGroups>"
            '<ZoneGroup Coordinator="RINCON_A" ID="RINCON_A:1">'
            '<ZoneGroupMember UUID="RINCON_A" ZoneName="Kitchen" Location="http://192.168.4.62:1400/xml/device_description.xml"/>'
            '<ZoneGroupMember UUID="RINCON_B" ZoneName="Porch" Location="http://192.168.4.72:1400/xml/device_description.xml"/>'
            '<ZoneGroupMember UUID="RINCON_SUB" ZoneName="Kitchen Sub" Invisible="1" Location="http://192.168.4.99:1400/xml/device_description.xml"/>'
            '<ZoneGroupMember UUID="RINCON_BAD" ZoneName="Evil" Location="http://8.8.8.8:1400/xml/device_description.xml"/>'
            "</ZoneGroup>"
            "</ZoneGroups></ZoneGroupState>"
        )
        groups, speakers = sonos.parse_zone_group_state(xml)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].room_label, "Kitchen (+1)")
        self.assertEqual(groups[0].member_names, ("Kitchen", "Porch"))
        self.assertEqual([s.name for s in speakers], ["Kitchen", "Porch"])

    def test_topology_parsing_places_coordinator_first(self) -> None:
        xml = (
            "<ZoneGroupState><ZoneGroups>"
            '<ZoneGroup Coordinator="RINCON_B" ID="RINCON_B:1">'
            '<ZoneGroupMember UUID="RINCON_A" ZoneName="Kitchen" Location="http://192.168.4.62:1400/xml/device_description.xml"/>'
            '<ZoneGroupMember UUID="RINCON_B" ZoneName="Porch" Location="http://192.168.4.72:1400/xml/device_description.xml"/>'
            "</ZoneGroup>"
            "</ZoneGroups></ZoneGroupState>"
        )
        groups, _ = sonos.parse_zone_group_state(xml)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].member_uuids, ("RINCON_B", "RINCON_A"))
        self.assertEqual(groups[0].member_names, ("Porch", "Kitchen"))

    def test_topology_parsing_caps_counts_deduplicates_truncates_and_drops_orphans(self) -> None:
        long_uuid = "RINCON_" + ("X" * 200)
        long_name = "Room " + ("N" * 200)
        # Group with invalid/public coordinator must not leak orphan member speakers.
        orphan_zg = (
            '<ZoneGroup Coordinator="RINCON_ORPHAN">'
            '<ZoneGroupMember UUID="RINCON_ORPHAN" ZoneName="BadCoord" Location="http://8.8.8.8:1400/xml"/>'
            '<ZoneGroupMember UUID="RINCON_LEAK" ZoneName="OrphanMember" Location="http://192.168.4.55:1400/xml"/>'
            "</ZoneGroup>"
        )
        # Duplicate coordinator + duplicate member + oversized UUID/name.
        dup_zg = (
            f'<ZoneGroup Coordinator="{long_uuid}">'
            f'<ZoneGroupMember UUID="{long_uuid}" ZoneName="{long_name}" Location="http://192.168.4.10:1400/xml"/>'
            f'<ZoneGroupMember UUID="{long_uuid}" ZoneName="DupMember" Location="http://192.168.4.10:1400/xml"/>'
            "</ZoneGroup>"
            f'<ZoneGroup Coordinator="{long_uuid}">'
            f'<ZoneGroupMember UUID="{long_uuid}" ZoneName="DupGroup" Location="http://192.168.4.11:1400/xml"/>'
            "</ZoneGroup>"
        )
        # Flood with 120 groups -> must cap at MAX_ZONE_GROUPS / MAX_SPEAKERS.
        flood = "".join(
            f'<ZoneGroup Coordinator="RINCON_F{i}">'
            f'<ZoneGroupMember UUID="RINCON_F{i}" ZoneName="F{i}" Location="http://192.168.4.{(i % 200) + 20}:1400/xml"/>'
            "</ZoneGroup>"
            for i in range(120)
        )
        xml = f"<ZoneGroupState><ZoneGroups>{orphan_zg}{dup_zg}{flood}</ZoneGroups></ZoneGroupState>"
        groups, speakers = sonos.parse_zone_group_state(xml)
        self.assertEqual(len(groups), sonos.MAX_ZONE_GROUPS)
        self.assertEqual(len(speakers), sonos.MAX_SPEAKERS)
        self.assertNotIn("RINCON_LEAK", {s.uuid for s in speakers})
        self.assertEqual(len({g.coord_uuid for g in groups}), len(groups))
        self.assertEqual(len({s.uuid for s in speakers}), len(speakers))
        trunc_sp = next(s for s in speakers if s.uuid.startswith("RINCON_XXXX"))
        self.assertEqual(len(trunc_sp.uuid), 128)
        self.assertEqual(len(trunc_sp.name), 120)


class TestHttpPoolAndSoapBatching(unittest.TestCase):
    def test_http_pool_reuses_connection_and_retries_stale_socket_once(self) -> None:
        created: list[object] = []

        class FakeResponse:
            def __init__(self, status: int, body: bytes, will_close: bool = False) -> None:
                self.status = status
                self.reason = "OK" if status == 200 else "Error"
                self.headers = {}
                self.will_close = will_close
                self._body = body
                self._closed = False

            def read(self, _amt: int = -1) -> bytes:
                self._closed = True
                return self._body

            def isclosed(self) -> bool:
                return self._closed

        class FakeConn:
            def __init__(self, host: str, port: int, timeout: float = 0) -> None:
                self.host = host
                self.port = port
                self.calls = 0
                self.closed = False
                created.append(self)

            def request(self, _method: str, _path: str, body: bytes = b"", headers=None) -> None:
                self.calls += 1
                # Simulate a stale keep-alive drop on the 3rd request on the first connection.
                if len(created) == 1 and self.calls == 3:
                    raise http.client.RemoteDisconnected("closed")

            def getresponse(self) -> FakeResponse:
                return FakeResponse(200, b"<ok/>")

            def close(self) -> None:
                self.closed = True

        pool = sonos._HttpPool(max_per_ip=4)
        with mock.patch.object(http.client, "HTTPConnection", FakeConn):
            self.assertEqual(pool.post("192.168.1.10", "/a", {}, b"1"), "<ok/>")
            self.assertEqual(pool.post("192.168.1.10", "/b", {}, b"2"), "<ok/>")
            self.assertEqual(len(created), 1)
            # 3rd call hits RemoteDisconnected on reused conn -> retries once on fresh conn.
            self.assertEqual(pool.post("192.168.1.10", "/c", {}, b"3"), "<ok/>")
            self.assertEqual(len(created), 2)
            self.assertTrue(created[0].closed)
        pool.close_all()

    def test_http_pool_raises_and_closes_on_http_error_without_retry(self) -> None:
        class Resp:
            def __init__(self, status: int) -> None:
                self.status = status
                self.reason = "OK" if status == 200 else "Internal Server Error"
                self.will_close = False

            def read(self, _amt: int = -1) -> bytes:
                return b"ok" if self.status == 200 else b"fault"

            def isclosed(self) -> bool:
                return True

        conns = []
        statuses = iter([200, 500])

        class Conn:
            def __init__(self, *_args, **_kwargs) -> None:
                self.closed = False
                self.calls = 0
                conns.append(self)

            def request(self, *_args, **_kwargs) -> None:
                self.calls += 1

            def getresponse(self) -> Resp:
                return Resp(next(statuses))

            def close(self) -> None:
                self.closed = True

        pool = sonos._HttpPool()
        with mock.patch.object(http.client, "HTTPConnection", Conn):
            self.assertEqual(pool.post("192.168.1.10", "/ok", {}, b"x"), "ok")
            # Second call reuses the pooled connection and gets HTTP 500: must close and NOT retry.
            with self.assertRaises(http.client.HTTPException):
                pool.post("192.168.1.10", "/err", {}, b"x")
        self.assertEqual(len(conns), 1)
        self.assertEqual(conns[0].calls, 2)
        self.assertTrue(conns[0].closed)

    def test_http_pool_does_not_retry_timeout_on_reused_socket(self) -> None:
        conns = []

        class OkResp:
            status = 200
            reason = "OK"
            will_close = False

            def read(self, _amt: int = -1) -> bytes:
                return b"ok"

            def isclosed(self) -> bool:
                return True

        class TimeoutConn:
            def __init__(self, *_args, **_kwargs) -> None:
                self.calls = 0
                self.closed = False
                conns.append(self)

            def request(self, *_args, **_kwargs) -> None:
                self.calls += 1
                if self.calls == 2:
                    raise TimeoutError("timed out")

            def getresponse(self) -> OkResp:
                return OkResp()

            def close(self) -> None:
                self.closed = True

        pool = sonos._HttpPool()
        with mock.patch.object(http.client, "HTTPConnection", TimeoutConn):
            self.assertEqual(pool.post("192.168.1.10", "/a", {}, b"1"), "ok")
            with self.assertRaises(TimeoutError):
                pool.post("192.168.1.10", "/b", {}, b"2")
        self.assertEqual(len(conns), 1)
        self.assertEqual(conns[0].calls, 2)
        self.assertTrue(conns[0].closed)

    def test_private_ipv4_and_xml_and_url_hardening(self) -> None:
        for ok_ip in ("192.168.4.62", "10.0.0.5", "172.16.1.9", "169.254.1.2"):
            self.assertTrue(sonos.is_valid_private_ipv4(ok_ip), ok_ip)
        for bad_ip in ("127.0.0.1", "0.0.0.0", "255.255.255.255", "240.0.0.1", "192.0.2.1", "8.8.8.8", "::1"):
            self.assertFalse(sonos.is_valid_private_ipv4(bad_ip), bad_ip)

        dtd_xml = '<!DOCTYPE x [<!ENTITY a "boom">]><r>&a;</r>'
        self.assertEqual(sonos.parse_didl_metadata("192.168.4.62", dtd_xml), ("", "", "", ""))
        self.assertEqual(sonos.resolve_art_url("192.168.4.62", "http://[invalid"), "")
        for bad_art in (
            "http://127.0.0.1:8080/secret",
            "http://127.1:8080/secret",
            "http://2130706433:8080/secret",
            "http://0x7f000001:8080/secret",
            "http://0177.0.0.1:8080/secret",
            "http://0:8080/secret",
            "http://[::ffff:127.0.0.1]:8080/secret",
            "http://127.0.0.1\\@example.com/secret",
            "http://user:pass@example.com/secret",
            "http://example.com/art\r\nX-Injected: 1",
            "//evil.com/art.jpg",
            "http://localhost/secret",
            "http://foo.localhost/secret",
            "http://[::1]:8080/secret",
            "http://0.0.0.0/secret",
            "http://0.0.0.1:8080/secret",
            "http://[::ffff:0.0.0.1]:8080/secret",
            "http://127\u30020\u30020\u30021:8080/secret",
            "http://\uff11\uff12\uff17.\uff10.\uff10.\uff11:8080/secret",
            "http://\u2460\u2461\u2466.0.0.1:8080/secret",
            "http://0x7f\u30021:8080/secret",
            "http://\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54:8080/secret",
            "http://localhost\u3002:8080/secret",
            "http://\u1d38ocalhost:8080/secret",
            "http://\u2097ocalhost:8080/secret",
            "http://\U0001fbf127.0.0.1:8080/secret",
            "http://\U0001fbf0:8080/secret",
            "http://127\u2064.0.0.1:8080/secret",
            "http://127\U000e0100.0.0.1:8080/secret",
            "http://\uff3b::1\uff3d/secret",
            "http://example.com:99999/secret",
            "http://example.com:0/secret",
            "http://",
        ):
            self.assertEqual(sonos.resolve_art_url("192.168.4.62", bad_art), "", bad_art)
        self.assertEqual(
            sonos.resolve_art_url("192.168.4.62", "https://i.scdn.co/image/caf\u00e9.jpg"),
            "https://i.scdn.co/image/caf\u00e9.jpg",
        )
        self.assertEqual(
            sonos.resolve_art_url("192.168.4.62", "https://i.scdn.co/image/abc123"),
            "https://i.scdn.co/image/abc123",
        )
        self.assertEqual(
            sonos.resolve_art_url("192.168.4.62", "/getaa?s=1&u=foo"),
            "http://192.168.4.62:1400/getaa?s=1&u=foo",
        )
        self.assertEqual(sonos.resolve_art_url("127.0.0.1", "/getaa?u=1"), "")
        bad_topo = (
            '<ZoneGroupState><ZoneGroups><ZoneGroup Coordinator="RINCON_A">'
            '<ZoneGroupMember UUID="RINCON_A" ZoneName="Bad" Location="http://[invalid"/>'
            '<ZoneGroupMember UUID="RINCON_B" ZoneName="Loop" Location="http://127.0.0.1:1400/xml"/>'
            "</ZoneGroup></ZoneGroups></ZoneGroupState>"
        )
        self.assertEqual(sonos.parse_zone_group_state(bad_topo), ([], []))

    def test_browse_content_directory_breaks_on_empty_children_response(self) -> None:
        calls = {"n": 0}

        def evil_soap(*_args, **_kwargs) -> str:
            calls["n"] += 1
            return "<r><NumberReturned>1</NumberReturned><TotalMatches>99999</TotalMatches><Result>&lt;DIDL-Lite/&gt;</Result></r>"

        with mock.patch.object(sonos, "soap_call", side_effect=evil_soap):
            items = sonos.browse_content_directory("192.168.4.62", "Q:0", limit=50)
        self.assertEqual(items, [])
        self.assertEqual(calls["n"], 1)

        # Oversized single XML page with >limit children must be strictly capped to limit.
        many_items = "".join(f'<item id="Q:0/{i}"><dc:title>T{i}</dc:title></item>' for i in range(25))
        many_didl = (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            f"{many_items}</DIDL-Lite>"
        )
        many_resp = (
            f"<r><NumberReturned>1</NumberReturned><TotalMatches>1</TotalMatches>"
            f"<Result>{sonos.xml_escape(many_didl)}</Result></r>"
        )
        with mock.patch.object(sonos, "soap_call", return_value=many_resp):
            capped = sonos.browse_content_directory("192.168.4.62", "Q:0", limit=5, minimal=True)
        self.assertEqual(len(capped), 5)

        # Slow-drip 1-item-per-page speaker must stop at MAX_BROWSE_PAGES.
        drip_calls = {"n": 0}
        one_didl = (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            '<item id="Q:0/1"><dc:title>One</dc:title></item></DIDL-Lite>'
        )
        one_resp = (
            f"<r><NumberReturned>1</NumberReturned><TotalMatches>99999</TotalMatches>"
            f"<Result>{sonos.xml_escape(one_didl)}</Result></r>"
        )

        def drip_soap(*_args, **_kwargs) -> str:
            drip_calls["n"] += 1
            return one_resp

        with mock.patch.object(sonos, "soap_call", side_effect=drip_soap):
            dripped = sonos.browse_content_directory("192.168.4.62", "Q:0", limit=200, minimal=True)
        self.assertEqual(drip_calls["n"], sonos.MAX_BROWSE_PAGES)
        self.assertEqual(len(dripped), sonos.MAX_BROWSE_PAGES)

        with mock.patch.object(sonos, "soap_call", side_effect=OSError("timeout")):
            self.assertEqual(sonos.browse_content_directory("192.168.4.62", "Q:0", limit=50), [])
            with self.assertRaises(OSError):
                sonos.browse_content_directory(
                    "192.168.4.62", "Q:0", limit=50, raise_on_error=True
                )

    def test_play_uri_on_coordinator_does_not_wipe_queue_for_linein_tv_or_vli(self) -> None:
        group = sonos.ZoneGroup("RINCON_1", "192.168.4.10", "Kitchen", ("RINCON_1",), ("Kitchen",))
        for uri in (
            "x-rincon-stream:RINCON_1",
            "x-sonos-htastream:RINCON_1:spdif",
            "x-sonos-vli:RINCON_1:2,spotify:1",
            "x-sonosapi-hls:station1",
        ):
            actions: list[str] = []
            with mock.patch.object(
                sonos,
                "soap_call",
                side_effect=lambda _ip, _p, _s, act, _b="": actions.append(act) or "<ok/>",
            ):
                sonos.play_uri_on_coordinator(group, uri, "", as_next=False)
            self.assertEqual(actions, ["SetAVTransportURI", "Play"], uri)
            with self.assertRaises(ValueError):
                sonos.play_uri_on_coordinator(group, uri, "", as_next=True)

    def test_fetch_groups_batch_preserves_stopped_room_volume_on_closed_sweep(self) -> None:
        g_stop = sonos.ZoneGroup("RINCON_2", "192.168.4.11", "Porch", ("RINCON_2",), ("Porch",))
        prev_st = sonos.replace(
            sonos._stopped_track_state(),
            group_volume=37,
            group_muted=True,
            shuffle=True,
            repeat="all",
        )
        with (
            mock.patch.object(
                sonos,
                "soap_call",
                return_value="<r><CurrentTransportState>STOPPED</CurrentTransportState></r>",
            ),
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex,
        ):
            states, _, _ = sonos.fetch_groups_batch(
                [g_stop],
                [],
                ex,
                include_speakers=False,
                known_active_uuids=set(),
                prev_states={"RINCON_2": prev_st},
            )
        self.assertEqual(states["RINCON_2"].playback_status, "Stopped")
        self.assertEqual(states["RINCON_2"].group_volume, 37)
        self.assertTrue(states["RINCON_2"].group_muted)
        self.assertTrue(states["RINCON_2"].shuffle)
        self.assertEqual(states["RINCON_2"].repeat, "all")

    def test_fetch_groups_batch_preserves_volume_on_transient_none_response(self) -> None:
        g = sonos.ZoneGroup("RINCON_1", "192.168.4.10", "Kitchen", ("RINCON_1", "RINCON_2"), ("Kitchen", "Den"))
        s1 = sonos.SpeakerInfo("RINCON_1", "192.168.4.10", "Kitchen", "RINCON_1", True)
        s2 = sonos.SpeakerInfo("RINCON_2", "192.168.4.11", "Den", "RINCON_1", False)
        prev_st = sonos.replace(sonos._stopped_track_state(), group_volume=55, group_muted=True)

        def flaky_soap(_ip: str, _path: str, _svc: str, action: str, _body: str = "") -> str:
            if action == "GetTransportInfo":
                return "<r><CurrentTransportState>PAUSED_PLAYBACK</CurrentTransportState></r>"
            if action == "GetPositionInfo":
                return "<r><TrackURI>x-sonos-http:t1</TrackURI><TrackMetaData>&lt;DIDL-Lite xmlns:dc=&quot;http://purl.org/dc/elements/1.1/&quot;&gt;&lt;item&gt;&lt;dc:title&gt;Song&lt;/dc:title&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;</TrackMetaData></r>"
            if action in ("GetGroupVolume", "GetGroupMute", "GetVolume", "GetMute"):
                raise OSError("transient timeout")
            return "<r/>"

        with (
            mock.patch.object(sonos, "soap_call", side_effect=flaky_soap),
            concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex,
        ):
            states, audio, _ = sonos.fetch_groups_batch(
                [g],
                [s1, s2],
                ex,
                include_speakers=True,
                prev_states={"RINCON_1": prev_st},
                prev_speaker_audio={"RINCON_1": (50, False), "RINCON_2": (60, True)},
            )
        self.assertEqual(states["RINCON_1"].group_volume, 55)
        self.assertTrue(states["RINCON_1"].group_muted)
        self.assertEqual(audio["RINCON_1"], (50, False))
        self.assertEqual(audio["RINCON_2"], (60, True))

    def test_fetch_groups_batch_fetches_audio_for_newly_playing_room(self) -> None:
        g_play = sonos.ZoneGroup(
            coord_uuid="RINCON_1",
            coord_ip="192.168.4.10",
            coord_name="Kitchen",
            member_uuids=("RINCON_1",),
            member_names=("Kitchen",),
        )
        g_stop = sonos.ZoneGroup(
            coord_uuid="RINCON_2",
            coord_ip="192.168.4.11",
            coord_name="Office",
            member_uuids=("RINCON_2",),
            member_names=("Office",),
        )
        calls: list[tuple[str, str]] = []

        def fake_soap(ip: str, _path: str, _service: str, action: str, _body: str = "") -> str:
            calls.append((ip, action))
            if action == "GetTransportInfo":
                state = "PLAYING" if ip == "192.168.4.10" else "STOPPED"
                return f"<r><CurrentTransportState>{state}</CurrentTransportState></r>"
            if action == "GetPositionInfo":
                return (
                    "<r><TrackURI>x-sonos-vli:RINCON_1:2,spotify:1</TrackURI>"
                    "<TrackMetaData>&lt;DIDL-Lite xmlns:dc=&quot;http://purl.org/dc/elements/1.1/&quot;&gt;"
                    "&lt;item&gt;&lt;dc:title&gt;Song&lt;/dc:title&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;</TrackMetaData>"
                    "<TrackDuration>0:03:00</TrackDuration><RelTime>0:00:30</RelTime><Track>1</Track></r>"
                )
            if action == "GetTransportSettings":
                return "<r><PlayMode>SHUFFLE</PlayMode></r>"
            if action == "GetVolume":
                return "<r><CurrentVolume>42</CurrentVolume></r>"
            if action == "GetMute":
                return "<r><CurrentMute>0</CurrentMute></r>"
            return "<r/>"

        with (
            mock.patch.object(sonos, "soap_call", side_effect=fake_soap),
            concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex,
        ):
            states, _, failed = sonos.fetch_groups_batch(
                [g_play, g_stop],
                [],
                ex,
                include_speakers=False,
                known_active_uuids=set(),
            )

        self.assertFalse(failed)
        self.assertEqual(states["RINCON_1"].playback_status, "Playing")
        self.assertEqual(states["RINCON_1"].group_volume, 42)
        self.assertTrue(states["RINCON_1"].shuffle)
        self.assertEqual(states["RINCON_1"].repeat, "all")
        self.assertEqual(states["RINCON_2"].playback_status, "Stopped")
        # Stopped idle room must only make GetTransportInfo during closed-panel sweep.
        office_actions = [act for ip, act in calls if ip == "192.168.4.11"]
        self.assertEqual(office_actions, ["GetTransportInfo"])

        # Transient GetPositionInfo failure must skip group & flag failed_any (never wipe track).
        def failing_pos_soap(_ip: str, _path: str, _service: str, action: str, _body: str = "") -> str:
            if action == "GetTransportInfo":
                return "<r><CurrentTransportState>PAUSED_PLAYBACK</CurrentTransportState></r>"
            if action == "GetPositionInfo":
                raise OSError("transient timeout")
            return "<r/>"

        with (
            mock.patch.object(sonos, "soap_call", side_effect=failing_pos_soap),
            concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex,
        ):
            pos_fail_states, _, pos_failed = sonos.fetch_groups_batch(
                [g_play], [], ex, include_speakers=False, known_active_uuids=None
            )
        self.assertTrue(pos_failed)
        self.assertEqual(pos_fail_states, {})

    def test_fetch_music_services_does_not_cache_transient_failure(self) -> None:
        orig_cache = dict(sonos._SERVICE_NAMES_CACHE)
        orig_fetched = sonos._SERVICE_NAMES_FETCHED
        orig_attempt = sonos._SERVICE_NAMES_LAST_ATTEMPT
        try:
            sonos._SERVICE_NAMES_CACHE = {}
            sonos._SERVICE_NAMES_FETCHED = False
            sonos._SERVICE_NAMES_LAST_ATTEMPT = 0.0

            with mock.patch.object(sonos, "soap_call", side_effect=OSError("timeout")):
                fallback = sonos.fetch_music_services("192.168.4.10")
            self.assertEqual(fallback["9"], "Spotify")
            self.assertFalse(sonos._SERVICE_NAMES_FETCHED)

            # Simulate retry window elapsed and speaker now answering.
            sonos._SERVICE_NAMES_LAST_ATTEMPT = 0.0
            xml = (
                "<r><AvailableServiceDescriptorList>"
                "&lt;Services&gt;&lt;Service Id=&quot;777&quot; Name=&quot;Qobuz&quot;/&gt;&lt;/Services&gt;"
                "</AvailableServiceDescriptorList></r>"
            )
            with mock.patch.object(sonos, "soap_call", return_value=xml) as mocked:
                loaded = sonos.fetch_music_services("192.168.4.10")
                cached = sonos.fetch_music_services("192.168.4.10")
            self.assertEqual(loaded["777"], "Qobuz")
            self.assertEqual(cached["777"], "Qobuz")
            self.assertEqual(mocked.call_count, 1)
        finally:
            sonos._SERVICE_NAMES_CACHE = orig_cache
            sonos._SERVICE_NAMES_FETCHED = orig_fetched
            sonos._SERVICE_NAMES_LAST_ATTEMPT = orig_attempt

    def test_browse_content_directory_minimal_and_oversized_metadata_guard(self) -> None:
        huge_md = "<DIDL-Lite>" + ("a" * (sonos.MAX_DIDL_METADATA_BYTES + 100)) + "</DIDL-Lite>"
        didl = (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            '<item id="Q:0/1">'
            "<dc:title>Track 1</dc:title>"
            "<dc:creator>Artist 1</dc:creator>"
            "<upnp:album>Album 1</upnp:album>"
            '<res duration="0:04:10">x-sonos-http:track1</res>'
            f"<r:resMD>{sonos.xml_escape(huge_md)}</r:resMD>"
            "</item></DIDL-Lite>"
        )
        resp = (
            f"<r><NumberReturned>1</NumberReturned><TotalMatches>1</TotalMatches>"
            f"<Result>{sonos.xml_escape(didl)}</Result></r>"
        )
        with mock.patch.object(sonos, "soap_call", return_value=resp):
            minimal = sonos.browse_content_directory("192.168.4.10", "Q:0", limit=10, minimal=True)
            full = sonos.browse_content_directory("192.168.4.10", "FV:2", limit=10, minimal=False)

        self.assertEqual(len(minimal), 1)
        self.assertEqual(minimal[0]["title"], "Track 1")
        self.assertEqual(minimal[0]["duration_sec"], 250)
        self.assertNotIn("metadata", minimal[0])
        self.assertNotIn("uri", minimal[0])

        self.assertEqual(len(full), 1)
        self.assertEqual(full[0]["metadata"], "")
        self.assertEqual(full[0]["uri"], "x-sonos-http:track1")

    def test_play_uri_rejects_live_stream_as_next(self) -> None:
        group = sonos.ZoneGroup(
            coord_uuid="RINCON_1",
            coord_ip="192.168.4.10",
            coord_name="Kitchen",
            member_uuids=("RINCON_1",),
            member_names=("Kitchen",),
        )
        with mock.patch.object(sonos, "soap_call") as mocked:
            with self.assertRaises(ValueError):
                sonos.play_uri_on_coordinator(
                    group, "x-sonosapi-stream:s12345?sid=254", "", as_next=True
                )
        mocked.assert_not_called()

    def test_discover_via_ssdp_filters_non_sonos_packets(self) -> None:
        responses = iter(
            [
                (b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\n\r\n", ("192.168.4.1", 1900)),
                (
                    b"HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n\r\n",
                    ("192.168.4.62", 1900),
                ),
            ]
        )

        class FakeSock:
            def setsockopt(self, *_args) -> None:
                pass

            def sendto(self, *_args) -> None:
                pass

            def settimeout(self, t: float) -> None:
                assert t > 0

            def recvfrom(self, _n: int):
                try:
                    return next(responses)
                except StopIteration:
                    raise socket.timeout()

            def close(self) -> None:
                pass

        with mock.patch.object(socket, "socket", return_value=FakeSock()):
            ips = sonos.discover_via_ssdp()
        self.assertEqual(ips, ["192.168.4.62"])

    def test_find_group_does_not_fallback_for_unknown_explicit_uuid(self) -> None:
        g1 = sonos.ZoneGroup("RINCON_1", "192.168.4.10", "Kitchen", ("RINCON_1",), ("Kitchen",))
        g2 = sonos.ZoneGroup("RINCON_2", "192.168.4.11", "Office", ("RINCON_2",), ("Office",))
        self.assertEqual(sonos.find_group([g1, g2], ""), g1)
        self.assertEqual(sonos.find_group([g1, g2], "RINCON_2"), g2)
        self.assertIsNone(sonos.find_group([g1, g2], "RINCON_GONE"))
        self.assertIsNone(sonos.find_group([], ""))

    def test_seed_cache_persists_valid_private_ips_and_rtts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_file = os.path.join(tmp, "seeds.json")
            sonos.save_cached_seeds(
                ["192.168.4.62", "8.8.8.8", "192.168.4.71", "192.168.4.62"],
                {"192.168.4.62": 92.4, "8.8.8.8": 10.0, "192.168.4.71": float("nan")},
                path=cache_file,
            )
            mode = os.stat(cache_file).st_mode & 0o777
            self.assertEqual(mode, 0o600)
            ips, rtts = sonos.load_cached_seeds(path=cache_file)
            self.assertEqual(ips, ["192.168.4.62", "192.168.4.71"])
            self.assertEqual(rtts, {"192.168.4.62": 92.4})

    def test_rtt_tracker_ranks_fastest_and_penalizes_failures(self) -> None:
        rtt = sonos._RttTracker(alpha=0.5)
        rtt.seed({"192.168.7.19": 390.0, "192.168.4.62": 95.0})
        self.assertEqual(
            rtt.rank(["192.168.7.19", "192.168.4.71", "192.168.4.62"]),
            ["192.168.4.62", "192.168.7.19", "192.168.4.71"],
        )
        rtt.record_failure("192.168.4.62")
        self.assertEqual(
            rtt.rank(["192.168.4.62", "192.168.7.19"]),
            ["192.168.7.19", "192.168.4.62"],
        )

    def test_rtt_tracker_hysteresis_keeps_preferred_until_much_faster_or_failed(self) -> None:
        rtt = sonos._RttTracker(alpha=1.0)
        rtt.record("192.168.4.62", 60.0)
        rtt.record("192.168.4.71", 52.0)
        # 52ms is not meaningfully faster than 60ms -> stay pinned to keep art URLs warm.
        self.assertEqual(
            rtt.pick_preferred(["192.168.4.71", "192.168.4.62"], current="192.168.4.62"),
            "192.168.4.62",
        )
        self.assertEqual(
            rtt.rank_with_preferred(["192.168.4.71", "192.168.4.62"], current="192.168.4.62")[0],
            "192.168.4.62",
        )
        # Significantly faster (>25% faster and >=15ms delta) switches cleanly.
        rtt.record("192.168.4.71", 35.0)
        self.assertEqual(
            rtt.pick_preferred(["192.168.4.71", "192.168.4.62"], current="192.168.4.62"),
            "192.168.4.71",
        )
        # Failure on current immediately demotes it.
        rtt.record_failure("192.168.4.71")
        self.assertEqual(
            rtt.pick_preferred(["192.168.4.71", "192.168.4.62"], current="192.168.4.71"),
            "192.168.4.62",
        )

    def test_extrapolate_position_and_build_state_payload(self) -> None:
        g1 = sonos.ZoneGroup("RINCON_1", "192.168.4.10", "Kitchen", ("RINCON_1",), ("Kitchen",))
        s1 = sonos.SpeakerInfo("RINCON_1", "192.168.4.10", "Kitchen", "RINCON_1", True)
        s2 = sonos.SpeakerInfo("RINCON_2", "192.168.4.11", "Office", "RINCON_2", True)
        st_play = sonos.TrackState(
            playback_status="Playing",
            title="Track",
            artist="Artist",
            album="Album",
            art_url="",
            source_name="Spotify",
            source_icon="",
            duration_sec=100,
            position_sec=20,
            track_num=1,
            shuffle=False,
            repeat="none",
            group_volume=30,
            group_muted=False,
            can_next=True,
            can_prev=True,
            position_at=1000.0,
            current_uri="x-rincon-queue:RINCON_1#0",
        )
        self.assertEqual(sonos.extrapolate_position_sec(st_play, now=1025.8), 45)
        self.assertEqual(sonos.extrapolate_position_sec(st_play, now=1200.0), 100)
        st_paused = sonos.replace(st_play, playback_status="Paused")
        self.assertEqual(sonos.extrapolate_position_sec(st_paused, now=1050.0), 20)

        payload_partial = sonos.build_state_payload(
            [g1], [s1, s2], {"RINCON_1": st_play}, {"RINCON_1": (30, False)},
            gena_active=True, cached=True, now=1012.0,
        )
        self.assertEqual(payload_partial["groups"][0]["position_sec"], 32)
        self.assertFalse(payload_partial["speakers_ready"])

        payload_ready = sonos.build_state_payload(
            [g1], [s1], {"RINCON_1": st_play}, {"RINCON_1": (30, False)},
            gena_active=True, cached=True, now=1012.0,
        )
        self.assertTrue(payload_ready["speakers_ready"])

    def test_play_uri_on_coordinator_skips_redundant_set_uri_and_retries_on_stale(self) -> None:
        group = sonos.ZoneGroup("RINCON_1", "192.168.4.10", "Kitchen", ("RINCON_1",), ("Kitchen",))
        queue_uri = "x-rincon-queue:RINCON_1#0"
        actions: list[str] = []

        def fake_soap(_ip: str, _path: str, _svc: str, action: str, _body: str = "") -> str:
            actions.append(action)
            return "<ok/>"

        with mock.patch.object(sonos, "soap_call", side_effect=fake_soap):
            res_uri = sonos.play_uri_on_coordinator(
                group, "x-rincon-playlist:RINCON_1#S:1", "", current_uri=queue_uri
            )
        self.assertEqual(res_uri, queue_uri)
        self.assertEqual(actions, ["RemoveAllTracksFromQueue", "AddURIToQueue", "Play"])

        # Stale current_uri: first Play fails, must fallback to SetAVTransportURI + Play.
        actions.clear()
        play_attempts = {"n": 0}

        def stale_soap(_ip: str, _path: str, _svc: str, action: str, _body: str = "") -> str:
            actions.append(action)
            if action == "Play":
                play_attempts["n"] += 1
                if play_attempts["n"] == 1:
                    raise http.client.HTTPException("500 UPnP 701")
            return "<ok/>"

        with mock.patch.object(sonos, "soap_call", side_effect=stale_soap):
            sonos.play_uri_on_coordinator(
                group, "x-rincon-playlist:RINCON_1#S:1", "", current_uri=queue_uri
            )
        self.assertEqual(
            actions,
            ["RemoveAllTracksFromQueue", "AddURIToQueue", "Play", "SetAVTransportURI", "Play"],
        )

        # Already playing live stream skips both SetAVTransportURI and Play.
        actions.clear()
        stream_uri = "x-sonosapi-stream:s12345?sid=254"
        with mock.patch.object(sonos, "soap_call", side_effect=fake_soap):
            sonos.play_uri_on_coordinator(
                group, stream_uri, "", current_uri=stream_uri, already_playing=True
            )
        self.assertEqual(actions, [])

        # Paused live stream skips SetAVTransportURI and only calls Play.
        with mock.patch.object(sonos, "soap_call", side_effect=fake_soap):
            sonos.play_uri_on_coordinator(
                group, stream_uri, "", current_uri=stream_uri, already_playing=False
            )
        self.assertEqual(actions, ["Play"])

    def test_debug_logging_opt_in(self) -> None:
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(sys, "stderr", buf):
            os.environ.pop("MC_SONOS_DEBUG", None)
            sonos._debug_log("silent")
        self.assertEqual(buf.getvalue(), "")

        buf2 = io.StringIO()
        with mock.patch.dict(os.environ, {"MC_SONOS_DEBUG": "1"}), mock.patch.object(sys, "stderr", buf2):
            sonos._debug_log("hello", ValueError("boom"))
        self.assertIn("hello: ValueError: boom", buf2.getvalue())

    def test_discover_via_avahi_streams_and_exits_early(self) -> None:
        r_fd, w_fd = os.pipe()
        os.write(
            w_fd,
            b"+;wlan0;IPv4;Sonos;_sonos._tcp;local\n"
            b"=;wlan0;IPv4;Sonos;_sonos._tcp;local;sonos.local;192.168.4.62;1400;\n",
        )
        # Leave write end open to verify discover_via_avahi exits via first_hit_grace without EOF.
        r_file = os.fdopen(r_fd, "rb", buffering=0)

        class FakeProc:
            def __init__(self) -> None:
                self.stdout = r_file
                self.terminated = False

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout=None) -> int:
                return 0

            def kill(self) -> None:
                pass

        fake_proc = FakeProc()
        try:
            with mock.patch.object(subprocess, "Popen", return_value=fake_proc):
                ips = sonos.discover_via_avahi(timeout=2.0, first_hit_grace=0.03)
            self.assertEqual(ips, ["192.168.4.62"])
            self.assertTrue(fake_proc.terminated)
        finally:
            r_file.close()
            os.close(w_fd)

    def test_adaptive_batch_speculates_media_info_and_skips_idle_and_settings(self) -> None:
        g_vli = sonos.ZoneGroup("RINCON_1", "192.168.4.10", "Kitchen", ("RINCON_1",), ("Kitchen",))
        g_idle = sonos.ZoneGroup("RINCON_2", "192.168.4.11", "Porch", ("RINCON_2",), ("Porch",))
        calls: list[tuple[str, str]] = []

        def fake_soap(ip: str, _path: str, _service: str, action: str, _body: str = "") -> str:
            calls.append((ip, action))
            if action == "GetTransportInfo":
                return (
                    "<r><CurrentTransportState>PAUSED_PLAYBACK</CurrentTransportState></r>"
                    if ip == "192.168.4.10"
                    else "<r><CurrentTransportState>STOPPED</CurrentTransportState></r>"
                )
            if action == "GetPositionInfo":
                if ip == "192.168.4.10":
                    return "<r><TrackURI>x-sonos-vli:RINCON_1:2,spotify:1</TrackURI><TrackMetaData></TrackMetaData></r>"
                return "<r><TrackURI></TrackURI><TrackMetaData></TrackMetaData></r>"
            if action == "GetMediaInfo":
                if ip == "192.168.4.10":
                    return (
                        "<r><CurrentURI>x-sonos-vli:RINCON_1:2,spotify:1</CurrentURI>"
                        "<CurrentURIMetaData>&lt;DIDL-Lite xmlns:dc=&quot;http://purl.org/dc/elements/1.1/&quot;&gt;"
                        "&lt;item&gt;&lt;dc:title&gt;Spotify&lt;/dc:title&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;</CurrentURIMetaData></r>"
                    )
                return "<r><CurrentURI></CurrentURI><CurrentURIMetaData></CurrentURIMetaData></r>"
            if action == "GetTransportSettings":
                return "<r><PlayMode>REPEAT_ALL</PlayMode></r>"
            if action == "GetVolume":
                return "<r><CurrentVolume>20</CurrentVolume></r>"
            if action == "GetMute":
                return "<r><CurrentMute>0</CurrentMute></r>"
            return "<r/>"

        hints: set[str] = set()
        with (
            mock.patch.object(sonos, "soap_call", side_effect=fake_soap),
            concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex,
        ):
            states1, _, _ = sonos.fetch_groups_batch(
                [g_vli, g_idle], [], ex, include_speakers=False, media_info_hints=hints
            )
            self.assertEqual(states1["RINCON_1"].title, "Spotify")
            self.assertIn("RINCON_1", hints)
            self.assertEqual(states1["RINCON_2"].repeat, "all")

            # Second poll with prev_states + hints: Porch skips GetMediaInfo AND GetTransportSettings.
            calls.clear()
            states2, _, _ = sonos.fetch_groups_batch(
                [g_vli, g_idle],
                [],
                ex,
                include_speakers=False,
                prev_states=states1,
                media_info_hints=hints,
                selected_coord_uuid="RINCON_1",
                refresh_all_settings=False,
            )

        self.assertEqual(states2["RINCON_1"].title, "Spotify")
        self.assertEqual(states2["RINCON_2"].repeat, "all")
        porch_actions = {act for ip, act in calls if ip == "192.168.4.11"}
        self.assertNotIn("GetMediaInfo", porch_actions)
        self.assertNotIn("GetTransportSettings", porch_actions)

    def test_browse_content_directory_requests_200_in_single_batch(self) -> None:
        bodies: list[str] = []

        def fake_soap(_ip: str, _path: str, _service: str, _action: str, body: str = "") -> str:
            bodies.append(body)
            return "<r><NumberReturned>0</NumberReturned><TotalMatches>0</TotalMatches><Result></Result></r>"

        with mock.patch.object(sonos, "soap_call", side_effect=fake_soap):
            sonos.browse_content_directory("192.168.4.62", "Q:0", limit=200, minimal=True)
        self.assertEqual(len(bodies), 1)
        self.assertIn("<RequestedCount>200</RequestedCount>", bodies[0])


class TestGenaEventsAndAutoDetect(unittest.TestCase):
    def test_parse_gena_topology_group_rc_and_rc_events(self) -> None:
        topo_xml = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0"><e:property>'
            "<ZoneGroupState>"
            + sonos.xml_escape(
                '<ZoneGroupState><ZoneGroups>'
                '<ZoneGroup Coordinator="RINCON_A" ID="RINCON_A:1">'
                '<ZoneGroupMember UUID="RINCON_A" ZoneName="Office" Location="http://192.168.4.71:1400/xml/device_description.xml"/>'
                "</ZoneGroup></ZoneGroups></ZoneGroupState>"
            )
            + "</ZoneGroupState></e:property></e:propertyset>"
        )
        parsed = sonos.parse_gena_topology_event(topo_xml)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual([g.coord_name for g in parsed[0]], ["Office"])

        grc_xml = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            "<e:property><GroupVolume>37</GroupVolume></e:property>"
            "<e:property><GroupMute>1</GroupMute></e:property>"
            "</e:propertyset>"
        )
        self.assertEqual(sonos.parse_gena_group_rc_event(grc_xml), (37, True))

        rc_last_change = (
            '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/RCS/">'
            '<InstanceID val="0">'
            '<Volume channel="LF" val="99"/>'
            '<Volume channel="Master" val="28"/>'
            '<Mute channel="LF" val="1"/>'
            '<Mute channel="Master" val="0"/>'
            "</InstanceID></Event>"
        )
        rc_xml = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f"<e:property><LastChange>{sonos.xml_escape(rc_last_change)}</LastChange></e:property>"
            "</e:propertyset>"
        )
        self.assertEqual(sonos.parse_gena_rc_event(rc_xml), (28, False))

    def test_parse_gena_avtransport_full_and_partial_last_change(self) -> None:
        didl = (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            "<item><dc:title>Silver Pearl</dc:title><dc:creator>Artist X</dc:creator>"
            "<upnp:album>Album Y</upnp:album></item></DIDL-Lite>"
        )
        full_lc = (
            '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
            '<InstanceID val="0">'
            '<TransportState val="PLAYING"/>'
            '<CurrentPlayMode val="SHUFFLE"/>'
            '<CurrentTrack val="3"/>'
            '<CurrentTrackDuration val="0:04:20"/>'
            '<CurrentTrackURI val="x-sonos-vli:RINCON_1:2,spotify:1"/>'
            f'<CurrentTrackMetaData val="{sonos.xml_escape(didl)}"/>'
            '<NumberOfTracks val="12"/>'
            "</InstanceID></Event>"
        )
        full_xml = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f"<e:property><LastChange>{sonos.xml_escape(full_lc)}</LastChange></e:property>"
            "</e:propertyset>"
        )
        st, need_pos, q_changed = sonos.parse_gena_avtransport_event(
            "192.168.4.62", full_xml, None
        )
        self.assertIsNotNone(st)
        assert st is not None
        self.assertEqual(st.playback_status, "Playing")
        self.assertEqual(st.title, "Silver Pearl")
        self.assertEqual(st.artist, "Artist X")
        self.assertEqual(st.source_name, "Spotify Connect")
        self.assertEqual(st.duration_sec, 260)
        self.assertEqual(st.track_num, 3)
        self.assertTrue(st.shuffle)
        self.assertEqual(st.repeat, "all")
        self.assertTrue(need_pos)
        self.assertTrue(q_changed)

        # Partial event with only TransportState must preserve existing title/artist/source.
        partial_lc = (
            '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
            '<InstanceID val="0"><TransportState val="PAUSED_PLAYBACK"/></InstanceID></Event>'
        )
        partial_xml = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f"<e:property><LastChange>{sonos.xml_escape(partial_lc)}</LastChange></e:property>"
            "</e:propertyset>"
        )
        st2, need_pos2, q_changed2 = sonos.parse_gena_avtransport_event(
            "192.168.4.62", partial_xml, st
        )
        assert st2 is not None
        self.assertEqual(st2.playback_status, "Paused")
        self.assertEqual(st2.title, "Silver Pearl")
        self.assertEqual(st2.source_name, "Spotify Connect")
        self.assertTrue(need_pos2)
        self.assertFalse(q_changed2)

    def test_gena_http_server_validates_sid_and_accepts_notify(self) -> None:
        received: list[tuple[str, str]] = []
        ev = threading.Event()

        def on_event(sub, body: str) -> None:
            received.append((sub.sid, body))
            ev.set()

        mgr = sonos._GenaManager(on_event=on_event)
        try:
            with mock.patch.dict(os.environ, {"MC_SONOS_GENA_PORT": "0"}):
                self.assertTrue(mgr._start_server("127.0.0.1"))
            parsed = sonos.urllib.parse.urlparse(mgr.callback_url)
            port = parsed.port
            assert port

            # Unknown SID must be rejected with 412 Precondition Failed.
            c1 = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
            c1.request("NOTIFY", "/notify", body=b"<x/>", headers={"SID": "uuid:bogus"})
            r1 = c1.getresponse()
            r1.read()
            c1.close()
            self.assertEqual(r1.status, 412)

            # Spoofed peer IP mismatching sub.ip must be rejected with 403 Forbidden.
            spoof_sub = sonos._GenaSub("uuid:spoof-1", "192.168.4.62", "/Event", "group_rc", "RINCON_A", 9999999999.0)
            with mgr._lock:
                mgr._subs[spoof_sub.sid] = spoof_sub
            c_spoof = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
            c_spoof.request("NOTIFY", "/notify", body=b"<evil/>", headers={"SID": "uuid:spoof-1"})
            r_spoof = c_spoof.getresponse()
            r_spoof.read()
            c_spoof.close()
            self.assertEqual(r_spoof.status, 403)
            self.assertEqual(received, [])

            # Register a valid SID matching peer IP and verify truncated Content-Length -> 400.
            sub = sonos._GenaSub("uuid:valid-1", "127.0.0.1", "/Event", "group_rc", "RINCON_A", 9999999999.0)
            with mgr._lock:
                mgr._subs[sub.sid] = sub
            s_trunc = socket.create_connection(("127.0.0.1", port), timeout=2.0)
            try:
                s_trunc.sendall(
                    b"NOTIFY /notify HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"SID: uuid:valid-1\r\n"
                    b"SEQ: 1\r\n"
                    b"Content-Length: 50\r\n\r\n"
                    b"<short/>"
                )
                s_trunc.shutdown(socket.SHUT_WR)
                trunc_resp = s_trunc.recv(4096)
            finally:
                s_trunc.close()
            self.assertIn(b"400", trunc_resp)
            self.assertEqual(sub.last_seq, -1)
            self.assertEqual(received, [])

            # Send valid SEQ:1 NOTIFY -> 200 OK + callback invoked deterministically.
            ev.clear()
            c2 = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
            c2.request("NOTIFY", "/notify", body=b"<ok/>", headers={"SID": "uuid:valid-1", "SEQ": "1"})
            r2 = c2.getresponse()
            r2.read()
            c2.close()
            self.assertEqual(r2.status, 200)
            self.assertTrue(ev.wait(timeout=1.0))
            self.assertTrue(mgr._first_notify.wait(timeout=1.0))
            self.assertEqual(received, [("uuid:valid-1", "<ok/>")])

            # Stale SEQ <= last_seq must be dropped; follow with SEQ=2 to synchronize deterministically.
            c3 = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
            c3.request("NOTIFY", "/notify", body=b"<stale/>", headers={"SID": "uuid:valid-1", "SEQ": "1"})
            r3 = c3.getresponse()
            r3.read()
            c3.close()
            self.assertEqual(r3.status, 200)

            ev.clear()
            c4 = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
            c4.request("NOTIFY", "/notify", body=b"<ok2/>", headers={"SID": "uuid:valid-1", "SEQ": "2"})
            r4 = c4.getresponse()
            r4.read()
            c4.close()
            self.assertEqual(r4.status, 200)
            self.assertTrue(ev.wait(timeout=1.0))
            self.assertEqual(received, [("uuid:valid-1", "<ok/>"), ("uuid:valid-1", "<ok2/>")])

            # Stale generation _mark_notify must not fire _first_notify, and reset after shutdown stays unavailable.
            mgr._first_notify.clear()
            with mgr._lock:
                mgr._gen += 1
            mgr._mark_notify(expected_gen=0)
            self.assertFalse(mgr._first_notify.is_set())
            mgr.shutdown()
            mgr.reset()
            self.assertEqual(mgr.state, "unavailable")
        finally:
            mgr.shutdown()

    def test_gena_inflight_subscribe_waits_for_sid_registration(self) -> None:
        received: list[tuple[str, str]] = []
        ev = threading.Event()

        def on_event(sub, body: str) -> None:
            received.append((sub.sid, body))
            ev.set()

        mgr = sonos._GenaManager(on_event=on_event)
        try:
            with mock.patch.dict(os.environ, {"MC_SONOS_GENA_PORT": "0"}):
                self.assertTrue(mgr._start_server("127.0.0.1"))
            port = sonos.urllib.parse.urlparse(mgr.callback_url).port
            assert port

            with mgr._lock:
                mgr._inflight_subscribes = 1

            def register_soon() -> None:
                time.sleep(0.03)
                sub = sonos._GenaSub("uuid:race-0", "127.0.0.1", "/Event", "topology", "127.0.0.1", 9999999999.0)
                with mgr._lock:
                    mgr._subs[sub.sid] = sub
                    mgr._inflight_subscribes = 0
                    mgr._sub_cond.notify_all()

            t = threading.Thread(target=register_soon)
            t.start()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
            conn.request("NOTIFY", "/notify", body=b"<seq0/>", headers={"SID": "uuid:race-0", "SEQ": "0"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
            t.join(timeout=2.0)

            self.assertEqual(resp.status, 200)
            self.assertTrue(ev.wait(timeout=1.0))
            self.assertEqual(received, [("uuid:race-0", "<seq0/>")])
        finally:
            mgr.shutdown()

    def test_gena_seq_wraparound_and_transitioning_partial_source_preservation(self) -> None:
        self.assertTrue(sonos._is_gena_seq_newer(-1, 0))
        self.assertTrue(sonos._is_gena_seq_newer(5, 6))
        self.assertFalse(sonos._is_gena_seq_newer(5, 5))
        self.assertFalse(sonos._is_gena_seq_newer(5, 4))
        self.assertFalse(sonos._is_gena_seq_newer(5, 0))
        self.assertFalse(sonos._is_gena_seq_newer(0, 0))
        self.assertFalse(sonos._is_gena_seq_newer(4294967295, 0))
        self.assertTrue(sonos._is_gena_seq_newer(4294967295, 1))

        # TRANSITIONING -> PLAYING must trigger need_pos_sync and re-anchor position_at.
        lc_trans = (
            '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
            '<InstanceID val="0">'
            '<TransportState val="TRANSITIONING"/>'
            '<CurrentTrack val="1"/>'
            '<CurrentTrackDuration val="0:03:00"/>'
            '<CurrentTrackURI val="x-sonos-vli:RINCON_1:2,spotify:1"/>'
            '<CurrentTrackMetaData val="&lt;DIDL-Lite xmlns:dc=&quot;http://purl.org/dc/elements/1.1/&quot;&gt;'
            '&lt;item&gt;&lt;dc:title&gt;Song&lt;/dc:title&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;"/>'
            "</InstanceID></Event>"
        )
        xml_trans = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f"<e:property><LastChange>{sonos.xml_escape(lc_trans)}</LastChange></e:property>"
            "</e:propertyset>"
        )
        st1, need_pos1, _ = sonos.parse_gena_avtransport_event("192.168.4.62", xml_trans, None, now=100.0)
        assert st1 is not None
        self.assertTrue(st1.was_transitioning)
        self.assertTrue(need_pos1)
        self.assertEqual(st1.source_name, "Spotify Connect")

        lc_playing = (
            '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
            '<InstanceID val="0">'
            '<TransportState val="PLAYING"/>'
            '<AVTransportURI val="x-rincon-queue:RINCON_1#0"/>'
            "</InstanceID></Event>"
        )
        xml_playing = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f"<e:property><LastChange>{sonos.xml_escape(lc_playing)}</LastChange></e:property>"
            "</e:propertyset>"
        )
        st2, need_pos2, _ = sonos.parse_gena_avtransport_event("192.168.4.62", xml_playing, st1, now=102.0)
        assert st2 is not None
        self.assertFalse(st2.was_transitioning)
        self.assertTrue(need_pos2)
        self.assertEqual(st2.position_at, 102.0)
        # Partial AVTransportURI-only event must NOT clobber Spotify Connect with generic "Sonos".
        self.assertEqual(st2.source_name, "Spotify Connect")

    def test_gena_probe_falls_back_cleanly_when_notify_blocked(self) -> None:
        mgr = sonos._GenaManager(on_event=lambda _s, _b: None)
        fake_sub = sonos._GenaSub("uuid:probe-1", "192.168.4.62", "/ZoneGroupTopology/Event", "topology", "192.168.4.62", 9999999999.0)
        with (
            mock.patch.dict(os.environ, {"MC_SONOS_GENA_PORT": "0"}),
            mock.patch.object(sonos, "detect_local_ip_for", return_value="127.0.0.1"),
            mock.patch.object(mgr, "_subscribe", return_value=fake_sub),
            mock.patch.object(mgr, "_unsubscribe") as unsub,
        ):
            ok = mgr.probe("192.168.4.62", wait_sec=0.05)
        self.assertFalse(ok)
        self.assertFalse(mgr.active)
        self.assertEqual(mgr.state, "unavailable")
        self.assertIsNone(mgr._server)
        unsub.assert_called_once()

    def test_parse_gena_content_directory_event_filters_library_containers(self) -> None:
        xml = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            "<e:property><FavoritesUpdateID>RINCON_A:17</FavoritesUpdateID></e:property>"
            "<e:property><SavedQueuesUpdateID>RINCON_A:5</SavedQueuesUpdateID></e:property>"
            "<e:property><ContainerUpdateIDs>Q:0,99,FV:2,18,SQ:,6</ContainerUpdateIDs></e:property>"
            "</e:propertyset>"
        )
        updates = sonos.parse_gena_content_directory_event(xml)
        self.assertEqual(
            updates,
            {"fav": "RINCON_A:17", "sq": "RINCON_A:5", "FV:2": "18", "SQ:": "6"},
        )
        self.assertNotIn("Q:0", updates)

        queue_only = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            "<e:property><ContainerUpdateIDs>Q:0,100</ContainerUpdateIDs></e:property>"
            "</e:propertyset>"
        )
        self.assertEqual(sonos.parse_gena_content_directory_event(queue_only), {})

    def test_parse_gena_avtransport_tracks_current_uri_and_position_anchor(self) -> None:
        lc_play = (
            '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
            '<InstanceID val="0">'
            '<TransportState val="PLAYING"/>'
            '<CurrentTrack val="2"/>'
            '<CurrentTrackDuration val="0:03:00"/>'
            '<AVTransportURI val="x-rincon-queue:RINCON_1#0"/>'
            '<CurrentTrackURI val="x-sonos-http:track2.mp4?sid=284"/>'
            '<CurrentTrackMetaData val="&lt;DIDL-Lite xmlns:dc=&quot;http://purl.org/dc/elements/1.1/&quot;&gt;'
            '&lt;item&gt;&lt;dc:title&gt;Song 2&lt;/dc:title&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;"/>'
            "</InstanceID></Event>"
        )
        xml_play = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f"<e:property><LastChange>{sonos.xml_escape(lc_play)}</LastChange></e:property>"
            "</e:propertyset>"
        )
        st1, _, _ = sonos.parse_gena_avtransport_event("192.168.4.62", xml_play, None, now=100.0)
        assert st1 is not None
        self.assertEqual(st1.current_uri, "x-rincon-queue:RINCON_1#0")
        self.assertEqual(st1.position_sec, 0)
        self.assertEqual(st1.position_at, 100.0)

        # Pause 25 seconds later -> freezes extrapolated position at 25s.
        lc_pause = (
            '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
            '<InstanceID val="0"><TransportState val="PAUSED_PLAYBACK"/></InstanceID></Event>'
        )
        xml_pause = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f"<e:property><LastChange>{sonos.xml_escape(lc_pause)}</LastChange></e:property>"
            "</e:propertyset>"
        )
        st2, _, _ = sonos.parse_gena_avtransport_event("192.168.4.62", xml_pause, st1, now=125.0)
        assert st2 is not None
        self.assertEqual(st2.playback_status, "Paused")
        self.assertEqual(st2.position_sec, 25)
        self.assertEqual(st2.position_at, 125.0)
        self.assertEqual(st2.current_uri, "x-rincon-queue:RINCON_1#0")

        # Resume 500 seconds later -> position stays 25s and re-anchors at t=625.0!
        lc_resume = (
            '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
            '<InstanceID val="0"><TransportState val="PLAYING"/></InstanceID></Event>'
        )
        xml_resume = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f"<e:property><LastChange>{sonos.xml_escape(lc_resume)}</LastChange></e:property>"
            "</e:propertyset>"
        )
        st3, _, _ = sonos.parse_gena_avtransport_event("192.168.4.62", xml_resume, st2, now=625.0)
        assert st3 is not None
        self.assertEqual(st3.position_sec, 25)
        self.assertEqual(st3.position_at, 625.0)
        self.assertEqual(sonos.extrapolate_position_sec(st3, now=635.0), 35)

    def test_gena_chunked_notify_valid_oversized_and_malformed(self) -> None:
        received: list[tuple[str, str]] = []
        mgr = sonos._GenaManager(on_event=lambda sub, body: received.append((sub.sid, body)))
        try:
            with mock.patch.dict(os.environ, {"MC_SONOS_GENA_PORT": "0"}):
                self.assertTrue(mgr._start_server("127.0.0.1"))
            port = sonos.urllib.parse.urlparse(mgr.callback_url).port
            assert port
            sub = sonos._GenaSub("uuid:chunk-1", "127.0.0.1", "/Event", "topology", "127.0.0.1", 9999999999.0)
            with mgr._lock:
                mgr._subs[sub.sid] = sub

            def send_raw(raw_req: bytes) -> bytes:
                s = socket.create_connection(("127.0.0.1", port), timeout=2.0)
                try:
                    s.sendall(raw_req)
                    s.shutdown(socket.SHUT_WR)
                    chunks: list[bytes] = []
                    while True:
                        d = s.recv(4096)
                        if not d:
                            break
                        chunks.append(d)
                    return b"".join(chunks)
                finally:
                    s.close()

            # Valid multi-chunk NOTIFY with chunk extension and trailer.
            valid_req = (
                b"NOTIFY /notify HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"SID: uuid:chunk-1\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
                b"6;ext=1\r\n<hello\r\n"
                b"7\r\n>world<\r\n"
                b"8\r\n/hello>\n\r\n"
                b"0\r\n"
                b"X-Trailer: ok\r\n"
                b"\r\n"
            )
            resp1 = send_raw(valid_req)
            self.assertIn(b"200 OK", resp1)
            self.assertEqual(received, [("uuid:chunk-1", "<hello>world</hello>\n")])

            # Oversized chunk header exceeding MAX_HTTP_BYTES -> 413.
            received.clear()
            too_big = f"{sonos.MAX_HTTP_BYTES + 10:x}".encode("ascii")
            big_req = (
                b"NOTIFY /notify HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"SID: uuid:chunk-1\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
                + too_big
                + b"\r\nabc\r\n0\r\n\r\n"
            )
            resp2 = send_raw(big_req)
            self.assertIn(b"413", resp2)
            self.assertEqual(received, [])

            # Malformed chunk size -> 400.
            bad_req = (
                b"NOTIFY /notify HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"SID: uuid:chunk-1\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
                b"ZZZZ\r\nhello\r\n0\r\n\r\n"
            )
            resp3 = send_raw(bad_req)
            self.assertIn(b"400", resp3)
            self.assertEqual(received, [])
        finally:
            mgr.shutdown()

    def test_gena_timeout_600_content_directory_sub_and_reset(self) -> None:
        self.assertEqual(sonos.GENA_SUB_TIMEOUT_SEC, 600)
        self.assertEqual(sonos.GENA_RENEW_BEFORE_SEC, 180.0)

        mgr = sonos._GenaManager(on_event=lambda _s, _b: None)
        subscribed_paths: list[tuple[str, str]] = []

        def fake_sub(ip: str, path: str, kind: str, target_uuid: str, **_kwargs):
            subscribed_paths.append((kind, path))
            sub = sonos._GenaSub(f"uuid:{kind}:{target_uuid}", ip, path, kind, target_uuid, 9999999999.0)
            with mgr._lock:
                mgr._subs[sub.sid] = sub
            return sub

        g = sonos.ZoneGroup("RINCON_1", "192.168.4.10", "Kitchen", ("RINCON_1",), ("Kitchen",))
        s = sonos.SpeakerInfo("RINCON_1", "192.168.4.10", "Kitchen", "RINCON_1", True)
        try:
            with mock.patch.dict(os.environ, {"MC_SONOS_GENA_PORT": "0"}):
                self.assertTrue(mgr._start_server("127.0.0.1"))
            with mgr._lock:
                mgr._state = "active"
            with (
                mock.patch.object(mgr, "_subscribe", side_effect=fake_sub),
                mock.patch.object(mgr, "_unsubscribe"),
                concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex,
            ):
                still_active = mgr.sync_subscriptions([g], [s], "192.168.4.10", ex)
            self.assertTrue(still_active)
            self.assertIn(("content_directory", "/MediaServer/ContentDirectory/Event"), subscribed_paths)
            self.assertTrue(mgr.has_kind("content_directory"))
            diag = mgr.diagnostics()
            self.assertTrue(diag["active"])
            self.assertEqual(diag["sub_timeout_sec"], 600)
            self.assertEqual(diag["subscriptions_by_kind"].get("content_directory"), 1)

            with mock.patch.object(mgr, "_unsubscribe"):
                mgr.reset()
            self.assertEqual(mgr.state, "unprobed")
            self.assertEqual(mgr.local_ip, "")
            self.assertIsNone(mgr._server)
        finally:
            mgr.shutdown()

    def test_gena_notify_commits_before_200_and_412_if_invalidated(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        events: list[str] = []

        def slow_on_event(_sub, body: str) -> None:
            events.append(body)
            entered.set()
            release.wait(timeout=2.0)

        mgr = sonos._GenaManager(on_event=slow_on_event)
        try:
            with mock.patch.dict(os.environ, {"MC_SONOS_GENA_PORT": "0"}):
                self.assertTrue(mgr._start_server("127.0.0.1"))
            port = sonos.urllib.parse.urlparse(mgr.callback_url).port
            assert port
            sub = sonos._GenaSub("uuid:order-1", "127.0.0.1", "/Event", "avtransport", "RINCON_1", 9999999999.0)
            with mgr._lock:
                mgr._subs[sub.sid] = sub

            res1: dict[str, int] = {}

            def send_notify(seq_val: str, body: str, out: dict[str, int]) -> None:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
                try:
                    c.request("NOTIFY", "/notify", body=body.encode("utf-8"), headers={"SID": sub.sid, "SEQ": seq_val})
                    resp = c.getresponse()
                    resp.read()
                    out["status"] = resp.status
                finally:
                    c.close()

            t1 = threading.Thread(target=send_notify, args=("0", "first", res1))
            t1.start()
            self.assertTrue(entered.wait(timeout=2.0))
            # While slow_on_event is still holding sub.seq_lock, HTTP 200 must NOT have returned yet.
            t1.join(timeout=0.05)
            self.assertTrue(t1.is_alive())

            # A second NOTIFY arriving while sub is invalidated before acquiring seq_lock must get 412.
            res2: dict[str, int] = {}
            t2 = threading.Thread(target=send_notify, args=("1", "second", res2))
            t2.start()
            time.sleep(0.05)
            with mgr._lock:
                mgr._subs.pop(sub.sid, None)
            release.set()
            t1.join(timeout=2.0)
            t2.join(timeout=2.0)
            self.assertEqual(res1.get("status"), 200)
            self.assertEqual(res2.get("status"), 412)
            self.assertEqual(events, ["first"])
        finally:
            release.set()
            mgr.shutdown()

    def test_apply_gena_avtransport_never_calls_network_under_lock(self) -> None:
        prev = sonos.TrackState(
            playback_status="Playing",
            title="Track",
            artist="Artist",
            album="Album",
            art_url="",
            source_name="Spotify",
            source_icon="",
            duration_sec=120,
            position_sec=10,
            track_num=1,
            shuffle=False,
            repeat="none",
            group_volume=30,
            group_muted=False,
            can_next=True,
            can_prev=True,
            current_uri="x-rincon-cpcontainer:100?sid=9",
        )
        # Partial event with CurrentTrackURI lacking sid= while prev.current_uri has sid=.
        with mock.patch.object(sonos, "fetch_music_services", side_effect=AssertionError("network call under lock!")):
            st, _, _ = sonos._apply_gena_avtransport_payload(
                "192.168.4.10",
                {"CurrentTrackURI": "x-sonos-http:track.mp3"},
                None,
                None,
                prev,
                now=100.0,
            )
        self.assertEqual(st.source_name, "Spotify")

    def test_partial_avtransport_uri_metadata_does_not_wipe_track_metadata(self) -> None:
        prev = sonos.TrackState(
            playback_status="Playing",
            title="Blue in Green",
            artist="Miles Davis",
            album="Kind of Blue",
            art_url="https://i.scdn.co/image/abc",
            source_name="Spotify",
            source_icon="",
            duration_sec=337,
            position_sec=95,
            track_num=3,
            shuffle=False,
            repeat="none",
            group_volume=35,
            group_muted=False,
            can_next=True,
            can_prev=True,
            position_at=500.0,
            current_uri="x-rincon-queue:RINCON_1#0",
        )
        # Partial event with only empty AVTransportURIMetaData must keep track metadata and position.
        lc_partial = (
            '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
            '<InstanceID val="0"><AVTransportURIMetaData val=""/></InstanceID></Event>'
        )
        xml_partial = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f"<e:property><LastChange>{sonos.xml_escape(lc_partial)}</LastChange></e:property>"
            "</e:propertyset>"
        )
        st, need_pos, _ = sonos.parse_gena_avtransport_event(
            "192.168.4.10", xml_partial, prev, now=510.0
        )
        assert st is not None
        self.assertEqual(st.title, "Blue in Green")
        self.assertEqual(st.artist, "Miles Davis")
        self.assertEqual(st.art_url, "https://i.scdn.co/image/abc")
        self.assertEqual(st.position_sec, 95)
        self.assertFalse(need_pos)

        # Incremental radio stream track change without AVTransportURI must keep current_uri & TuneIn source.
        radio_prev = sonos.replace(
            prev,
            title="Old Radio Song",
            source_name="TuneIn",
            current_uri="x-sonosapi-stream:s12345?sid=254",
        )
        lc_radio = (
            '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
            '<InstanceID val="0">'
            '<CurrentTrackURI val="x-rincon-mp3radio://stream/kexp.mp3"/>'
            '<CurrentTrackMetaData val="&lt;DIDL-Lite xmlns:dc=&quot;http://purl.org/dc/elements/1.1/&quot;&gt;'
            '&lt;item&gt;&lt;dc:title&gt;New Radio Song&lt;/dc:title&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;"/>'
            "</InstanceID></Event>"
        )
        xml_radio = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f"<e:property><LastChange>{sonos.xml_escape(lc_radio)}</LastChange></e:property>"
            "</e:propertyset>"
        )
        st_radio, _, _ = sonos.parse_gena_avtransport_event(
            "192.168.4.10", xml_radio, radio_prev, now=520.0
        )
        assert st_radio is not None
        self.assertEqual(st_radio.title, "New Radio Song")
        self.assertEqual(st_radio.current_uri, "x-sonosapi-stream:s12345?sid=254")
        self.assertEqual(st_radio.source_name, "TuneIn")

    def test_gena_concurrent_reset_waits_for_server_close_before_unprobed(self) -> None:
        mgr = sonos._GenaManager(on_event=lambda _s, _b: None)
        entered_close = threading.Event()
        release_close = threading.Event()
        orig_close = mgr._close_server

        def slow_close(srv):
            if srv is not None:
                entered_close.set()
                release_close.wait(timeout=2.0)
            orig_close(srv)

        try:
            with mock.patch.dict(os.environ, {"MC_SONOS_GENA_PORT": "0"}):
                self.assertTrue(mgr._start_server("127.0.0.1"))
            with mgr._lock:
                mgr._state = "active"
            with mock.patch.object(mgr, "_close_server", side_effect=slow_close):
                t1 = threading.Thread(target=mgr.reset)
                t1.start()
                self.assertTrue(entered_close.wait(timeout=2.0))
                t2 = threading.Thread(target=mgr.reset)
                t2.start()
                # Thread 2 must wait while Thread 1 is still closing the server socket.
                t2.join(timeout=0.08)
                self.assertTrue(t2.is_alive())
                self.assertEqual(mgr.state, "resetting")
                release_close.set()
                t1.join(timeout=2.0)
                t2.join(timeout=2.0)
            self.assertEqual(mgr.state, "unprobed")
            self.assertEqual(mgr._closing_servers, 0)
        finally:
            release_close.set()
            mgr.shutdown()


class TestQmlUiSafetyAndScrollBehavior(unittest.TestCase):
    def test_no_panel_slider_wheel_hijack_in_plugin_qml(self) -> None:
        qml_files = sorted(PLUGIN_DIR.glob("*.qml"))
        self.assertTrue(qml_files)
        for path in qml_files:
            content = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "PanelSlider {",
                content,
                f"{path.name} must use SonosSlider instead of PanelSlider so scrolling is not hijacked",
            )

        slider_qml = (PLUGIN_DIR / "SonosSlider.qml").read_text(encoding="utf-8")
        self.assertNotIn("onWheel:", slider_qml)
        self.assertIn("if (!root.dragging) return", slider_qml)

        panel_qml = (PLUGIN_DIR / "Panel.qml").read_text(encoding="utf-8")
        self.assertIn("Util.wheelSteps(", panel_qml)

    def test_all_qml_text_elements_enforce_plain_text(self) -> None:
        import re

        for path in sorted(PLUGIN_DIR.glob("*.qml")):
            content = path.read_text(encoding="utf-8")
            for match in re.finditer(r"\bText\s*\{", content):
                # Inspect up to the next 400 chars of the Text block for textFormat: Text.PlainText
                snippet = content[match.start() : match.start() + 400]
                self.assertIn(
                    "textFormat: Text.PlainText",
                    snippet,
                    f"Text element in {path.name} missing textFormat: Text.PlainText",
                )

    def test_speakers_drawer_uses_stable_uuid_models(self) -> None:
        speakers_qml = (PLUGIN_DIR / "SpeakersDrawer.qml").read_text(encoding="utf-8")
        self.assertIn("root.controller.groupUuids", speakers_qml)
        self.assertIn("root.controller.speakerUuids", speakers_qml)
        self.assertNotIn("model: root.visible ? root.controller.groups", speakers_qml)
        self.assertNotIn("root.controller.speakers :", speakers_qml)

    def test_rpc_uses_single_json_parse_and_service_backoff_preserves_delay(self) -> None:
        panel_qml = (PLUGIN_DIR / "Panel.qml").read_text(encoding="utf-8")
        self.assertNotIn("Model.toPlain(", panel_qml)
        self.assertIn("queueCoordUuid", panel_qml)

        service_qml = (PLUGIN_DIR / "Service.qml").read_text(encoding="utf-8")
        self.assertIn("startedAtMs", service_qml)
        self.assertNotIn("if (running) root.restartDelayMs = 2000", service_qml)

        rpc_qml = (PLUGIN_DIR / "SonosRpc.qml").read_text(encoding="utf-8")
        self.assertIn("delete _pendingVolumes[volKey]", rpc_qml)
        self.assertIn("requestSeq", rpc_qml)
        self.assertIn('{"cmd":"subscribe"}', rpc_qml)
        self.assertIn("commandCompleted(", rpc_qml)

        self.assertIn("subscribed: root.opened", panel_qml)
        self.assertIn("genaActive", panel_qml)
        self.assertIn("coordUuid: target", panel_qml)
        self.assertIn("_speakersReady", panel_qml)
        self.assertIn("openSyncFallback", panel_qml)
        self.assertIn("onActiveCoordUuidChanged", panel_qml)
        self.assertIn("_libraryVersion", panel_qml)
        self.assertIn("_volumeDragCount", panel_qml)
        self.assertIn("onEventStreamDisconnected", panel_qml)
        self.assertIn("onEventStreamConnected", panel_qml)
        self.assertIn("onStateFailed", panel_qml)
        self.assertIn("onSearchFocusedChanged", panel_qml)
        self.assertIn("rpc.eventConnected", panel_qml)
        self.assertIn("_queueFetchedCoord", panel_qml)
        self.assertIn("if (root.opened) root.genaActive = !!data.gena_active && rpc.eventConnected", panel_qml)
        self.assertIn("if (!root.opened || root._openedSyncPending) return", panel_qml)

        self.assertIn("eventStreamConnected()", rpc_qml)
        self.assertIn("eventStreamDisconnected()", rpc_qml)
        self.assertIn("stateFailed()", rpc_qml)
        self.assertIn("property bool gotResponse: false", rpc_qml)
        self.assertIn("_pendingLibraryFetch", rpc_qml)
        self.assertIn("property bool reconnecting: false", rpc_qml)
        self.assertIn("_isAudioOrTopoBarrier", rpc_qml)
        self.assertIn('else if (payload.cmd === "seek")', rpc_qml)
        self.assertIn("readonly property bool barHidden:", panel_qml)
        self.assertIn("root.effectiveLabelWidth > 0 && implicitWidth > scrollClip.width", panel_qml)
        self.assertNotIn("if (ch.busy) ch.finish(false, \"\")\n      }\n    }", rpc_qml)

        queue_qml = (PLUGIN_DIR / "QueueDrawer.qml").read_text(encoding="utf-8")
        self.assertIn("fetchQueue(true)", queue_qml)


@unittest.skipUnless(shutil.which("node"), "node is required for Model.js unit tests")
class TestModelJs(unittest.TestCase):
    def test_model_js_functions_and_edge_cases(self) -> None:
        model_path = PLUGIN_DIR / "Model.js"
        js_harness = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const src = fs.readFileSync(process.argv[1], 'utf8').replace(/^\s*\.pragma\s+library\s*\n?/, '');
const ctx = vm.createContext({});
vm.runInContext(src, ctx);
const plain = (x) => JSON.parse(JSON.stringify(x));

// clampInt
assert.strictEqual(ctx.clampInt(5, 1, 15, 4), 5);
assert.strictEqual(ctx.clampInt(0, 1, 15, 4), 1);
assert.strictEqual(ctx.clampInt(99, 1, 15, 4), 15);
assert.strictEqual(ctx.clampInt('abc', 1, 15, 4), 4);
assert.strictEqual(ctx.clampInt(3.6, 1, 15, 4), 4);

// sanitizeTooltip
assert.strictEqual(ctx.sanitizeTooltip('<b>Hi</b> & Bye'), 'bHi/b & Bye');
assert.strictEqual(ctx.sanitizeTooltip(null), '');

// formatPercent & formatDuration
const fmt = (n) => String(n);
assert.strictEqual(ctx.formatPercent(42.4, fmt), '42%');
assert.strictEqual(ctx.formatPercent(150, fmt), '100%');
assert.strictEqual(ctx.formatPercent(-5, fmt), '0%');
assert.strictEqual(ctx.formatDuration(0, fmt), '0:00');
assert.strictEqual(ctx.formatDuration(65, fmt), '1:05');
assert.strictEqual(ctx.formatDuration(3661, fmt), '1:01:01');
assert.strictEqual(ctx.formatDuration(-10, fmt), '0:00');

// volumeIcon
assert.strictEqual(ctx.volumeIcon(50, true), ctx.volumeIcon(0, false));
assert.notStrictEqual(ctx.volumeIcon(10, false), ctx.volumeIcon(50, false));
assert.notStrictEqual(ctx.volumeIcon(50, false), ctx.volumeIcon(80, false));

// activeSpeakersList & isSpeakerInGroup
const g1 = {
  coord_uuid: 'RINCON_K',
  coord_name: 'Kitchen',
  room_label: 'Kitchen (+1)',
  member_uuids: ['RINCON_K', 'RINCON_D'],
  member_names: ['Kitchen', 'Den'],
  playback_status: 'Paused',
  title: 'Song A',
  artist: 'Artist A'
};
const g2 = {
  coord_uuid: 'RINCON_O',
  coord_name: 'Office',
  room_label: 'Office',
  member_uuids: ['RINCON_O'],
  member_names: ['Office'],
  playback_status: 'Playing',
  title: 'Song B',
  artist: 'Artist B'
};
const g3 = {
  coord_uuid: 'RINCON_P',
  coord_name: 'Porch',
  room_label: 'Porch',
  member_uuids: ['RINCON_P'],
  member_names: ['Porch'],
  playback_status: 'Stopped',
  title: '',
  artist: ''
};
const groups = [g1, g2, g3];
const speakers = [
  { uuid: 'RINCON_D', name: 'Den', volume: 20, muted: false },
  { uuid: 'RINCON_K', name: 'Kitchen', volume: 40, muted: false },
  { uuid: 'RINCON_O', name: 'Office', volume: 30, muted: false },
  { uuid: 'RINCON_P', name: 'Porch', volume: 10, muted: true }
];

assert.strictEqual(ctx.activeSpeakersList(g1), 'Kitchen · Den');
assert.strictEqual(ctx.activeSpeakersList(null), '');
assert.strictEqual(ctx.isSpeakerInGroup(g1, speakers[0]), true);
assert.strictEqual(ctx.isSpeakerInGroup(g1, speakers[2]), false);

// findActiveGroup
assert.strictEqual(ctx.findActiveGroup([], '', ''), null);
assert.strictEqual(ctx.findActiveGroup(groups, 'RINCON_P', 'Office').coord_uuid, 'RINCON_P');
assert.strictEqual(ctx.findActiveGroup(groups, '', 'den').coord_uuid, 'RINCON_K');
assert.strictEqual(ctx.findActiveGroup(groups, '', 'rincon_d').coord_uuid, 'RINCON_K');
assert.strictEqual(ctx.findActiveGroup(groups, '', 'KITCHEN').coord_uuid, 'RINCON_K');
assert.strictEqual(ctx.findActiveGroup(groups, '', '').coord_uuid, 'RINCON_O');
assert.strictEqual(ctx.findActiveGroup([g3, g1], '', '').coord_uuid, 'RINCON_K');
assert.strictEqual(ctx.findActiveGroup([g3], '', '').coord_uuid, 'RINCON_P');

// findActiveMprisPlayer (exact room name match, ignores substring 'Kitchen' vs 'Kitchen 2')
const players = [
  { dbusName: 'org.mpris.MediaPlayer2.spotify', identity: 'Spotify', isPlaying: true, trackTitle: 'X' },
  { dbusName: 'org.mpris.MediaPlayer2.sonos_k2', identity: 'Sonos · Kitchen 2', isPlaying: true, trackTitle: 'K2' },
  { dbusName: 'org.mpris.MediaPlayer2.sonos_k1', identity: 'Sonos · Kitchen (+1)', isPlaying: false, trackTitle: 'K1' }
];
assert.strictEqual(ctx.findActiveMprisPlayer(players, 'Kitchen').dbusName, 'org.mpris.MediaPlayer2.sonos_k1');
assert.strictEqual(ctx.findActiveMprisPlayer(players, 'Kitchen 2').dbusName, 'org.mpris.MediaPlayer2.sonos_k2');
assert.strictEqual(ctx.findActiveMprisPlayer(players, '').dbusName, 'org.mpris.MediaPlayer2.sonos_k2');
assert.strictEqual(ctx.findActiveMprisPlayer([players[0]], ''), null);

// indexByKey & groupMembers (coordinator first)
const byUuid = ctx.indexByKey(speakers, 'uuid');
assert.strictEqual(byUuid['RINCON_K'].name, 'Kitchen');
const members = ctx.groupMembers(g1, speakers);
assert.deepStrictEqual(plain(members.map((s) => s.uuid)), ['RINCON_K', 'RINCON_D']);

// buildSpeakersRows
const rows = ctx.buildSpeakersRows(groups, speakers, g2);
assert.strictEqual(rows.length, groups.length + speakers.length);
const officeRow = rows.find((r) => r.kind === 'speaker' && r.speaker.uuid === 'RINCON_O');
assert.strictEqual(officeRow.inGroup, true);
assert.strictEqual(officeRow.isCoord, true);
assert.strictEqual(officeRow.canToggleGroup, false);
const kitchenRow = rows.find((r) => r.kind === 'speaker' && r.speaker.uuid === 'RINCON_K');
assert.strictEqual(kitchenRow.inGroup, false);
assert.strictEqual(kitchenRow.canToggleGroup, true);
// Multi-room coordinator can leave its own group
const rowsMulti = ctx.buildSpeakersRows(groups, speakers, g1);
const coordMulti = rowsMulti.find((r) => r.kind === 'speaker' && r.speaker.uuid === 'RINCON_K');
assert.strictEqual(coordMulti.canToggleGroup, true);

// predictMemberVolumes
const pred = ctx.predictMemberVolumes({ a: 20, b: 40 }, 30, 60);
assert.deepStrictEqual(plain(pred), { a: 40, b: 80 });
const predZero = ctx.predictMemberVolumes({ a: 0, b: 0 }, 0, 25);
assert.deepStrictEqual(plain(predZero), { a: 25, b: 25 });
const predClamp = ctx.predictMemberVolumes({ a: 80 }, 50, 90);
assert.strictEqual(predClamp.a, 100);

// filterLibrary & filterQueue
const lib = [
  { title: 'Jazz Classics', description: 'Spotify Playlist', kind: 'favorite' },
  { title: 'Workout', description: '', kind: 'playlist' }
];
assert.strictEqual(ctx.filterLibrary(lib, '').length, 2);
assert.strictEqual(ctx.filterLibrary(lib, 'SPOTIFY').length, 1);
assert.strictEqual(ctx.filterLibrary(lib, 'playlist').length, 2);

const queue = [
  { title: 'Blue in Green', artist: 'Miles Davis', album: 'Kind of Blue' },
  { title: 'Roygbiv', artist: 'Boards of Canada', album: 'MHTRTC' }
];
assert.strictEqual(ctx.filterQueue(queue, '').length, 2);
assert.strictEqual(ctx.filterQueue(queue, 'miles').length, 1);
assert.strictEqual(ctx.filterQueue(queue, 'mhtrtc').length, 1);
assert.strictEqual(ctx.filterQueue(queue, 'missing').length, 0);

console.log('OK');
"""
        proc = subprocess.run(
            ["node", "-e", js_harness, str(model_path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Model.js tests failed:\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )
        self.assertEqual(proc.stdout.strip(), "OK")


if __name__ == "__main__":
    unittest.main()
