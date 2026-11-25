# Sonos (`mc.sonos`)

![Sonos panel: selecting a grouped room, the room volumes following the group volume, and the Playlists drawer](docs/demo.gif)

Control Sonos speakers on your local network from the Omarchy bar.

The bar shows the playing track. Click it to open a now-playing panel with
transport, seek and group volume. When rooms are grouped, setting the group
volume also shows each room's volume. Speakers, Playlists and Queue open as
drawers below it; click a tab again to close it.

A small bridge (`sonos-mpris.py`) talks to the speakers over the LAN and
exposes them over MPRIS, so media keys and the bar work while the panel is
closed. The panel only polls while it is open.

## Install

```bash
omarchy plugin add https://github.com/mcarvalho21/omarchy-sonos.git --enable
```

Needs Sonos speakers on the same local network. Update later with
`omarchy plugin update mc.sonos`.

## Bar

- Left-click: open the panel
- Right-click: play / pause
- Scroll: group volume

## Keys

`j`/`k` move, `h`/`l` adjust volume or seek, `Space`/`Enter` activate,
`m` mute, `n`/`p` next / previous, `a` play next, `1`/`2`/`3` toggle the
Speakers / Playlists / Queue drawer, `/` search, `r` refresh, `Esc` close.

## Options

Set these on the `mc.sonos` entry in `~/.config/omarchy/shell.json`:

| Key | Default | |
|---|---|---|
| `showTrackInBar` | `true` | Show the track title in the bar |
| `showArtistInBar` | `true` | Add the artist after the title |
| `preferredRoom` | `""` | Room to pick first (blank = whichever is playing) |
| `volumeStep` | `4` | Volume change per scroll or key step, 1–15 |

## Demo GIF

`scripts/record-demo.sh [room]` regenerates `docs/demo.gif`. It switches to an
empty workspace, drives the panel with simulated keys, nudges the room's group
volume up and back, and restores everything afterwards. Needs `wtype` and
`wf-recorder` (`omarchy pkg add wtype wf-recorder`).

## Testing

```bash
python3 -m unittest discover -s ~/.config/omarchy/plugins/mc.sonos/test -v
omarchy plugin validate ~/.config/omarchy/plugins/mc.sonos
```
