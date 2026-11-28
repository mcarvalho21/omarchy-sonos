import QtQuick
import Quickshell.Io

Item {
  id: root

  property var shell: null
  property var manifest: null
  property var omarchyPath: ""

  readonly property string scriptPath: decodeURIComponent(String(Qt.resolvedUrl("sonos-mpris.py")).replace(/^file:\/\//, ""))
  property bool tearingDown: false
  property int restartDelayMs: 2000
  property double startedAtMs: 0

  visible: false

  Process {
    id: bridgeProc
    command: ["/usr/bin/python3", root.scriptPath]
    running: !root.tearingDown && root.scriptPath !== ""

    onRunningChanged: {
      if (running) root.startedAtMs = Date.now()
    }

    onExited: function(exitCode) {
      if (root.tearingDown || root.scriptPath === "") return
      // Only reset exponential backoff if the bridge stayed up long enough to be healthy.
      if (root.startedAtMs > 0 && (Date.now() - root.startedAtMs) >= 15000)
        root.restartDelayMs = 2000
      restartTimer.interval = root.restartDelayMs
      root.restartDelayMs = Math.min(root.restartDelayMs * 2, 30000)
      restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    repeat: false
    onTriggered: {
      if (!root.tearingDown && root.scriptPath !== "" && !bridgeProc.running)
        bridgeProc.running = true
    }
  }

  Component.onDestruction: {
    tearingDown = true
    restartTimer.stop()
    if (bridgeProc.running) bridgeProc.running = false
  }
}
