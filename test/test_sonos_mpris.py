#!/usr/bin/env python3
"""Unit tests for mc.sonos sonos-mpris.py parsing, source detection, play modes, and safety."""

from __future__ import annotations

import concurrent.futures
import http.client
import importlib.util
import pathlib
import socket
import sys
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


if __name__ == "__main__":
    unittest.main()
