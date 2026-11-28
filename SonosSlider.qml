import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Omarchy-styled slider matching qs.Ui.PanelSlider visuals, but with wheel
// handling disabled by default so scrolling a Flickable/ScrollView/ListView
// never accidentally mutates sliders passing under the cursor.
Item {
  id: root

  property QtObject bar: null
  property real value: 0
  property real minimum: 0
  property real maximum: 1
  property real step: 0.05
  property bool integer: false
  property color trackColor: bar ? Style.selectedFillFor(bar.foreground, Commons.Color.accent) : "#333"
  property color fillColor: bar ? bar.foreground : Commons.Color.foreground
  property color knobColor: bar ? bar.foreground : Commons.Color.foreground
  property bool dragging: false
  property real trackHeight: Math.max(4, Math.round(Style.spacing.controlHeight * 0.11))
  property real knobSize: Math.max(14, Math.round(Style.spacing.controlHeight * 0.38))
  property real liveValue: value

  onValueChanged: if (!dragging) liveValue = value

  signal moved(real value)
  signal released(real value)
  signal rightClicked()

  implicitWidth: Style.space(200)
  implicitHeight: Math.max(Style.space(22), knobSize + Style.spacing.md)

  readonly property real range: Math.max(0.0001, maximum - minimum)
  readonly property real progress: Math.max(0, Math.min(1, (liveValue - minimum) / range))
  readonly property bool _hot: mouseArea.containsMouse || root.dragging
  readonly property bool mirrored: LayoutMirroring.enabled

  function trackX(fraction) {
    return track.x + track.width * (root.mirrored ? 1 - fraction : fraction)
  }

  Rectangle {
    id: track
    anchors.verticalCenter: parent.verticalCenter
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.leftMargin: root.knobSize / 2
    anchors.rightMargin: root.knobSize / 2
    height: root.trackHeight
    radius: height / 2
    color: root.trackColor
  }

  Rectangle {
    id: fill
    anchors.verticalCenter: track.verticalCenter
    x: root.mirrored ? (track.x + track.width - width) : track.x
    height: track.height
    radius: track.radius
    color: root.fillColor
    width: track.width * root.progress

    Behavior on width {
      enabled: !root.dragging
      NumberAnimation { duration: Style.duration(140); easing.type: Easing.OutCubic }
    }
  }

  BorderSurface {
    id: knob
    width: root.knobSize
    height: root.knobSize
    radius: root.knobSize / 2
    color: root.knobColor
    borderSpec: Border.flat(root.bar ? root.bar.background : "#101315", Math.max(1, Style.space(2)))
    anchors.verticalCenter: track.verticalCenter
    x: root.trackX(root.progress) - width / 2
    scale: root._hot ? 1.15 : 1.0

    Behavior on x {
      enabled: !root.dragging
      NumberAnimation { duration: Style.duration(140); easing.type: Easing.OutCubic }
    }

    Behavior on scale {
      NumberAnimation { duration: Style.duration(110); easing.type: Easing.OutCubic }
    }
  }

  MouseArea {
    id: mouseArea
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    acceptedButtons: Qt.LeftButton | Qt.RightButton
    preventStealing: root.dragging

    function valueFromX(x) {
      if (track.width <= 0) return root.minimum
      var fraction = Math.max(0, Math.min(1, (x - track.x) / track.width))
      if (root.mirrored) fraction = 1 - fraction
      var raw = root.minimum + fraction * root.range
      if (root.integer) raw = Math.round(raw)
      return Math.max(root.minimum, Math.min(root.maximum, raw))
    }

    onPressed: function(mouse) {
      if (mouse.button !== Qt.LeftButton) return
      root.dragging = true
      var next = valueFromX(mouse.x)
      root.liveValue = next
      root.moved(next)
    }
    onClicked: function(mouse) {
      if (mouse.button === Qt.RightButton) root.rightClicked()
    }
    onPositionChanged: function(mouse) {
      if (!root.dragging) return
      var next = valueFromX(mouse.x)
      if (next === root.liveValue) return
      root.liveValue = next
      root.moved(next)
    }
    onReleased: function(mouse) {
      if (!root.dragging) return
      root.dragging = false
      root.released(root.liveValue)
      root.liveValue = root.value
    }
    onCanceled: {
      if (!root.dragging) return
      root.dragging = false
      root.liveValue = root.value
    }
    // Intentionally no onWheel handler: wheel/trackpad scroll events must always
    // pass straight through to enclosing Flickable/ListView containers.
  }
}
