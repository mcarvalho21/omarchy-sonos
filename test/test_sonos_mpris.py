#!/usr/bin/env python3
"""Unit tests for mc.sonos sonos-mpris.py parsing, source detection, play modes, and safety."""

from __future__ import annotations

import concurrent.futures
import http.client
import importlib.util
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
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
        mgr = sonos._GenaManager(on_event=lambda sub, body: received.append((sub.sid, body)))
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

            # Register a valid SID and send NOTIFY -> 200 OK + callback invoked.
            sub = sonos._GenaSub("uuid:valid-1", "192.168.4.62", "/Event", "group_rc", "RINCON_A", 9999999999.0)
            with mgr._lock:
                mgr._subs[sub.sid] = sub
            c2 = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
            c2.request("NOTIFY", "/notify", body=b"<ok/>", headers={"SID": "uuid:valid-1"})
            r2 = c2.getresponse()
            r2.read()
            c2.close()
            self.assertEqual(r2.status, 200)
            self.assertTrue(mgr._first_notify.is_set())
            self.assertEqual(received, [("uuid:valid-1", "<ok/>")])
        finally:
            mgr.shutdown()

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

        queue_qml = (PLUGIN_DIR / "QueueDrawer.qml").read_text(encoding="utf-8")
        self.assertIn("fetchQueue(true)", queue_qml)


if __name__ == "__main__":
    unittest.main()
