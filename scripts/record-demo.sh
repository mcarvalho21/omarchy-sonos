#!/bin/bash
# Record docs/demo.gif by driving the Sonos panel with simulated key presses.
#
# Usage: scripts/record-demo.sh [room-name]
#
# Picks the first grouped room unless a room name is given. Runs on an empty
# workspace so nothing else is on screen, nudges that room's group volume up
# and back down to show the per-room sliders, then puts everything back.
#
# Needs: wf-recorder, ffmpeg, wtype, jq, hyprctl, qs (all on a stock Omarchy
# install except wtype and wf-recorder: `omarchy pkg add wtype wf-recorder`).
#
# Env: OUT (gif path), STEPS (volume steps up and back, default 6),
#      WIDTH (gif width in px, default 480), FPS (default 12).

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${OUT:-$ROOT/docs/demo.gif}
STEPS=${STEPS:-6}
WIDTH=${WIDTH:-480}
FPS=${FPS:-12}
SHELL_PATH="${OMARCHY_PATH:-/usr/share/omarchy}/shell"
# Logical-pixel region: the left of the bar plus the panel that opens under it.
REGION=${REGION:-"0,0 520x660"}

for dep in wf-recorder ffmpeg wtype jq hyprctl qs python3; do
  command -v "$dep" >/dev/null || { echo "record-demo: missing $dep" >&2; exit 1; }
done

rpc() { python3 "$ROOT/sonos-mpris.py" --rpc "$1"; }
panel() { qs -p "$SHELL_PATH" ipc call mc.sonos "$1" >/dev/null; }
key() { wtype "$1"; sleep "${2:-0.45}"; }
special() { wtype -k "$1"; sleep "${2:-0.45}"; }
focus_ws() { hyprctl dispatch "hl.dsp.focus({ workspace = \"$1\" })" >/dev/null; }

state=$(rpc '{"cmd":"get_state"}')
[[ $(jq -r .ok <<<"$state") == true ]] || { echo "record-demo: Sonos bridge not answering" >&2; exit 1; }

room=${1:-}
if [[ -z $room ]]; then
  room=$(jq -r '[.groups[] | select((.member_uuids | length) > 1)][0].coord_name // empty' <<<"$state")
  [[ -n $room ]] || { echo "record-demo: no grouped rooms; group two rooms or pass a room name" >&2; exit 1; }
fi
index=$(jq -r --arg r "$room" '[.groups[].coord_name] | index($r) // empty' <<<"$state")
[[ -n $index ]] || { echo "record-demo: no room named '$room'" >&2; exit 1; }
coord=$(jq -r ".groups[$index].coord_uuid" <<<"$state")
volume=$(jq -r ".groups[$index].group_volume" <<<"$state")
echo "record-demo: room '$room' (volume $volume%)"

tmp=$(mktemp -d)
orig_ws=$(hyprctl activeworkspace -j | jq -r .id)
rec_pid=""

cleanup() {
  [[ -n $rec_pid ]] && kill -INT "$rec_pid" 2>/dev/null && wait "$rec_pid" 2>/dev/null || true
  panel close || true
  rpc "{\"cmd\":\"set_group_volume\",\"coord_uuid\":\"$coord\",\"volume\":$volume}" >/dev/null || true
  focus_ws "$orig_ws"
  rm -rf "$tmp"
}
trap cleanup EXIT

panel close
focus_ws empty
sleep 0.8

wf-recorder -y -g "$REGION" -f "$tmp/demo.mp4" >/dev/null 2>&1 &
rec_pid=$!
sleep 0.3

panel open
sleep 1.6

# Speakers drawer, move to the room and select it.
key 1 1.0
key j 0.35                               # cursor on, at the group volume
key j 0.35                               # first room
for ((i = 0; i < index; i++)); do key j 0.35; done
sleep 0.4
special Return 1.2
key 1 0.9                                # close the drawer; cursor back on group volume

# Group volume up and back down: the room sliders appear and follow it.
for ((i = 0; i < STEPS; i++)); do key l 0.45; done
sleep 0.8
for ((i = 0; i < STEPS; i++)); do key h 0.45; done
sleep 1.6

# Playlists drawer (its search box takes typed keys, so just let it load).
key 2 2.6
special Escape 0.4

kill -INT "$rec_pid"
wait "$rec_pid" 2>/dev/null || true
rec_pid=""

mkdir -p "$(dirname "$OUT")"
ffmpeg -v error -y -i "$tmp/demo.mp4" \
  -vf "fps=$FPS,scale=$WIDTH:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" \
  "$OUT"
echo "record-demo: wrote $OUT ($(du -h "$OUT" | cut -f1))"
