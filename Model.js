.pragma library

function clampInt(raw, minVal, maxVal, fallback) {
  var n = Math.round(Number(raw))
  if (!isFinite(n)) return fallback
  return Math.max(minVal, Math.min(maxVal, n))
}

function sanitizeTooltip(text) {
  return String(text || "").replace(/[<>]/g, "")
}

function formatPercent(n, formatNumberFn) {
  var clamped = Math.max(0, Math.min(100, Math.round(n || 0)))
  return formatNumberFn(clamped) + "%"
}

function formatDuration(sec, formatNumberFn) {
  var s = Math.max(0, Math.floor(sec || 0))
  var h = Math.floor(s / 3600)
  var m = Math.floor((s % 3600) / 60)
  var r = s % 60
  var zero = formatNumberFn(0)
  var rStr = r < 10 ? zero + formatNumberFn(r) : formatNumberFn(r)
  if (h > 0) {
    var mStr = m < 10 ? zero + formatNumberFn(m) : formatNumberFn(m)
    return formatNumberFn(h) + ":" + mStr + ":" + rStr
  }
  return formatNumberFn(m) + ":" + rStr
}

function volumeIcon(vol, muted) {
  if (muted || vol <= 0) return ""
  if (vol >= 67) return ""
  if (vol >= 34) return ""
  return ""
}

function activeSpeakersList(group) {
  if (!group) return ""
  var names = group.member_names
  if (Array.isArray(names) && names.length > 0) return names.join(" · ")
  return group.room_label || ""
}

function isSpeakerInGroup(group, speaker) {
  if (!group || !speaker) return false
  var members = group.member_uuids || []
  return members.indexOf(speaker.uuid) !== -1
}

function _extractMprisRoomName(identity) {
  var s = String(identity || "").trim()
  var prefix = "sonos · "
  if (s.toLowerCase().indexOf(prefix) === 0) s = s.slice(prefix.length).trim()
  return s.replace(/\s*\(\+\d+\)\s*$/, "").trim().toLowerCase()
}

function findActiveGroup(groups, selectedCoordUuid, preferredRoom) {
  if (!groups || groups.length === 0) return null
  if (selectedCoordUuid !== "") {
    for (var i = 0; i < groups.length; i++)
      if (groups[i].coord_uuid === selectedCoordUuid) return groups[i]
  }
  var pref = String(preferredRoom || "").trim().toLowerCase()
  if (pref !== "") {
    for (var r = 0; r < groups.length; r++) {
      if (String(groups[r].coord_name || "").toLowerCase() === pref
          || String(groups[r].coord_uuid || "").toLowerCase() === pref)
        return groups[r]
    }
    for (var m = 0; m < groups.length; m++) {
      var names = groups[m].member_names || []
      for (var n = 0; n < names.length; n++) {
        if (String(names[n] || "").toLowerCase() === pref) return groups[m]
      }
      var uuids = groups[m].member_uuids || []
      for (var u = 0; u < uuids.length; u++) {
        if (String(uuids[u] || "").toLowerCase() === pref) return groups[m]
      }
    }
  }
  for (var j = 0; j < groups.length; j++)
    if (groups[j].playback_status === "Playing") return groups[j]
  for (var k = 0; k < groups.length; k++)
    if (groups[k].title || groups[k].artist) return groups[k]
  return groups[0]
}

