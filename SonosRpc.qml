import QtQuick
import Quickshell
import Quickshell.Io

// Direct Unix-socket JSON-line RPC client for sonos-mpris.py ($XDG_RUNTIME_DIR/mc-sonos.sock).
// Supports one-shot RPC commands plus a persistent event-push stream while the panel is open.
Item {
  id: root
  visible: false

  readonly property string socketPath: {
    var runtime = Quickshell.env("XDG_RUNTIME_DIR") || ""
    return runtime !== "" ? runtime + "/mc-sonos.sock" : ""
  }

  // Subscribe to daemon state pushes (cached initial paint + live GENA/poll updates)
  // only while the panel is open (zero idle sockets when closed).
  property bool subscribed: false

  property bool stateLoading: false
  property bool libraryLoading: false
  property bool queueLoading: false
  readonly property bool eventConnected: eventSock.connected

  // Emit raw JSON strings so listeners parse once into native JS objects/arrays
  // without crossing a `var` signal boundary into Qt sequences.
  signal stateReceived(string rawJson)
  signal stateFailed()
  signal libraryReceived(string rawJson)
  signal queueReceived(string rawJson)
  signal commandCompleted(string cmd, string coordUuid)
  signal eventStreamConnected()
  signal eventStreamDisconnected()

  property var _cmdQueue: []
  property var _pendingVolumes: ({})
  property var _pendingStateFetch: null
  property var _pendingLibraryFetch: null
  property var _pendingQueueFetch: null
  property var _activeCmd: null

  function _volumeKey(payload) {
    if (!payload) return ""
    if (payload.cmd === "set_group_volume") return "g:" + (payload.coord_uuid || "")
    if (payload.cmd === "set_speaker_volume") return "s:" + (payload.speaker_uuid || "")
    return ""
  }

  function _hasPendingVolumes() {
    for (var k in _pendingVolumes) return true
    return false
  }

  function fetchState(forceTopology, showSpinner, opts) {
    if (!socketPath) return
    opts = opts || ({})
    var reqForce = !!forceTopology
    var reqSpinner = !!showSpinner
    var reqCoord = opts.coordUuid ? String(opts.coordUuid) : ""
    var reqIncludeSpeakers = opts.includeSpeakers !== undefined ? !!opts.includeSpeakers : true
    var reqSelectedCoord = opts.selectedCoordUuid ? String(opts.selectedCoordUuid) : ""
    var reqCached = !!opts.cached

    if (stateChan.busy) {
      var prev = _pendingStateFetch
      if (!prev) {
        _pendingStateFetch = {
          forceTopology: reqForce,
          showSpinner: reqSpinner,
          coordUuid: reqCoord,
          includeSpeakers: reqIncludeSpeakers,
          selectedCoordUuid: reqSelectedCoord,
          cached: reqCached
        }
      } else {
        // If either queued fetch needs a full snapshot, upgrade targeted to full.
        var mergedCoord = (prev.coordUuid !== "" && reqCoord !== "" && prev.coordUuid === reqCoord)
          ? reqCoord
          : ""
        _pendingStateFetch = {
          forceTopology: prev.forceTopology || reqForce,
          showSpinner: prev.showSpinner || reqSpinner,
          coordUuid: mergedCoord,
          includeSpeakers: prev.includeSpeakers || reqIncludeSpeakers,
          selectedCoordUuid: reqSelectedCoord || prev.selectedCoordUuid,
          cached: prev.cached && reqCached
        }
      }
      return
    }
    if (reqSpinner) stateLoading = true
    var payload = {
      cmd: "get_state",
      force_topology: reqForce,
      include_speakers: reqIncludeSpeakers
    }
    if (reqCoord !== "") payload.coord_uuid = reqCoord
    if (reqSelectedCoord !== "") payload.selected_coord_uuid = reqSelectedCoord
    if (reqCached) payload.cached = true
    if (!stateChan.send(payload)) {
      stateLoading = false
    }
  }

  function _drainPendingStateFetch() {
    if (!subscribed || !_pendingStateFetch || stateChan.busy) return
    var next = _pendingStateFetch
    _pendingStateFetch = null
    fetchState(next.forceTopology, next.showSpinner, next)
  }

  function fetchLibrary(force) {
    if (!socketPath) return
    if (libraryChan.busy) {
      var prevForce = _pendingLibraryFetch ? _pendingLibraryFetch.force : false
      _pendingLibraryFetch = { force: prevForce || !!force }
      return
    }
    libraryLoading = true
    if (!libraryChan.send({ cmd: "browse_library", force: !!force })) {
      libraryLoading = false
    }
  }

  function _drainPendingLibraryFetch() {
    if (!subscribed || !_pendingLibraryFetch || libraryChan.busy) return
    var next = _pendingLibraryFetch
    _pendingLibraryFetch = null
    fetchLibrary(next.force)
  }

  function fetchQueue(coordUuid, limit, force) {
    if (!socketPath || !coordUuid) return
    if (queueChan.busy) {
      var prevForce = _pendingQueueFetch && _pendingQueueFetch.coordUuid === coordUuid
        ? _pendingQueueFetch.force
        : false
      _pendingQueueFetch = { coordUuid: coordUuid, limit: limit, force: prevForce || !!force }
      return
    }
    queueLoading = true
    if (!queueChan.send({ cmd: "browse_queue", coord_uuid: coordUuid, limit: limit, force: !!force })) {
      queueLoading = false
    }
  }

  function _drainPendingQueueFetch() {
    if (!subscribed || !_pendingQueueFetch || queueChan.busy) return
    var next = _pendingQueueFetch
    _pendingQueueFetch = null
    fetchQueue(next.coordUuid, next.limit, next.force)
  }

  function _isAudioOrTopoBarrier(cmd) {
    return cmd === "set_group_volume" || cmd === "set_speaker_volume"
        || cmd === "set_group_mute" || cmd === "set_speaker_mute"
        || cmd === "join_group" || cmd === "leave_group"
  }

  function sendCommand(payload) {
    if (!socketPath || !payload) return
    // Clear any stale debounced drag update for the same target so it cannot
    // overwrite a final release or direct volume step.
    var volKey = _volumeKey(payload)
    if (volKey !== "" && _pendingVolumes[volKey]) {
      delete _pendingVolumes[volKey]
      if (!_hasPendingVolumes() && volumeDebounce.running) volumeDebounce.stop()
    }
    // Flush other targets' debounced volumes before enqueuing an audio/topology
    // barrier so an older pending volume cannot fire later and invert order.
    if (_isAudioOrTopoBarrier(payload.cmd) && _hasPendingVolumes()) {
      if (volumeDebounce.running) volumeDebounce.stop()
      var pending = _pendingVolumes
      _pendingVolumes = ({})
      for (var k in pending) {
        sendCommand(pending[k])
      }
    }
    if (volKey !== "") {
      var targetKey = payload.cmd === "set_group_volume" ? "coord_uuid" : "speaker_uuid"
      for (var i = _cmdQueue.length - 1; i >= 0; i--) {
        var qv = _cmdQueue[i]
        if (qv.cmd === payload.cmd && qv[targetKey] === payload[targetKey]) {
          _cmdQueue[i] = payload
          drainCommands()
          return
        }
        if (_isAudioOrTopoBarrier(qv.cmd)) {
          break
        }
      }
    } else if (payload.cmd === "seek") {
      for (var j = _cmdQueue.length - 1; j >= 0; j--) {
        var qc = _cmdQueue[j]
        if (qc.cmd === "seek" && qc.coord_uuid === payload.coord_uuid) {
          _cmdQueue[j] = payload
          drainCommands()
          return
        }
        if (qc.cmd === "previous" || qc.cmd === "next" || qc.cmd === "stop"
            || qc.cmd === "play_pause" || qc.cmd === "play_queue_track"
            || qc.cmd === "play_item" || qc.cmd === "join_group"
            || qc.cmd === "leave_group") {
          break
        }
      }
    }
    _cmdQueue.push(payload)
    drainCommands()
  }

  function queueVolumeChange(payload) {
    var volKey = _volumeKey(payload)
    if (volKey === "") {
      sendCommand(payload)
      return
    }
    _pendingVolumes[volKey] = payload
    if (!volumeDebounce.running) volumeDebounce.start()
  }

  function flushPendingVolume() {
    if (volumeDebounce.running) volumeDebounce.stop()
    var pending = _pendingVolumes
    _pendingVolumes = ({})
    for (var k in pending) {
      sendCommand(pending[k])
    }
  }

  function drainCommands() {
    if (cmdChan.busy || _cmdQueue.length === 0) return
    var next = _cmdQueue.shift()
    _activeCmd = next
    if (!cmdChan.send(next)) {
      _activeCmd = null
    }
  }

  Timer {
    id: volumeDebounce
    interval: 75
    repeat: false
    onTriggered: root.flushPendingVolume()
  }

  // Persistent push stream for instant cached initial paint and live daemon updates.
  Timer {
    id: eventReconnect
    interval: 2000
    repeat: false
    onTriggered: {
      if (root.subscribed && root.socketPath !== "" && !eventSock.connected)
        eventSock.connected = true
    }
  }

  onSubscribedChanged: {
    if (subscribed && socketPath !== "") {
      eventReconnect.stop()
      eventSock.connected = true
    } else {
      eventReconnect.stop()
      _pendingStateFetch = null
      _pendingLibraryFetch = null
      _pendingQueueFetch = null
      eventSock.connected = false
    }
  }

  Socket {
    id: eventSock
    path: root.socketPath
    connected: false

    parser: SplitParser {
      splitMarker: "\n"
      onRead: function(line) {
        if (!root.subscribed) return
        var text = String(line || "").trim()
        if (text !== "") root.stateReceived(text)
      }
    }

    onConnectionStateChanged: {
      if (connected) {
        if (!root.subscribed) {
          Qt.callLater(function() {
            if (!root.subscribed && eventSock.connected) eventSock.connected = false
          })
          return
        }
        write('{"cmd":"subscribe"}\n')
        flush()
        root.eventStreamConnected()
      } else if (root.subscribed) {
        if (!eventReconnect.running) {
          root.eventStreamDisconnected()
          eventReconnect.restart()
        }
      }
    }

    onError: function() {
      if (root.subscribed) {
        Qt.callLater(function() {
          if (root.subscribed && !eventSock.connected && !eventReconnect.running) {
            root.eventStreamDisconnected()
            eventReconnect.restart()
          }
        })
      }
    }
  }

  component RpcChannel: Item {
    id: ch
    property string socketPath: ""
    property int timeoutMs: 8000
    property bool busy: false
    property bool reconnecting: false
    property bool gotResponse: false
    property string pendingLine: ""
    property int requestSeq: 0

    signal rawResponse(string line)
    signal failed()

    function send(payloadObj) {
      if (busy || !socketPath) return false
      requestSeq += 1
      pendingLine = JSON.stringify(payloadObj)
      gotResponse = false
      busy = true
      timeoutTimer.restart()
      // Daemon closes each non-subscribe connection after one response; always open a fresh socket.
      if (sock.connected) {
        reconnecting = true
        sock.connected = false
      } else {
        reconnecting = false
        sock.connected = true
      }
      return true
    }

    function finish(ok, line) {
      timeoutTimer.stop()
      reconnecting = false
      gotResponse = false
      pendingLine = ""
      busy = false
      sock.connected = false
      if (ok) rawResponse(line)
      else failed()
    }

    Timer {
      id: timeoutTimer
      interval: ch.timeoutMs
      repeat: false
      onTriggered: if (ch.busy) ch.finish(false, "")
    }

    Socket {
      id: sock
      path: ch.socketPath
      connected: false

      parser: SplitParser {
        splitMarker: "\n"
        onRead: function(line) {
          if (!ch.busy || ch.gotResponse) return
          ch.gotResponse = true
          var seq = ch.requestSeq
          var text = String(line || "").trim()
          // Defer teardown so readyRead finishes unwinding cleanly in Quickshell.
          Qt.callLater(function() {
            if (ch.busy && ch.requestSeq === seq) ch.finish(text !== "", text)
          })
        }
      }

      onConnectionStateChanged: {
        if (connected) {
          if (!ch.busy) {
            Qt.callLater(function() {
              if (!ch.busy && sock.connected) sock.connected = false
            })
            return
          }
          if (ch.pendingLine !== "") {
            var line = ch.pendingLine
            ch.pendingLine = ""
            write(line + "\n")
            flush()
          }
        } else if (ch.busy) {
          if (ch.reconnecting) {
            var rseq = ch.requestSeq
            Qt.callLater(function() {
              if (ch.busy && ch.reconnecting && ch.requestSeq === rseq && !sock.connected) {
                ch.reconnecting = false
                sock.connected = true
              }
            })
            return
          }
          if (ch.gotResponse) return
          var seq = ch.requestSeq
          // Defer one tick so a SplitParser.onRead arriving with EOF wins cleanly.
          Qt.callLater(function() {
            if (ch.busy && !ch.gotResponse && !ch.reconnecting && ch.requestSeq === seq && !sock.connected)
              ch.finish(false, "")
          })
        }
      }

      onError: function() {
        if (!ch.busy || ch.reconnecting || ch.gotResponse) return
        var seq = ch.requestSeq
        // Defer so SplitParser.onRead wins on PeerClosedError EOF and Quickshell finishes unwinding.
        Qt.callLater(function() {
          if (ch.busy && !ch.gotResponse && !ch.reconnecting && ch.requestSeq === seq)
            ch.finish(false, "")
        })
      }
    }
  }

  RpcChannel {
    id: stateChan
    socketPath: root.socketPath
    timeoutMs: 8000
    onRawResponse: function(line) {
      root.stateLoading = false
      root.stateReceived(line)
      Qt.callLater(root._drainPendingStateFetch)
    }
    onFailed: {
      root.stateLoading = false
      root.stateFailed()
      Qt.callLater(root._drainPendingStateFetch)
    }
  }

  RpcChannel {
    id: libraryChan
    socketPath: root.socketPath
    timeoutMs: 8000
    onRawResponse: function(line) {
      root.libraryLoading = false
      root.libraryReceived(line)
      Qt.callLater(root._drainPendingLibraryFetch)
    }
    onFailed: {
      root.libraryLoading = false
      Qt.callLater(root._drainPendingLibraryFetch)
    }
  }

  RpcChannel {
    id: queueChan
    socketPath: root.socketPath
    timeoutMs: 8000
    onRawResponse: function(line) {
      root.queueLoading = false
      root.queueReceived(line)
      Qt.callLater(root._drainPendingQueueFetch)
    }
    onFailed: {
      root.queueLoading = false
      Qt.callLater(root._drainPendingQueueFetch)
    }
  }

  RpcChannel {
    id: cmdChan
    socketPath: root.socketPath
    timeoutMs: 5000
    onRawResponse: function() {
      var done = root._activeCmd
      root._activeCmd = null
      root.commandCompleted(
        done && done.cmd ? String(done.cmd) : "",
        done && done.coord_uuid ? String(done.coord_uuid) : ""
      )
      Qt.callLater(root.drainCommands)
    }
    onFailed: {
      root._activeCmd = null
      Qt.callLater(root.drainCommands)
    }
  }
}
