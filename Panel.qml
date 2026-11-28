import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Services.Mpris
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "mc.sonos"
  ipcTarget: "mc.sonos"

  property string fixedEdge: "center"
  property bool inTraySection: false
  readonly property bool vertical: bar ? bar.vertical : false
  readonly property int barSize: bar ? bar.barSize : Style.bar.sizeHorizontal
  readonly property bool barHidden: !!(bar && (bar.barHidden === true
    || (bar.shell && bar.shell.bar && bar.shell.bar.barHidden === true)))

  Commons.Translations { id: sonosStrings; directory: Qt.resolvedUrl("i18n") }

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color accent: Commons.Color.accent
  readonly property color dim: Qt.darker(foreground, 1.45)
  readonly property color dimmer: Qt.darker(foreground, 1.75)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // Options, set in this widget's ~/.config/omarchy/shell.json entry.
  readonly property bool showTrackInBar: !!setting("showTrackInBar", true)
  readonly property bool showArtistInBar: !!setting("showArtistInBar", true)
  readonly property string preferredRoom: String(setting("preferredRoom", "") || "")
  readonly property int volumeStep: Model.clampInt(setting("volumeStep", 4), 1, 15, 4)

  // Fixed behavior.
  readonly property bool scrollTrackInBar: true
  readonly property int maxBarLabelWidth: 185
  readonly property int seekStep: 5
  readonly property int pollIntervalSec: 2
  readonly property int queueLimit: 200

  // Zero-cost D-Bus MPRIS view for the bar trigger while the panel is closed.
  readonly property var mprisPlayers: Mpris.players ? Mpris.players.values : []
  readonly property var activeMprisPlayer: Model.findActiveMprisPlayer(mprisPlayers, preferredRoom)

  // On-demand rich Sonos state populated over direct Unix RPC ONLY while panel is open.
  property var groups: []
  property var speakers: []
  property string selectedCoordUuid: ""
  property bool expanded: false
  property string activeTab: "speakers"
  property string playlistQuery: ""
  property string queueQuery: ""
  property var libraryItems: []
  property var queueItems: []
  property string queueCoordUuid: ""
  property string _queueFetchedCoord: ""
  property bool genaActive: false
  property bool _speakersReady: false
  property bool _openedSyncDone: false
  property bool _openedSyncPending: false
  property int _libraryVersion: -1
  property int _volumeDragCount: 0
  property string _pendingRefreshCoord: ""
  property bool _pendingRefreshFull: false
  property bool _pendingRefreshIncludeSpeakers: true
  property int livePositionSec: 0
  property double lastSeekMs: 0
  property double _lastStateAtMs: 0
  property string _lastTrackKey: ""
  property real barWheelAccumulator: 0

  readonly property bool stateLoading: rpc.stateLoading
  readonly property bool libraryLoading: rpc.libraryLoading
  readonly property bool queueLoading: rpc.queueLoading
  readonly property bool needsMemberAudio: (expanded && activeTab === "speakers") || memberVolumesShown

  // Unified keyboard + mouse cursor model (matches omarchy.audio / omarchy.bluetooth):
  //   focusSection: "master" | "drawer"
  //   selectedIndex: -1 for "master", 0..N-1 inside the active drawer
  property bool cursorActive: false
  property string focusSection: "master"
  property int selectedIndex: -1

  readonly property var activeGroup: Model.findActiveGroup(groups, selectedCoordUuid, preferredRoom)
  readonly property string activeCoordUuid: activeGroup ? activeGroup.coord_uuid : ""
  readonly property var speakersRows: Model.buildSpeakersRows(groups, speakers, activeGroup)

  // Grouped-room volumes, shown under the group slider while it is being set (like the Sonos app).
  readonly property var groupsByUuid: Model.indexByKey(groups, "coord_uuid")
  readonly property var speakersByUuid: Model.indexByKey(speakers, "uuid")
  readonly property var groupMembers: Model.groupMembers(activeGroup, speakers)
  readonly property bool isMultiRoomGroup: groupMembers.length > 1
  // Stable string keys only change when topology/membership changes, so Repeater
  // rows are not destroyed (and slider drags are not cut off) on every poll tick.
  readonly property string groupKey: groups.map(function(g) { return g.coord_uuid }).join(",")
  readonly property var groupUuids: groupKey === "" ? [] : groupKey.split(",")
  readonly property string speakerKey: speakers.map(function(sp) { return sp.uuid }).join(",")
  readonly property var speakerUuids: speakerKey === "" ? [] : speakerKey.split(",")
  readonly property string groupMemberKey: groupMembers.map(function(sp) { return sp.uuid }).join(",")
  readonly property var groupMemberUuids: groupMemberKey === "" ? [] : groupMemberKey.split(",")

  function groupByUuid(uuid) {
    return groupsByUuid[uuid] || null
  }

  function speakerByUuid(uuid) {
    return speakersByUuid[uuid] || null
  }
  property bool memberVolumesShown: false
  property bool masterVolumeHovered: false
  property bool memberVolumesHovered: false
  property var memberVolumeBase: null
  property var groupVolumePreview: ({})
  property var memberVolumePreview: ({})
  property double lastVolumeAdjustMs: 0

  function setVolumeDragging(isDragging) {
    _volumeDragCount = Math.max(0, _volumeDragCount + (isDragging ? 1 : -1))
  }

  function revealMemberVolumes() {
    if (!isMultiRoomGroup) return
    memberVolumesShown = true
    memberVolumeHide.restart()
  }

  function hideMemberVolumes() {
    memberVolumeHide.stop()
    memberVolumesShown = false
    masterVolumeHovered = false
    memberVolumesHovered = false
    memberVolumeBase = null
    if (!memberVolumeSettle.running && _volumeDragCount <= 0)
      memberVolumePreview = ({})
  }

  function groupVolume(group) {
    if (!group) return 0
    var preview = groupVolumePreview[group.coord_uuid]
    return preview !== undefined ? preview : (group.group_volume || 0)
  }

  // Move the sliders optimistically until the speakers report real values.
  function previewGroupVolume(volume) {
    lastVolumeAdjustMs = Date.now()
    memberVolumeSettle.restart()
    if (!activeGroup) return
    var target = Math.max(0, Math.min(100, Math.round(volume)))
    var prevBaseGroup = groupVolume(activeGroup)
    var nextGroup = {}
    for (var gk in groupVolumePreview) nextGroup[gk] = groupVolumePreview[gk]
    nextGroup[activeGroup.coord_uuid] = target
    groupVolumePreview = nextGroup
    if (!isMultiRoomGroup) return
    if (!memberVolumeBase) {
      var vols = {}
      for (var i = 0; i < groupMembers.length; i++)
        vols[groupMembers[i].uuid] = memberVolume(groupMembers[i])
      memberVolumeBase = { group: prevBaseGroup, volumes: vols }
    }
    memberVolumePreview = Model.predictMemberVolumes(memberVolumeBase.volumes, memberVolumeBase.group, target)
    revealMemberVolumes()
  }

  function previewMemberVolume(speaker, volume, reveal) {
    if (!speaker) return
    lastVolumeAdjustMs = Date.now()
    memberVolumeSettle.restart()
    memberVolumeBase = null
    var next = {}
    for (var k in memberVolumePreview) next[k] = memberVolumePreview[k]
    next[speaker.uuid] = Math.max(0, Math.min(100, Math.round(volume)))
    memberVolumePreview = next
    if (activeGroup && groupVolumePreview[activeGroup.coord_uuid] !== undefined
        && Model.isSpeakerInGroup(activeGroup, speaker)) {
      var sum = 0
      var count = groupMembers.length
      for (var i = 0; i < count; i++) sum += memberVolume(groupMembers[i])
      if (count > 0) {
        var nextGroup = {}
        for (var gk in groupVolumePreview) nextGroup[gk] = groupVolumePreview[gk]
        nextGroup[activeGroup.coord_uuid] = Math.max(0, Math.min(100, Math.round(sum / count)))
        groupVolumePreview = nextGroup
      }
    }
    if (reveal !== false) revealMemberVolumes()
  }

  function memberVolume(speaker) {
    if (!speaker) return 0
    var preview = memberVolumePreview[speaker.uuid]
    return preview !== undefined ? preview : (speaker.volume || 0)
  }
  readonly property var filteredLibrary: Model.filterLibrary(libraryItems, playlistQuery)
  readonly property var filteredQueue: Model.filterQueue(queueItems, queueQuery)

  readonly property bool isPlaying: opened && activeGroup
    ? activeGroup.playback_status === "Playing"
    : !!(activeMprisPlayer && activeMprisPlayer.isPlaying)
  readonly property bool anyPlaying: {
    if (opened && groups.length > 0) {
      for (var i = 0; i < groups.length; i++)
        if (groups[i].playback_status === "Playing") return true
      return false
    }
    return !!(activeMprisPlayer && activeMprisPlayer.isPlaying)
  }
  readonly property string barTitle: opened && activeGroup
    ? (activeGroup.title || "")
    : (activeMprisPlayer ? (activeMprisPlayer.trackTitle || "") : "")
  readonly property string barArtist: opened && activeGroup
    ? (activeGroup.artist || "")
    : (activeMprisPlayer ? (activeMprisPlayer.trackArtist || "") : "")
  readonly property bool hasBarTrack: barTitle !== "" || (showArtistInBar && barArtist !== "")

  readonly property string activeSourceName: activeGroup && activeGroup.source_name
    ? activeGroup.source_name
    : sonosStrings.t("sonos.source.unknown")
  readonly property string activeSourceIcon: activeGroup && activeGroup.source_icon
    ? activeGroup.source_icon
    : "󰓃"
  readonly property string activeSpeakersList: Model.activeSpeakersList(activeGroup)

  function formatNumber(n) {
    return Commons.I18n.formatNumber(n)
  }

  function formatPercent(n) {
    return Model.formatPercent(n, formatNumber)
  }

  function formatDuration(sec) {
    return Model.formatDuration(sec, formatNumber)
  }

  // ---- Cursor navigation ----

  function activeDrawerRowCount() {
    if (!expanded) return 0
    if (activeTab === "speakers") return speakersRows.length
    if (activeTab === "playlists") return filteredLibrary.length
    if (activeTab === "queue") return filteredQueue.length
    return 0
  }

  function resetPointerGate() {
    if (panel.loadedContent && panel.loadedContent.pointerGate)
      panel.loadedContent.pointerGate.reset()
  }

  function clampCursor() {
    if (!activeGroup && focusSection === "master") {
      cursorActive = false
      selectedIndex = -1
      return
    }
    if (focusSection === "drawer") {
      var count = activeDrawerRowCount()
      if (count <= 0) {
        focusSection = "master"
        selectedIndex = -1
      } else if (selectedIndex >= count) {
        selectedIndex = count - 1
      } else if (selectedIndex < 0) {
        selectedIndex = 0
      }
    }
  }

  function setMasterCursor() {
    cursorActive = true
    focusSection = "master"
    selectedIndex = -1
  }

  function setDrawerCursor(index) {
    var count = activeDrawerRowCount()
    if (count <= 0) return
    cursorActive = true
    focusSection = "drawer"
    selectedIndex = Math.max(0, Math.min(count - 1, index))
  }

  function moveCursor(dx, dy) {
    if (dy !== 0) {
      resetPointerGate()
      if (!cursorActive) {
        cursorActive = true
        if (dy > 0 && focusSection === "master" && !activeGroup && activeDrawerRowCount() > 0) {
          focusSection = "drawer"
          selectedIndex = 0
        }
        return
      }
      var count = activeDrawerRowCount()
      if (focusSection === "master") {
        if (dy > 0 && count > 0) {
          focusSection = "drawer"
          selectedIndex = 0
        }
        return
      }
      if (focusSection === "drawer") {
        if (dy > 0) {
          if (selectedIndex < count - 1) selectedIndex += 1
        } else {
          if (selectedIndex > 0) selectedIndex -= 1
          else if (activeGroup) {
            focusSection = "master"
            selectedIndex = -1
          }
        }
      }
      return
    }

    if (dx !== 0) {
      // When the cursor sits on the master volume row or on a grouped speaker
      // row, h/l adjusts that slider (just like omarchy.audio). Otherwise h/l
      // seeks the current track.
      if (cursorActive && focusSection === "master" && activeGroup) {
        var nextGroupVol = Math.max(0, Math.min(100, groupVolume(activeGroup) + dx * volumeStep))
        previewGroupVolume(nextGroupVol)
        sendCommand({ cmd: "set_group_volume", coord_uuid: activeGroup.coord_uuid, volume: nextGroupVol })
        return
      }
      if (cursorActive && focusSection === "drawer" && activeTab === "speakers"
          && selectedIndex >= 0 && selectedIndex < speakersRows.length) {
        var row = speakersRows[selectedIndex]
        if (row.kind === "speaker" && row.inGroup) {
          var nextSpVol = Math.max(0, Math.min(100, memberVolume(row.speaker) + dx * volumeStep))
          previewMemberVolume(row.speaker, nextSpVol, false)
          sendCommand({ cmd: "set_speaker_volume", speaker_uuid: row.speaker.uuid, volume: nextSpVol })
          return
        }
      }
      if (activeGroup && (activeGroup.duration_sec || 0) > 0) {
        var dur = activeGroup.duration_sec || 0
        var nextSec = Math.max(0, Math.min(dur, livePositionSec + dx * seekStep))
        lastSeekMs = Date.now()
        livePositionSec = nextSec
        sendCommand({ cmd: "seek", coord_uuid: activeGroup.coord_uuid, seconds: nextSec })
      }
    }
  }

  function activateCursor() {
    if (!cursorActive || focusSection === "master") {
      if (activeGroup) sendCommand({ cmd: "play_pause", coord_uuid: activeGroup.coord_uuid })
      return
    }
    if (focusSection !== "drawer" || selectedIndex < 0) return

    if (activeTab === "speakers" && selectedIndex < speakersRows.length) {
      var row = speakersRows[selectedIndex]
      if (row.kind === "room") {
        selectedCoordUuid = row.group.coord_uuid
      } else if (row.kind === "speaker") {
        toggleSpeakerMembership(row.speaker, row.inGroup, row.canToggleGroup)
      }
    } else if (activeTab === "playlists" && selectedIndex < filteredLibrary.length) {
      playLibraryItem(filteredLibrary[selectedIndex], false)
    } else if (activeTab === "queue" && selectedIndex < filteredQueue.length) {
      playQueueTrack(filteredQueue[selectedIndex].index)
    }
  }

  function muteFocused() {
    if (cursorActive && focusSection === "drawer" && activeTab === "speakers"
        && selectedIndex >= 0 && selectedIndex < speakersRows.length) {
      var row = speakersRows[selectedIndex]
      if (row.kind === "speaker" && row.inGroup) {
        toggleSpeakerMute(row.speaker)
        return
      }
    }
    if (activeGroup) {
      sendCommand({
        cmd: "set_group_mute",
        coord_uuid: activeGroup.coord_uuid,
        muted: !activeGroup.group_muted
      })
    }
  }

  function queueNextSelected() {
    if (cursorActive && focusSection === "drawer" && activeTab === "playlists"
        && selectedIndex >= 0 && selectedIndex < filteredLibrary.length) {
      playLibraryItem(filteredLibrary[selectedIndex], true)
    }
  }

  // ---- Actions & RPC helpers ----

  function toggleSpeakerMembership(speaker, inGroup, canToggleGroup) {
    if (!canToggleGroup || !activeGroup || !speaker) return
    if (inGroup) {
      sendCommand({ cmd: "leave_group", speaker_uuid: speaker.uuid })
    } else {
      sendCommand({
        cmd: "join_group",
        speaker_uuid: speaker.uuid,
        target_coord_uuid: activeGroup.coord_uuid
      })
    }
  }

  function toggleSpeakerMute(speaker) {
    if (!speaker) return
    sendCommand({
      cmd: "set_speaker_mute",
      speaker_uuid: speaker.uuid,
      muted: !speaker.muted
    })
  }

  function playLibraryItem(item, asNext) {
    if (!activeGroup || !item) return
    sendCommand({
      cmd: "play_item",
      coord_uuid: activeGroup.coord_uuid,
      uri: item.uri,
      metadata: item.metadata,
      as_next: !!asNext
    })
  }

  function playQueueTrack(trackNum) {
    if (!activeGroup) return
    sendCommand({
      cmd: "play_queue_track",
      coord_uuid: activeGroup.coord_uuid,
      track_num: trackNum
    })
  }

  function toggleDrawerTab(tabName) {
    if (expanded && activeTab === tabName) {
      expanded = false
      focusSection = "master"
      selectedIndex = -1
    } else {
      activeTab = tabName
      expanded = true
      if (focusSection === "drawer") selectedIndex = 0
      clampCursor()
      resetActiveDrawerScroll()
    }
  }

  function resetActiveDrawerScroll() {
    if (!panel.loadedContent) return
    panel.loadedContent.resetDrawerScroll()
  }

  function focusActiveSearch() {
    if (!panel.loadedContent) return
    if (activeTab === "playlists" && panel.loadedContent.playlistSearchField) {
      if (!expanded) expanded = true
      panel.loadedContent.playlistSearchField.forceActiveFocus()
    } else if (activeTab === "queue" && panel.loadedContent.queueSearchField) {
      if (!expanded) expanded = true
      panel.loadedContent.queueSearchField.forceActiveFocus()
    }
  }

  function fetchState(forceTopology, opts) {
    var options = opts || ({})
    if (options.selectedCoordUuid === undefined && activeGroup)
      options.selectedCoordUuid = activeGroup.coord_uuid
    rpc.fetchState(forceTopology, groups.length === 0, options)
  }

  function sendCommand(payload) {
    if (payload && (payload.cmd === "previous" || payload.cmd === "next"
        || payload.cmd === "play_queue_track" || payload.cmd === "play_item")) {
      lastSeekMs = 0
    }
    rpc.sendCommand(payload)
  }

  function queueVolumeChange(payload) {
    rpc.queueVolumeChange(payload)
  }

  function fetchLibrary(force) {
    rpc.fetchLibrary(force)
  }

  function fetchQueue(force) {
    if (!activeGroup) return
    _queueFetchedCoord = activeGroup.coord_uuid
    rpc.fetchQueue(activeGroup.coord_uuid, queueLimit, !!force)
  }

  function refresh() {
    if (opened) fetchState(true)
  }

  onOpenedChanged: {
    if (opened) {
      cursorActive = false
      focusSection = "master"
      selectedIndex = -1
      _openedSyncDone = false
      _openedSyncPending = false
      resetPointerGate()
      // Persistent event stream pushes cached state in <1ms. Always give that push
      // a brief window before falling back so GENA opens need zero SOAP calls and
      // non-GENA opens run exactly one live 1-wave refresh (never racing subscribe).
      openSyncFallback.interval = (root.genaActive && root.groups.length > 0 && root._speakersReady) ? 150 : 40
      openSyncFallback.restart()
      if (expanded) {
        if (activeTab === "playlists") fetchLibrary(false)
        if (activeTab === "queue" && activeGroup) fetchQueue(false)
      }
      Qt.callLater(resetActiveDrawerScroll)
    } else {
      openSyncFallback.stop()
      _openedSyncDone = false
      _openedSyncPending = false
      _queueFetchedCoord = ""
      postCommandRefresh.stop()
      memberVolumeSettle.stop()
      _pendingRefreshCoord = ""
      _pendingRefreshFull = false
      _volumeDragCount = 0
      lastSeekMs = 0
      rpc.flushPendingVolume()
      hideMemberVolumes()
      groupVolumePreview = ({})
      memberVolumePreview = ({})
      expanded = false
      activeTab = "speakers"
    }
  }

  onExpandedChanged: {
    if (!opened) return
    if (!expanded) {
      focusSection = "master"
      selectedIndex = -1
      return
    }
    if (activeTab === "speakers" && !(root.genaActive && root.groups.length > 0 && root._speakersReady))
      fetchState(false, { includeSpeakers: true })
    if (activeTab === "playlists") fetchLibrary(false)
    if (activeTab === "queue" && activeGroup) fetchQueue(false)
    clampCursor()
  }

  onActiveTabChanged: {
    if (!opened || !expanded) return
    if (activeTab === "speakers" && !(root.genaActive && root.groups.length > 0 && root._speakersReady))
      fetchState(false, { includeSpeakers: true })
    if (activeTab === "playlists") fetchLibrary(false)
    if (activeTab === "queue" && activeGroup) fetchQueue(false)
    clampCursor()
    resetActiveDrawerScroll()
  }

  onGroupMemberKeyChanged: {
    memberVolumeBase = null
  }

  onActiveCoordUuidChanged: {
    hideMemberVolumes()
    groupVolumePreview = ({})
    memberVolumePreview = ({})
    queueCoordUuid = ""
    _queueFetchedCoord = ""
    queueItems = []
    lastSeekMs = 0
    if (activeGroup) {
      var pos = activeGroup.position_sec || 0
      var dur = activeGroup.duration_sec || 0
      if (activeGroup.playback_status === "Playing" && dur > 0 && _lastStateAtMs > 0) {
        var elapsed = Math.max(0, Math.floor((Date.now() - _lastStateAtMs) / 1000))
        pos = Math.min(dur, pos + elapsed)
      }
      livePositionSec = pos
      _lastTrackKey = activeGroup.coord_uuid + "|" + (activeGroup.track_num || 0) + "|" + (activeGroup.title || "")
    } else {
      livePositionSec = 0
      _lastTrackKey = ""
    }
    if (opened && expanded && activeTab === "queue" && activeCoordUuid !== "") fetchQueue(false)
  }

  onPlaylistQueryChanged: clampCursor()
  onQueueQueryChanged: clampCursor()

  SonosRpc {
    id: rpc
    subscribed: root.opened

    onEventStreamConnected: {
      if (!root.opened || root._openedSyncPending) return
      root._openedSyncDone = false
      openSyncFallback.interval = (root.genaActive && root.groups.length > 0 && root._speakersReady) ? 150 : 40
      openSyncFallback.restart()
    }

    onEventStreamDisconnected: {
      if (!root.opened) return
      root.genaActive = false
      root._openedSyncDone = false
      root._openedSyncPending = false
      openSyncFallback.interval = 250
      openSyncFallback.restart()
    }

    onStateFailed: {
      if (root.opened && root._openedSyncPending) {
        root._openedSyncPending = false
        root._openedSyncDone = false
      }
    }

    onStateReceived: function(rawJson) {
      var data = null
      try { data = JSON.parse(rawJson) } catch (e) { data = null }
      if (!data || !data.ok) {
        if (root.opened && root._openedSyncPending) {
          root._openedSyncPending = false
          root._openedSyncDone = false
        }
        return
      }
      root._openedSyncPending = false
      if (root.opened) root.genaActive = !!data.gena_active && rpc.eventConnected
      var speakersReady = (data.speakers_ready !== false) && Array.isArray(data.speakers) && data.speakers.length > 0
      root._speakersReady = speakersReady
      var hadGroups = root.groups.length > 0
      if (Array.isArray(data.groups)) {
        root.groups = data.groups
        if (data.groups.length === 0) {
          root.libraryItems = []
          root.queueItems = []
          root.queueCoordUuid = ""
          root._queueFetchedCoord = ""
        }
      }
      if (Array.isArray(data.speakers)) root.speakers = data.speakers
      var libVerChanged = false
      if (data.library_version !== undefined) {
        var nextLibVer = Number(data.library_version)
        libVerChanged = root._libraryVersion >= 0 && nextLibVer !== root._libraryVersion
        root._libraryVersion = nextLibVer
      }
      if (root.opened && root.expanded && root.activeTab === "playlists" && root.groups.length > 0
          && (libVerChanged || (!hadGroups && root.libraryItems.length === 0))) {
        root.fetchLibrary(false)
      }
      // Real values replace the estimate once no change is in flight.
      if (Date.now() - root.lastVolumeAdjustMs > 1000 && !memberVolumeSettle.running && root._volumeDragCount <= 0) {
        root.memberVolumeBase = null
        root.groupVolumePreview = ({})
        root.memberVolumePreview = ({})
      }
      if (root.activeGroup) {
        var trackKey = root.activeGroup.coord_uuid + "|" + (root.activeGroup.track_num || 0) + "|" + (root.activeGroup.title || "")
        var trackChanged = trackKey !== root._lastTrackKey
        root._lastTrackKey = trackKey
        if (trackChanged || (Date.now() - root.lastSeekMs > 1000))
          root.livePositionSec = root.activeGroup.position_sec || 0
      }
      if (root.opened && !root._openedSyncDone) {
        if (root.genaActive && Array.isArray(data.groups) && data.groups.length > 0 && speakersReady) {
          root._openedSyncDone = true
          openSyncFallback.stop()
        } else if (data.cached || (Array.isArray(data.groups) && data.groups.length > 0 && !speakersReady)) {
          root._openedSyncDone = true
          root._openedSyncPending = true
          openSyncFallback.stop()
          root.fetchState(false, { includeSpeakers: true })
        } else {
          root._openedSyncDone = true
          openSyncFallback.stop()
        }
      }
      if (!data.cached || root.genaActive) root._lastStateAtMs = Date.now()
      root.clampCursor()
      if (root.opened && root.expanded && root.activeTab === "queue" && root.activeGroup
          && !root.queueLoading
          && root.queueCoordUuid !== root.activeGroup.coord_uuid
          && root._queueFetchedCoord !== root.activeGroup.coord_uuid)
        root.fetchQueue(false)
    }

    onLibraryReceived: function(rawJson) {
      var data = null
      try { data = JSON.parse(rawJson) } catch (e) { return }
      if (!data || !data.ok || !Array.isArray(data.items)) return
      root.libraryItems = data.items
      root.clampCursor()
    }

    onQueueReceived: function(rawJson) {
      var data = null
      try { data = JSON.parse(rawJson) } catch (e) { return }
      if (!data || !data.ok || !Array.isArray(data.items)) return
      if (data.coord_uuid && root.activeGroup && data.coord_uuid !== root.activeGroup.coord_uuid) return
      root.queueItems = data.items
      root.queueCoordUuid = data.coord_uuid || (root.activeGroup ? root.activeGroup.coord_uuid : "")
      root.clampCursor()
    }

    onCommandCompleted: function(cmd, coordUuid) {
      if (!root.opened) return
      if (cmd === "join_group" || cmd === "leave_group") {
        root._pendingRefreshFull = true
        root._pendingRefreshCoord = ""
        root._pendingRefreshIncludeSpeakers = true
        postCommandRefresh.interval = 220
        postCommandRefresh.restart()
        return
      }
      // When GENA push is active, Sonos pushes confirmed updates automatically except
      // same-track restart (previous / play_queue_track) where RelTime is not evented.
      var needsPosRefresh = (cmd === "previous" || cmd === "play_queue_track")
      if (root.genaActive && !needsPosRefresh) return
      if (root._pendingRefreshFull) {
        root._pendingRefreshIncludeSpeakers = true
        return
      }
      var target = coordUuid || (root.activeGroup ? root.activeGroup.coord_uuid : "")
      var incSp = root.genaActive ? false : root.needsMemberAudio
      if (postCommandRefresh.running) {
        if (root._pendingRefreshCoord !== target) root._pendingRefreshCoord = ""
        root._pendingRefreshIncludeSpeakers = root._pendingRefreshIncludeSpeakers || incSp
      } else {
        root._pendingRefreshCoord = target
        root._pendingRefreshIncludeSpeakers = incSp
      }
      postCommandRefresh.interval = 100
      postCommandRefresh.restart()
    }
  }

  // On-demand RPC polling ONLY while the panel is open (slows to a 15s safety
  // heartbeat when GENA push events are active; zero idle work when closed).
  Timer {
    id: openPollTimer
    interval: root.genaActive ? 15000 : (root.pollIntervalSec * 1000)
    running: root.opened
    repeat: true
    onTriggered: {
      if (Date.now() - root._lastStateAtMs >= (interval - 300))
        root.fetchState(false, { includeSpeakers: root.needsMemberAudio })
    }
  }

  // Hide the room volumes a few seconds after the last change, unless the pointer is on them.
  Timer {
    id: memberVolumeHide
    interval: 3000
    repeat: false
    onTriggered: {
      if (root.masterVolumeHovered || root.memberVolumesHovered || root._volumeDragCount > 0) restart()
      else root.hideMemberVolumes()
    }
  }

  // Fallback live refresh on open if the persistent event stream did not deliver
  // an active GENA state push promptly (e.g., daemon restarting or empty cache).
  Timer {
    id: openSyncFallback
    interval: 50
    repeat: false
    onTriggered: {
      if (root.opened && !root._openedSyncDone) {
        root._openedSyncDone = true
        root._openedSyncPending = true
        root.fetchState(false, { includeSpeakers: true })
      }
    }
  }

  // Fetch the real room volumes once the speakers have settled after a change.
  Timer {
    id: memberVolumeSettle
    interval: 1200
    repeat: false
    onTriggered: {
      if (!root.opened) return
      if (root._volumeDragCount > 0) {
        restart()
        return
      }
      if (root.genaActive) {
        root.memberVolumeBase = null
        root.groupVolumePreview = ({})
        root.memberVolumePreview = ({})
        return
      }
      if (root.activeGroup)
        root.fetchState(false, { coordUuid: root.activeGroup.coord_uuid, includeSpeakers: true })
      else
        root.fetchState(false, { includeSpeakers: true })
    }
  }

  Timer {
    id: postCommandRefresh
    interval: 100
    repeat: false
    onTriggered: {
      if (!root.opened) return
      var full = root._pendingRefreshFull
      var target = root._pendingRefreshCoord
      var incSp = root._pendingRefreshIncludeSpeakers
      root._pendingRefreshFull = false
      root._pendingRefreshCoord = ""
      root._pendingRefreshIncludeSpeakers = true
      if (full) {
        root.fetchState(true, { includeSpeakers: true })
      } else if (target !== "") {
        root.fetchState(false, { coordUuid: target, includeSpeakers: incSp })
      } else {
        root.fetchState(false, { includeSpeakers: incSp })
      }
    }
  }

  Timer {
    interval: 1000
    running: root.opened && root.isPlaying && root.activeGroup !== null && (root.activeGroup.duration_sec || 0) > 0
    repeat: true
    onTriggered: {
      var dur = root.activeGroup ? (root.activeGroup.duration_sec || 0) : 0
      if (root.livePositionSec < dur) root.livePositionSec += 1
    }
  }

  // Dynamic notch-aware bar width (uses Bar.qml's availableWidth budget when provided).
  property real availableWidth: -1
  readonly property real chromeWidth: Math.max(Style.bar.statusSlot, barGlyph.implicitWidth + Style.space(14))
  readonly property real minLabelWidth: Style.space(48)
  readonly property real desiredLabelWidth: showTrackInBar && hasBarTrack && !vertical
    ? Math.min(Style.space(maxBarLabelWidth), barLabelText.implicitWidth)
    : 0
  readonly property real effectiveLabelWidth: {
    if (desiredLabelWidth <= 0) return 0
    if (availableWidth < 0) return desiredLabelWidth
    var room = availableWidth - chromeWidth - barRow.spacing
    return room >= minLabelWidth ? Math.min(desiredLabelWidth, Math.floor(room)) : 0
  }

  property real preferredWidth: desiredLabelWidth > 0 ? chromeWidth + barRow.spacing + desiredLabelWidth : chromeWidth
  property real minimumWidth: chromeWidth

  implicitWidth: barTrigger.implicitWidth
  implicitHeight: barTrigger.implicitHeight

  // -- Bar trigger: left-click opens KeyboardPanel, right-click toggles play/pause --
  Item {
    id: barTrigger

    property var registeredBar: null
    property int heldButtons: Qt.NoButton

    function triggerPress(button) {
      if (root.bar) root.bar.hideTooltip(barTrigger)
      if (button === Qt.RightButton) {
        if (root.opened && root.activeGroup) {
          root.sendCommand({ cmd: "play_pause", coord_uuid: root.activeGroup.coord_uuid })
        } else if (root.activeMprisPlayer && root.activeMprisPlayer.canTogglePlaying) {
          root.activeMprisPlayer.togglePlaying()
        } else if (root.activeGroup) {
          root.sendCommand({ cmd: "play_pause", coord_uuid: root.activeGroup.coord_uuid })
        }
      } else {
        root.toggle()
      }
    }

    function syncClickRegistration() {
      if (registeredBar && registeredBar.unregisterClickTarget) registeredBar.unregisterClickTarget(barTrigger)
      registeredBar = root.bar
      if (registeredBar && registeredBar.registerClickTarget) registeredBar.registerClickTarget(barTrigger)
    }

    anchors.fill: parent
    implicitWidth: root.effectiveLabelWidth > 0
      ? barRow.implicitWidth + Style.space(14)
      : Style.bar.statusSlot
    implicitHeight: root.barSize

    Connections {
      target: root
      function onBarChanged() { barTrigger.syncClickRegistration() }
    }
    Component.onCompleted: syncClickRegistration()
    Component.onDestruction: if (registeredBar && registeredBar.unregisterClickTarget) registeredBar.unregisterClickTarget(barTrigger)

    Row {
      id: barRow
      anchors.centerIn: parent
      spacing: Style.space(6)

      Text {
        id: barGlyph
        textFormat: Text.PlainText
        anchors.verticalCenter: parent.verticalCenter
        text: root.anyPlaying ? "󰓃" : "󰓄"
        color: root.anyPlaying ? root.barForeground : Qt.darker(root.barForeground, 1.4)
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        Behavior on color {
          enabled: !root.bar || root.bar.foregroundAnimationEnabled
          ColorAnimation { duration: Style.duration(160) }
        }
      }

      Item {
        id: scrollClip
        visible: root.effectiveLabelWidth > 0
        width: root.effectiveLabelWidth
        height: barGlyph.height
        clip: true
        anchors.verticalCenter: parent.verticalCenter

        Text {
          id: barLabelText
          textFormat: Text.PlainText
          anchors.verticalCenter: parent.verticalCenter
          text: root.barTitle + (root.showArtistInBar && root.barArtist ? (root.barTitle ? "  ·  " : "") + root.barArtist : "")
          color: root.isPlaying ? root.barForeground : Qt.darker(root.barForeground, 1.3)
          font.family: root.fontFamily
          font.pixelSize: Style.font.body

          readonly property bool needsScroll: root.effectiveLabelWidth > 0 && implicitWidth > scrollClip.width

          NumberAnimation on x {
            running: root.scrollTrackInBar && barLabelText.needsScroll && root.isPlaying && root.visible && !root.barHidden && !root.opened && !root.vertical && !Style.reduceMotion
            onRunningChanged: if (!running) barLabelText.x = 0
            loops: Animation.Infinite
            duration: Style.duration(Math.max(6000, barLabelText.implicitWidth * 25))
            from: scrollClip.width
            to: -barLabelText.implicitWidth
            easing.type: Easing.Linear
          }
        }
      }
    }

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      acceptedButtons: Qt.LeftButton | Qt.RightButton

      onClicked: function(mouse) { barTrigger.triggerPress(mouse.button) }
      onWheel: function(wheel) {
        var res = Util.wheelSteps(root.barWheelAccumulator, wheel.angleDelta.y)
        root.barWheelAccumulator = res.remainder
        if (res.steps === 0) return
        var step = res.steps * root.volumeStep
        if (root.opened && root.activeGroup) {
          var nextVol = Math.max(0, Math.min(100, root.groupVolume(root.activeGroup) + step))
          root.previewGroupVolume(nextVol)
          root.queueVolumeChange({ cmd: "set_group_volume", coord_uuid: root.activeGroup.coord_uuid, volume: nextVol })
        } else if (root.activeMprisPlayer && root.activeMprisPlayer.volumeSupported) {
          var cur = Number(root.activeMprisPlayer.volume) || 0
          root.activeMprisPlayer.volume = Math.max(0, Math.min(1, cur + step / 100))
        }
      }
      onEntered: if (root.bar) root.bar.showTooltip(
        barTrigger,
        Model.sanitizeTooltip(
          root.hasBarTrack
            ? (root.barTitle + (root.barArtist ? " — " + root.barArtist : ""))
            : sonosStrings.t("sonos.tooltip.idle")
        )
      )
      onExited: if (root.bar) root.bar.hideTooltip(barTrigger)
    }
  }

  // -- Native Omarchy KeyboardPanel (lazy-loaded on first open) --
  KeyboardPanel {
    id: panel
    anchorItem: barTrigger
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(root.expanded ? Style.space(396) : Style.space(360))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight, Style.space(620))

    content: PanelKeyCatcher {
      id: panelKeyCatcher
      property alias playlistSearchField: playlistsDrawer.searchField
      property alias queueSearchField: queueDrawer.searchField
      property alias pointerGate: panelPointerGate
      readonly property bool searchFocused: playlistSearchField.activeFocus || queueSearchField.activeFocus

      onSearchFocusedChanged: {
        if (!searchFocused && root.opened) {
          Qt.callLater(function() {
            if (root.opened && !panelKeyCatcher.searchFocused)
              panelKeyCatcher.forceActiveFocus()
          })
        }
      }

      readonly property bool drawerVisible: root.expanded && root.groups.length > 0
      readonly property real activeDrawerHeight: {
        if (!drawerVisible) return 0
        if (root.activeTab === "speakers") return speakersDrawer.desiredHeight
        if (root.activeTab === "playlists") return playlistsDrawer.desiredHeight
        if (root.activeTab === "queue") return queueDrawer.desiredHeight
        return 0
      }

      implicitHeight: headerCol.implicitHeight + (drawerVisible ? (Style.space(18) + drawerSep.implicitHeight + activeDrawerHeight) : 0)
      anchors.fill: parent
      blocked: searchFocused

      function resetDrawerScroll() {
        speakersDrawer.resetScroll()
        playlistsDrawer.resetScroll()
        queueDrawer.resetScroll()
      }

      PointerMoveGate {
        id: panelPointerGate
        referenceItem: panelKeyCatcher
      }

      Component.onCompleted: {
        if (root.opened) Qt.callLater(function() {
          if (root.opened) panelKeyCatcher.forceActiveFocus()
        })
      }

      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onActivateRequested: root.activateCursor()
      onMoveRequested: function(dx, dy) { root.moveCursor(dx, dy) }
      onTextKey: function(t) {
        if (t === "n" || t === "N") {
          if (root.activeGroup) root.sendCommand({ cmd: "next", coord_uuid: root.activeGroup.coord_uuid })
        } else if (t === "p" || t === "P") {
          if (root.activeGroup) root.sendCommand({ cmd: "previous", coord_uuid: root.activeGroup.coord_uuid })
        } else if (t === "m" || t === "M") {
          root.muteFocused()
        } else if (t === "a" || t === "A") {
          root.queueNextSelected()
        } else if (t === "1") {
          root.toggleDrawerTab("speakers")
        } else if (t === "2") {
          root.toggleDrawerTab("playlists")
        } else if (t === "3") {
          root.toggleDrawerTab("queue")
        } else if (t === "/") {
          root.focusActiveSearch()
        } else if (t === "r" || t === "R") {
          root.fetchState(true)
        }
      }

      ColumnLayout {
        anchors.fill: parent
        spacing: Style.space(9)

        // =================================================================
        // PINNED HEADER: Now Playing Card + Drawer Tabs (never scrolls away)
        // =================================================================
        Column {
          id: headerCol
          Layout.fillWidth: true
          spacing: Style.space(9)

          // -- Loading / Empty state --
          Text {
            visible: root.groups.length === 0
            width: parent.width
            textFormat: Text.PlainText
            text: root.stateLoading ? sonosStrings.t("sonos.loading") : sonosStrings.t("sonos.noSpeakers")
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
            padding: Style.space(16)
          }

          Column {
            visible: root.activeGroup !== null
            width: parent.width
            spacing: Style.space(9)

            // Top strip: Audio Source (left) + Output routing pill (right)
            RowLayout {
              width: parent.width
              spacing: Style.space(8)

              RowLayout {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.alignment: Qt.AlignVCenter
                spacing: Style.space(5)

                Text {
                  Layout.alignment: Qt.AlignVCenter
                  textFormat: Text.PlainText
                  text: root.activeSourceIcon
                  color: root.isPlaying ? root.accent : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }

                Text {
                  Layout.fillWidth: true
                  Layout.minimumWidth: 0
                  Layout.alignment: Qt.AlignVCenter
                  textFormat: Text.PlainText
                  text: root.activeSourceName
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                  elide: Text.ElideRight
                }
              }

              BorderSurface {
                Layout.alignment: Qt.AlignVCenter
                Layout.maximumWidth: Math.round(headerCol.width * 0.55)
                implicitWidth: Math.min(Layout.maximumWidth, outputChipRow.implicitWidth + Style.space(12))
                implicitHeight: outputChipRow.implicitHeight + Style.space(4)
                radius: Style.spacing.labelGap
                color: (root.expanded && root.activeTab === "speakers")
                  ? Style.selectedFillFor(root.foreground, root.accent)
                  : (outChipMouse.containsMouse ? Style.hoverFillFor(root.foreground, root.accent) : Style.normalFillFor(root.foreground, root.accent))
                borderSpec: Border.controlSpec((root.expanded && root.activeTab === "speakers") ? "focus" : "normal", root.foreground, root.accent)

                RowLayout {
                  id: outputChipRow
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  anchors.leftMargin: Style.space(6)
                  anchors.rightMargin: Style.space(6)
                  spacing: Style.space(5)

                  Text {
                    Layout.alignment: Qt.AlignVCenter
                    textFormat: Text.PlainText
                    text: "󰐰"
                    color: root.isPlaying ? root.accent : root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Text {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    Layout.alignment: Qt.AlignVCenter
                    textFormat: Text.PlainText
                    text: root.activeGroup ? root.activeGroup.room_label : ""
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                    elide: Text.ElideRight
                  }
                }

                MouseArea {
                  id: outChipMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.toggleDrawerTab("speakers")
                }
              }
            }

            // Artwork (with corner source badge) + Track Metadata
            RowLayout {
              width: parent.width
              spacing: Style.space(12)

              Item {
                Layout.preferredWidth: Style.space(64)
                Layout.preferredHeight: Style.space(64)
                Layout.alignment: Qt.AlignVCenter

                BorderSurface {
                  anchors.fill: parent
                  radius: Style.spacing.labelGap
                  color: Style.selectedFillFor(root.foreground, root.accent)
                  borderSpec: Border.controlSpec(root.isPlaying ? "focus" : "normal", root.foreground, root.accent)

                  SizedImage {
                    anchors.fill: parent
                    anchors.margins: Style.space(2)
                    crop: true
                    url: root.activeGroup && root.activeGroup.art_url ? root.activeGroup.art_url : ""
                    visible: url !== ""
                  }

                  Text {
                    anchors.centerIn: parent
                    visible: !root.activeGroup || !root.activeGroup.art_url
                    textFormat: Text.PlainText
                    text: "󰝚"
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.displayLarge
                  }
                }

                BorderSurface {
                  width: Style.space(20)
                  height: Style.space(20)
                  radius: width / 2
                  anchors.right: parent.right
                  anchors.bottom: parent.bottom
                  anchors.rightMargin: -Style.space(3)
                  anchors.bottomMargin: -Style.space(3)
                  color: Commons.Color.popups.background
                  borderSpec: Border.controlSpec(root.isPlaying ? "focus" : "normal", root.foreground, root.accent)

                  Text {
                    anchors.centerIn: parent
                    textFormat: Text.PlainText
                    text: root.activeSourceIcon
                    color: root.isPlaying ? root.accent : root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
              }

              Column {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.alignment: Qt.AlignVCenter
                spacing: Style.space(2)

                Text {
                  width: parent.width
                  textFormat: Text.PlainText
                  text: root.activeGroup && root.activeGroup.title ? root.activeGroup.title : sonosStrings.t("sonos.nothingPlaying")
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.subtitle
                  font.bold: true
                  elide: Text.ElideRight
                }

                Text {
                  width: parent.width
                  visible: text !== ""
                  textFormat: Text.PlainText
                  text: root.activeGroup && root.activeGroup.artist ? root.activeGroup.artist : ""
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                }

                RowLayout {
                  width: parent.width
                  spacing: Style.space(5)

                  Text {
                    Layout.alignment: Qt.AlignVCenter
                    textFormat: Text.PlainText
                    text: "󰓃"
                    color: root.isPlaying ? root.accent : root.dimmer
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Text {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    Layout.alignment: Qt.AlignVCenter
                    textFormat: Text.PlainText
                    text: root.activeSpeakersList
                    color: root.dimmer
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }
                }
              }
            }

            // Progress scrubber
            RowLayout {
              visible: root.activeGroup !== null && (root.activeGroup.duration_sec || 0) > 0
              width: parent.width
              spacing: Style.space(8)

              Text {
                Layout.alignment: Qt.AlignVCenter
                Layout.preferredWidth: Style.space(38)
                textFormat: Text.PlainText
                text: root.formatDuration(seekSlider.dragging ? seekSlider.liveValue : root.livePositionSec)
                color: root.dimmer
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              SonosSlider {
                id: seekSlider
                bar: root.bar
                Layout.fillWidth: true
                Layout.minimumWidth: Style.space(60)
                Layout.alignment: Qt.AlignVCenter
                minimum: 0
                maximum: Math.max(1, root.activeGroup ? (root.activeGroup.duration_sec || 1) : 1)
                step: root.seekStep
                integer: true
                value: root.livePositionSec
                knobSize: Style.space(10)
                trackHeight: Style.space(3)
                fillColor: root.accent
                onReleased: function(v) {
                  if (!root.activeGroup) return
                  root.lastSeekMs = Date.now()
                  root.livePositionSec = Math.round(v)
                  root.sendCommand({ cmd: "seek", coord_uuid: root.activeGroup.coord_uuid, seconds: Math.round(v) })
                }
              }

              Text {
                Layout.alignment: Qt.AlignVCenter
                Layout.preferredWidth: Style.space(38)
                textFormat: Text.PlainText
                text: root.formatDuration(root.activeGroup ? (root.activeGroup.duration_sec || 0) : 0)
                color: root.dimmer
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                horizontalAlignment: Text.AlignRight
              }
            }

            // Transport controls
            Row {
              anchors.horizontalCenter: parent.horizontalCenter
              spacing: Style.space(8)
              LayoutMirroring.enabled: false
              LayoutMirroring.childrenInherit: true

              Button {
                visible: root.expanded
                iconText: "󰒟"
                bordered: true
                selected: !!(root.activeGroup && root.activeGroup.shuffle)
                foreground: root.foreground
                accent: root.accent
                horizontalPadding: Style.space(8)
                verticalPadding: Style.space(5)
                onClicked: {
                  if (!root.activeGroup) return
                  root.sendCommand({
                    cmd: "set_play_mode",
                    coord_uuid: root.activeGroup.coord_uuid,
                    shuffle: !root.activeGroup.shuffle,
                    repeat: root.activeGroup.repeat || "none"
                  })
                }
              }

              Button {
                iconText: "󰒮"
                bordered: true
                foreground: root.foreground
                accent: root.accent
                enabled: !!(root.activeGroup && root.activeGroup.can_prev)
                opacity: enabled ? 1.0 : 0.4
                horizontalPadding: Style.space(10)
                verticalPadding: Style.space(5)
                onClicked: if (root.activeGroup) root.sendCommand({ cmd: "previous", coord_uuid: root.activeGroup.coord_uuid })
              }

              Button {
                iconText: root.isPlaying ? "󰏤" : "󰐊"
                bordered: true
                selected: root.isPlaying
                foreground: root.foreground
                accent: root.accent
                iconSize: Style.font.iconLarge
                horizontalPadding: Style.space(16)
                verticalPadding: Style.space(5)
                onClicked: if (root.activeGroup) root.sendCommand({ cmd: "play_pause", coord_uuid: root.activeGroup.coord_uuid })
              }

              Button {
                iconText: "󰒭"
                bordered: true
                foreground: root.foreground
                accent: root.accent
                enabled: !!(root.activeGroup && root.activeGroup.can_next)
                opacity: enabled ? 1.0 : 0.4
                horizontalPadding: Style.space(10)
                verticalPadding: Style.space(5)
                onClicked: if (root.activeGroup) root.sendCommand({ cmd: "next", coord_uuid: root.activeGroup.coord_uuid })
              }

              Button {
                visible: root.expanded
                iconText: root.activeGroup && root.activeGroup.repeat === "one" ? "󰑘" : "󰑖"
                bordered: true
                selected: !!(root.activeGroup && root.activeGroup.repeat && root.activeGroup.repeat !== "none")
                foreground: root.foreground
                accent: root.accent
                horizontalPadding: Style.space(8)
                verticalPadding: Style.space(5)
                onClicked: {
                  if (!root.activeGroup) return
                  var cur = root.activeGroup.repeat || "none"
                  var nextRep = cur === "none" ? "all" : (cur === "all" ? "one" : "none")
                  root.sendCommand({
                    cmd: "set_play_mode",
                    coord_uuid: root.activeGroup.coord_uuid,
                    shuffle: !!root.activeGroup.shuffle,
                    repeat: nextRep
                  })
                }
              }
            }

            // Master / Group Volume Slider (CursorSurface for unified keyboard/mouse highlight)
            CursorSurface {
              id: masterVolRow
              width: parent.width
              implicitHeight: masterVolInner.implicitHeight + Style.space(4)
              radius: Style.spacing.labelGap
              hasCursor: root.cursorActive && root.focusSection === "master"
              foreground: root.foreground
              accent: root.accent

              RowLayout {
                id: masterVolInner
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Style.space(2)
                anchors.rightMargin: Style.space(4)
                spacing: Style.space(8)

                Button {
                  Layout.alignment: Qt.AlignVCenter
                  iconText: Model.volumeIcon(root.groupVolume(root.activeGroup), root.activeGroup && root.activeGroup.group_muted)
                  bordered: false
                  foreground: root.activeGroup && root.activeGroup.group_muted ? root.dimmer : root.foreground
                  horizontalPadding: Style.space(4)
                  verticalPadding: Style.space(2)
                  onClicked: {
                    if (!root.activeGroup) return
                    root.revealMemberVolumes()
                    root.sendCommand({
                      cmd: "set_group_mute",
                      coord_uuid: root.activeGroup.coord_uuid,
                      muted: !root.activeGroup.group_muted
                    })
                  }
                }

                SonosSlider {
                  id: masterVolSlider
                  property bool _countedDrag: false
                  bar: root.bar
                  Layout.fillWidth: true
                  Layout.minimumWidth: Style.space(60)
                  Layout.alignment: Qt.AlignVCenter
                  minimum: 0
                  maximum: 100
                  step: root.volumeStep
                  integer: true
                  value: root.groupVolume(root.activeGroup)
                  opacity: root.activeGroup && root.activeGroup.group_muted ? 0.5 : 1.0
                  onDraggingChanged: {
                    if (dragging !== _countedDrag) {
                      _countedDrag = dragging
                      root.setVolumeDragging(dragging)
                    }
                    if (dragging) root.revealMemberVolumes()
                  }
                  Component.onDestruction: {
                    if (_countedDrag) {
                      _countedDrag = false
                      root.setVolumeDragging(false)
                    }
                  }
                  onMoved: function(v) {
                    if (!root.activeGroup) return
                    root.previewGroupVolume(v)
                    root.queueVolumeChange({ cmd: "set_group_volume", coord_uuid: root.activeGroup.coord_uuid, volume: Math.round(v) })
                  }
                  onReleased: function(v) {
                    if (!root.activeGroup) return
                    root.previewGroupVolume(v)
                    root.sendCommand({ cmd: "set_group_volume", coord_uuid: root.activeGroup.coord_uuid, volume: Math.round(v) })
                  }
                  onRightClicked: {
                    if (!root.activeGroup) return
                    root.sendCommand({ cmd: "set_group_mute", coord_uuid: root.activeGroup.coord_uuid, muted: !root.activeGroup.group_muted })
                  }
                }

                Text {
                  Layout.alignment: Qt.AlignVCenter
                  Layout.preferredWidth: Style.space(34)
                  textFormat: Text.PlainText
                  text: root.formatPercent(masterVolSlider.liveValue)
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  horizontalAlignment: Text.AlignRight
                }
              }

              HoverHandler {
                onHoveredChanged: root.masterVolumeHovered = hovered
                onPointChanged: {
                  if (hovered && panelPointerGate.moved(masterVolRow, point.position))
                    root.setMasterCursor()
                }
              }
            }

            // Grouped rooms' volumes, revealed while the group volume is being set.
            Column {
              id: memberVolumes
              visible: root.memberVolumesShown && root.isMultiRoomGroup
              width: parent.width
              leftPadding: Style.space(10)
              spacing: Style.space(2)

              HoverHandler {
                onHoveredChanged: root.memberVolumesHovered = hovered
              }

              Repeater {
                model: memberVolumes.visible ? root.groupMemberUuids : []

                RowLayout {
                  id: memberRow
                  required property var modelData
                  readonly property var speaker: root.speakerByUuid(modelData) || ({ uuid: modelData, name: "", volume: 0, muted: false })
                  width: memberVolumes.width - memberVolumes.leftPadding
                  spacing: Style.space(6)

                  Text {
                    Layout.alignment: Qt.AlignVCenter
                    Layout.preferredWidth: Style.space(18)
                    textFormat: Text.PlainText
                    text: Model.volumeIcon(root.memberVolume(memberRow.speaker), memberRow.speaker.muted)
                    color: memberRow.speaker.muted ? root.dimmer : root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    horizontalAlignment: Text.AlignHCenter

                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: {
                        root.revealMemberVolumes()
                        root.toggleSpeakerMute(memberRow.speaker)
                      }
                    }
                  }

                  Text {
                    Layout.alignment: Qt.AlignVCenter
                    Layout.preferredWidth: Style.space(92)
                    textFormat: Text.PlainText
                    text: memberRow.speaker.name
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }

                  SonosSlider {
                    id: memberSlider
                    property bool _countedDrag: false
                    bar: root.bar
                    Layout.fillWidth: true
                    Layout.minimumWidth: Style.space(60)
                    Layout.alignment: Qt.AlignVCenter
                    minimum: 0
                    maximum: 100
                    step: root.volumeStep
                    integer: true
                    knobSize: Style.space(11)
                    trackHeight: Style.space(3)
                    value: root.memberVolume(memberRow.speaker)
                    opacity: memberRow.speaker.muted ? 0.5 : 1.0
                    onDraggingChanged: {
                      if (dragging !== _countedDrag) {
                        _countedDrag = dragging
                        root.setVolumeDragging(dragging)
                      }
                      if (dragging) root.revealMemberVolumes()
                    }
                    Component.onDestruction: {
                      if (_countedDrag) {
                        _countedDrag = false
                        root.setVolumeDragging(false)
                      }
                    }
                    onMoved: function(v) {
                      root.previewMemberVolume(memberRow.speaker, v)
                      root.queueVolumeChange({ cmd: "set_speaker_volume", speaker_uuid: memberRow.speaker.uuid, volume: Math.round(v) })
                    }
                    onReleased: function(v) {
                      root.previewMemberVolume(memberRow.speaker, v)
                      root.sendCommand({ cmd: "set_speaker_volume", speaker_uuid: memberRow.speaker.uuid, volume: Math.round(v) })
                    }
                    onRightClicked: root.toggleSpeakerMute(memberRow.speaker)
                  }

                  Text {
                    Layout.alignment: Qt.AlignVCenter
                    Layout.preferredWidth: Style.space(34)
                    textFormat: Text.PlainText
                    text: root.formatPercent(memberSlider.liveValue)
                    color: root.dimmer
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    horizontalAlignment: Text.AlignRight
                  }
                }
              }
            }
          }

          PanelSeparator {
            visible: root.groups.length > 0
            width: parent.width
            foreground: root.foreground
          }

          // -- Drawer tabs (click a tab to open its drawer, click again to close) --
          RowLayout {
            visible: root.groups.length > 0
            width: parent.width
            spacing: Style.space(5)

            Button {
              visible: root.groups.length > 0
              Layout.alignment: Qt.AlignVCenter
              iconText: "󰓃"
              text: sonosStrings.t("sonos.tab.speakers")
              bordered: true
              selected: root.expanded && root.activeTab === "speakers"
              foreground: root.foreground
              accent: root.accent
              fontSize: Style.font.caption
              horizontalPadding: Style.space(7)
              verticalPadding: Style.space(4)
              onClicked: root.toggleDrawerTab("speakers")
            }

            Button {
              visible: root.groups.length > 0
              Layout.alignment: Qt.AlignVCenter
              iconText: "󰲸"
              text: sonosStrings.t("sonos.tab.playlists")
              bordered: true
              selected: root.expanded && root.activeTab === "playlists"
              foreground: root.foreground
              accent: root.accent
              fontSize: Style.font.caption
              horizontalPadding: Style.space(7)
              verticalPadding: Style.space(4)
              onClicked: root.toggleDrawerTab("playlists")
            }

            Button {
              visible: root.groups.length > 0
              Layout.alignment: Qt.AlignVCenter
              iconText: "󰐑"
              text: sonosStrings.t("sonos.tab.queue")
              bordered: true
              selected: root.expanded && root.activeTab === "queue"
              foreground: root.foreground
              accent: root.accent
              fontSize: Style.font.caption
              horizontalPadding: Style.space(7)
              verticalPadding: Style.space(4)
              onClicked: root.toggleDrawerTab("queue")
            }
          }
        }

        // =================================================================
        // SCROLLABLE DRAWER AREA (only active drawer scrolls; header stays pinned)
        // =================================================================
        PanelSeparator {
          id: drawerSep
          visible: panelKeyCatcher.drawerVisible
          Layout.fillWidth: true
          foreground: root.foreground
        }

        Item {
          visible: panelKeyCatcher.drawerVisible
          Layout.fillWidth: true
          Layout.fillHeight: true
          Layout.minimumHeight: 0

          SpeakersDrawer {
            id: speakersDrawer
            anchors.fill: parent
            visible: root.groups.length > 0 && root.expanded && root.activeTab === "speakers"
            controller: root
            strings: sonosStrings
            pointerGate: panelPointerGate
          }

          PlaylistsDrawer {
            id: playlistsDrawer
            anchors.fill: parent
            visible: root.groups.length > 0 && root.expanded && root.activeTab === "playlists"
            controller: root
            strings: sonosStrings
            pointerGate: panelPointerGate
            keyCatcher: panelKeyCatcher
          }

          QueueDrawer {
            id: queueDrawer
            anchors.fill: parent
            visible: root.groups.length > 0 && root.expanded && root.activeTab === "queue"
            controller: root
            strings: sonosStrings
            pointerGate: panelPointerGate
            keyCatcher: panelKeyCatcher
          }
        }
      }
    }
  }
}
