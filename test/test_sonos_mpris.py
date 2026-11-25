#!/usr/bin/env python3
"""Unit tests for mc.sonos sonos-mpris.py parsing, source detection, play modes, and safety."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