function findActiveMprisPlayer(mprisPlayers, preferredRoom) {
  if (!mprisPlayers || mprisPlayers.length === 0) return null
  var fallback = null
  var preferred = null
  var pref = String(preferredRoom || "").trim().toLowerCase()
  for (var i = 0; i < mprisPlayers.length; i++) {
    var p = mprisPlayers[i]
    if (!p) continue
    var dbus = String(p.dbusName || "")
    if (dbus.indexOf("org.mpris.MediaPlayer2.sonos_") !== 0) continue
    if (pref !== "" && _extractMprisRoomName(p.identity) === pref) {
      if (p.isPlaying) return p
      if (!preferred && (p.trackTitle || p.trackArtist)) preferred = p
    }
    if (p.isPlaying && pref === "") return p
    if (!fallback && (p.trackTitle || p.trackArtist)) fallback = p
  }
  if (preferred) return preferred
  for (var j = 0; j < mprisPlayers.length; j++) {
    var pj = mprisPlayers[j]
    if (!pj) continue
    if (String(pj.dbusName || "").indexOf("org.mpris.MediaPlayer2.sonos_") === 0 && pj.isPlaying) return pj
  }
  return fallback
}

function filterLibrary(items, query) {
  if (!items || items.length === 0) return []
  var q = String(query || "").trim().toLowerCase()
  if (!q) return items
  var out = []
  for (var i = 0; i < items.length; i++) {
    var item = items[i]
    var hay = ((item.title || "") + " " + (item.description || "") + " " + (item.kind || "")).toLowerCase()
    if (hay.indexOf(q) !== -1) out.push(item)
  }
  return out
}

function filterQueue(items, query) {
  if (!items || items.length === 0) return []
  var q = String(query || "").trim().toLowerCase()
  if (!q) return items
  var out = []
  for (var i = 0; i < items.length; i++) {
    var item = items[i]
    var hay = ((item.title || "") + " " + (item.artist || "") + " " + (item.album || "")).toLowerCase()
    if (hay.indexOf(q) !== -1) out.push(item)
  }
  return out
}

function buildSpeakersRows(groups, speakers, activeGroup) {
  var rows = []
  if (groups && groups.length > 0) {
    for (var i = 0; i < groups.length; i++) {
      rows.push({
        kind: "room",
        group: groups[i]
      })
    }
  }
  if (speakers && speakers.length > 1 && activeGroup) {
    for (var j = 0; j < speakers.length; j++) {
      var sp = speakers[j]
      var inGroup = isSpeakerInGroup(activeGroup, sp)
      var isCoord = activeGroup.coord_uuid === sp.uuid
      var canToggleGroup = !(isCoord && (activeGroup.member_uuids || []).length <= 1)
      rows.push({
        kind: "speaker",
        speaker: sp,
        inGroup: inGroup,
        isCoord: isCoord,
        canToggleGroup: canToggleGroup
      })
    }
  }
  return rows
}

function indexByKey(items, key) {
  var map = {}
  if (!items) return map
  for (var i = 0; i < items.length; i++) {
    var item = items[i]
    if (item && item[key] !== undefined && item[key] !== null)
      map[item[key]] = item
  }
  return map
}

// Speakers in a group, coordinator first, in the group's member order.
function groupMembers(group, speakers) {
  if (!group || !speakers) return []
  var members = group.member_uuids || []
  var out = []
  for (var i = 0; i < members.length; i++) {
    for (var j = 0; j < speakers.length; j++) {
      if (speakers[j].uuid === members[i]) {
        out.push(speakers[j])
        break
      }
    }
  }
  out.sort(function(a, b) {
    if (a.uuid === group.coord_uuid) return -1
    if (b.uuid === group.coord_uuid) return 1
    return 0
  })
  return out
}

// Sonos keeps grouped rooms' relative levels when the group volume changes.
// Estimate each room's new volume from where it was when the change began,
// so the room sliders move with the group slider before the speakers report back.
function predictMemberVolumes(baseVolumes, baseGroupVolume, newGroupVolume) {
  var out = {}
  var base = Number(baseGroupVolume) || 0
  var target = Math.max(0, Math.min(100, Number(newGroupVolume) || 0))
  for (var uuid in baseVolumes) {
    var v = Number(baseVolumes[uuid]) || 0
    var next = base > 0 ? Math.round(v * target / base) : target
    out[uuid] = Math.max(0, Math.min(100, next))
  }
  return out
}
