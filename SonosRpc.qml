import QtQuick
import Quickshell
import Quickshell.Io

// Direct Unix-socket JSON-line RPC client for sonos-mpris.py ($XDG_RUNTIME_DIR/mc-sonos.sock).
// Avoids spawning python3 subprocesses on every poll tick, click, or volume step.
Item {
  id: root
  visible: false

  readonly property string socketPath: {
    var runtime = Quickshell.env("XDG_RUNTIME_DIR") || ""
    return runtime !== "" ? runtime + "/mc-sonos.sock" : ""
  }

  property bool stateLoading: false
  property bool libraryLoading: false
  property bool queueLoading: false

  // Emit raw JSON strings so listeners parse once into native JS objects/arrays
  // without crossing a `var` signal boundary into Qt sequences.
  signal stateReceived(string rawJson)
  signal libraryReceived(string rawJson)
  signal queueReceived(string rawJson)
  signal commandCompleted()

  property var _cmdQueue: []
  property var _pendingVolumes: ({})
  property var _pendingStateFetch: null
  property var _pendingQueueFetch: null

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

  function fetchState(forceTopology, showSpinner) {
    if (!socketPath) return
    if (stateChan.busy) {
      var prev = _pendingStateFetch
      _pendingStateFetch = {
        forceTopology: (prev ? prev.forceTopology : false) || !!forceTopology,
        showSpinner: (prev ? prev.showSpinner : false) || !!showSpinner
      }
      return
    }
    if (showSpinner) stateLoading = true
    if (!stateChan.send({ cmd: "get_state", force_topology: !!forceTopology })) {
      stateLoading = false
    }
  }

  function _drainPendingStateFetch() {
    if (!_pendingStateFetch || stateChan.busy) return
    var next = _pendingStateFetch
    _pendingStateFetch = null
    fetchState(next.forceTopology, next.showSpinner)
  }

  function fetchLibrary(force) {
    if (!socketPath || libraryChan.busy) return
    libraryLoading = true
    if (!libraryChan.send({ cmd: "browse_library", force: !!force })) {
      libraryLoading = false
    }
  }

  function fetchQueue(coordUuid, limit) {
    if (!socketPath || !coordUuid) return
    if (queueChan.busy) {
      _pendingQueueFetch = { coordUuid: coordUuid, limit: limit }
      return
    }
    queueLoading = true
    if (!queueChan.send({ cmd: "browse_queue", coord_uuid: coordUuid, limit: limit })) {
      queueLoading = false
    }
  }

  function _drainPendingQueueFetch() {
    if (!_pendingQueueFetch || queueChan.busy) return
    var next = _pendingQueueFetch
    _pendingQueueFetch = null
    fetchQueue(next.coordUuid, next.limit)
  }

  function sendCommand(payload) {
    if (!socketPath || !payload) return
    // Clear any stale debounced drag update for the same target so it cannot
    // overwrite a final release or direct volume step.
    var volKey = _volumeKey(payload)
    if (volKey !== "") {
      if (_pendingVolumes[volKey]) {
        delete _pendingVolumes[volKey]
        if (!_hasPendingVolumes() && volumeDebounce.running) volumeDebounce.stop()
      }
      var targetKey = payload.cmd === "set_group_volume" ? "coord_uuid" : "speaker_uuid"
      for (var i = _cmdQueue.length - 1; i >= 0; i--) {
        if (_cmdQueue[i].cmd === payload.cmd && _cmdQueue[i][targetKey] === payload[targetKey]) {
          _cmdQueue[i] = payload
          drainCommands()
          return
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
    cmdChan.send(next)
  }

  Timer {
    id: volumeDebounce
    interval: 75
    repeat: false
    onTriggered: root.flushPendingVolume()
  }

  component RpcChannel: Item {
    id: ch
    property string socketPath: ""
    property int timeoutMs: 8000
    property bool busy: false
    property string pendingLine: ""
    property int requestSeq: 0

    signal rawResponse(string line)
    signal failed()

    function send(payloadObj) {
      if (busy || !socketPath) return false
      requestSeq += 1
      pendingLine = JSON.stringify(payloadObj)
      busy = true
      timeoutTimer.restart()
      if (sock.connected) {
        var line = pendingLine
        pendingLine = ""
        sock.write(line + "\n")
        sock.flush()
      } else {
        sock.connected = true
      }
      return true
    }

    function finish(ok, line) {
      timeoutTimer.stop()
      pendingLine = ""
      busy = false
      if (sock.connected) sock.connected = false
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
          if (!ch.busy) return
          var text = String(line || "").trim()
          ch.finish(text !== "", text)
        }
      }

      onConnectionStateChanged: {
        if (connected && ch.busy && ch.pendingLine !== "") {
          var line = ch.pendingLine
          ch.pendingLine = ""
          write(line + "\n")
          flush()
        } else if (!connected && ch.busy) {
          var seq = ch.requestSeq
          // Defer one tick so a SplitParser.onRead arriving with EOF wins cleanly.
          Qt.callLater(function() {
            if (ch.busy && ch.requestSeq === seq && !sock.connected) ch.finish(false, "")
          })
        }
      }

      onError: function() {
        if (ch.busy) ch.finish(false, "")
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
    }
    onFailed: root.libraryLoading = false
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
      root.commandCompleted()
      Qt.callLater(root.drainCommands)
    }
    onFailed: Qt.callLater(root.drainCommands)
  }
}
