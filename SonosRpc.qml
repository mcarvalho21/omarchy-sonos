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

  signal stateReceived(var data)
  signal libraryReceived(var items)
  signal queueReceived(var items)
  signal commandCompleted()

  // Lists that cross a `var` signal arrive as Qt sequences, which fail Array.isArray().
  function isList(value) {
    return value !== null && value !== undefined && typeof value === "object" && typeof value.length === "number"
  }

  property var _cmdQueue: []
  property var _pendingVolumePayload: null

  function fetchState(forceTopology, showSpinner) {
    if (!socketPath || stateChan.busy) return
    if (showSpinner) stateLoading = true
    if (!stateChan.send({ cmd: "get_state", force_topology: !!forceTopology })) {
      stateLoading = false
    }
  }

  function fetchLibrary(force) {
    if (!socketPath || libraryChan.busy) return
    libraryLoading = true
    if (!libraryChan.send({ cmd: "browse_library", force: !!force })) {
      libraryLoading = false
    }
  }

  function fetchQueue(coordUuid, limit) {
    if (!socketPath || !coordUuid || queueChan.busy) return
    queueLoading = true
    if (!queueChan.send({ cmd: "browse_queue", coord_uuid: coordUuid, limit: limit })) {
      queueLoading = false
    }
  }

  function sendCommand(payload) {
    if (!socketPath || !payload) return
    // Coalesce queued volume updates for the same target so rapid steps stay snappy.
    if (payload.cmd === "set_group_volume" || payload.cmd === "set_speaker_volume") {
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
    _pendingVolumePayload = payload
    if (!volumeDebounce.running) volumeDebounce.start()
  }

  function flushPendingVolume() {
    if (volumeDebounce.running) volumeDebounce.stop()
    if (_pendingVolumePayload) {
      var p = _pendingVolumePayload
      _pendingVolumePayload = null
      sendCommand(p)
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
    onTriggered: {
      if (root._pendingVolumePayload) {
        var p = root._pendingVolumePayload
        root._pendingVolumePayload = null
        root.sendCommand(p)
      }
    }
  }

  component RpcChannel: Item {
    id: ch
    property string socketPath: ""
    property bool busy: false
    property string pendingLine: ""

    signal response(var data)
    signal failed()

    function send(payloadObj) {
      if (busy || !socketPath) return false
      pendingLine = JSON.stringify(payloadObj)
      busy = true
      timeoutTimer.restart()
      sock.connected = true
      return true
    }

    function finish(ok, parsed) {
      timeoutTimer.stop()
      pendingLine = ""
      busy = false
      if (sock.connected) sock.connected = false
      if (ok) response(parsed)
      else failed()
    }

    Timer {
      id: timeoutTimer
      interval: 5000
      repeat: false
      onTriggered: if (ch.busy) ch.finish(false, null)
    }

    Socket {
      id: sock
      path: ch.socketPath
      connected: false

      parser: SplitParser {
        splitMarker: "\n"
        onRead: function(line) {
          if (!ch.busy) return
          var parsed = null
          try {
            parsed = JSON.parse(String(line))
          } catch (e) {}
          ch.finish(parsed !== null, parsed)
        }
      }

      onConnectionStateChanged: {
        if (connected && ch.busy && ch.pendingLine !== "") {
          var line = ch.pendingLine
          ch.pendingLine = ""
          write(line + "\n")
          flush()
        } else if (!connected && ch.busy) {
          // Defer one tick so a SplitParser.onRead arriving with EOF wins cleanly.
          Qt.callLater(function() {
            if (ch.busy && !sock.connected) ch.finish(false, null)
          })
        }
      }

      onError: function() {
        if (ch.busy) ch.finish(false, null)
      }
    }
  }

  RpcChannel {
    id: stateChan
    socketPath: root.socketPath
    onResponse: function(data) {
      root.stateLoading = false
      if (data && data.ok) root.stateReceived(data)
    }
    onFailed: root.stateLoading = false
  }

  RpcChannel {
    id: libraryChan
    socketPath: root.socketPath
    onResponse: function(data) {
      root.libraryLoading = false
      if (data && data.ok && root.isList(data.items)) root.libraryReceived(data.items)
    }
    onFailed: root.libraryLoading = false
  }

  RpcChannel {
    id: queueChan
    socketPath: root.socketPath
    onResponse: function(data) {
      root.queueLoading = false
      if (data && data.ok && root.isList(data.items)) root.queueReceived(data.items)
    }
    onFailed: root.queueLoading = false
  }

  RpcChannel {
    id: cmdChan
    socketPath: root.socketPath
    onResponse: function() {
      root.commandCompleted()
      Qt.callLater(root.drainCommands)
    }
    onFailed: Qt.callLater(root.drainCommands)
  }
}
