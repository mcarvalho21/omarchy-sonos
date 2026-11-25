import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// Speakers & multi-room grouping drawer.
// Owns its own bounded Flickable and participates in the panel's single
// keyboard + mouse CursorSurface cursor model.
Item {
  id: root

  required property QtObject controller
  required property QtObject strings
  required property PointerMoveGate pointerGate

  readonly property real desiredHeight: drawerCol.implicitHeight
  readonly property int roomCount: controller.groups.length
  readonly property bool showGrouping: controller.speakers.length > 1 && controller.activeGroup !== null

  function resetScroll() {
    flick.contentY = 0
  }

  function ensureCursorVisible(item) {
    if (!item) return
    var margin = Style.space(6)
    var maxY = Math.max(0, flick.contentHeight - flick.height)
    if (maxY <= 0) {
      flick.contentY = 0
      return
    }
    var pt = item.mapToItem(flick.contentItem, 0, 0)
    var top = pt.y
    var bottom = top + (item.height || 0)
    var viewTop = flick.contentY
    var viewBottom = viewTop + flick.height
    if (top < viewTop + margin) flick.contentY = Math.max(0, Math.min(maxY, top - margin))
    else if (bottom > viewBottom - margin) flick.contentY = Math.max(0, Math.min(maxY, bottom + margin - flick.height))
  }

  Flickable {
    id: flick
    anchors.fill: parent
    clip: true
    contentWidth: width
    contentHeight: drawerCol.implicitHeight
    boundsBehavior: Flickable.StopAtBounds
    flickableDirection: Flickable.VerticalFlick
    interactive: contentHeight > height

    ScrollBar.vertical: ScrollBar {
      policy: flick.contentHeight > flick.height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
    }

    Column {
      id: drawerCol
      width: flick.width
      spacing: Style.space(8)

      PanelSectionHeader {
        text: root.strings.t("sonos.section.rooms")
        foreground: root.controller.foreground
        fontFamily: root.controller.fontFamily
      }

      Column {
        width: parent.width
        spacing: Style.space(4)

        Repeater {
          model: root.visible ? root.controller.groups : []

          CursorSurface {
            id: groupRow
            required property var modelData
            required property int index

            readonly property bool selected: root.controller.activeGroup !== null
              && root.controller.activeGroup.coord_uuid === modelData.coord_uuid
            readonly property bool playing: modelData.playback_status === "Playing"

            width: parent.width
            implicitHeight: gInner.implicitHeight + Style.space(10)
            radius: Style.spacing.labelGap
            hasCursor: root.controller.cursorActive
              && root.controller.focusSection === "drawer"
              && root.controller.selectedIndex === index
            current: selected
            foreground: root.controller.foreground
            accent: root.controller.accent
            onHasCursorChanged: if (hasCursor) root.ensureCursorVisible(groupRow)

            RowLayout {
              id: gInner
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(8)
              anchors.rightMargin: Style.space(8)
              spacing: Style.space(8)

              Text {
                Layout.alignment: Qt.AlignVCenter
                Layout.preferredWidth: Style.space(16)
                textFormat: Text.PlainText
                text: groupRow.playing ? (groupRow.modelData.source_icon || "󰐊")
                  : (groupRow.modelData.playback_status === "Paused" ? "󰏤" : "󰓄")
                color: groupRow.playing ? root.controller.accent : root.controller.dim
                font.family: root.controller.fontFamily
                font.pixelSize: Style.font.body
                horizontalAlignment: Text.AlignHCenter
              }

              Column {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.alignment: Qt.AlignVCenter
                spacing: Style.space(1)

                Text {
                  width: parent.width
                  textFormat: Text.PlainText
                  text: (Array.isArray(groupRow.modelData.member_names) && groupRow.modelData.member_names.length > 0)
                    ? groupRow.modelData.member_names.join(" + ")
                    : groupRow.modelData.room_label
                  color: root.controller.foreground
                  font.family: root.controller.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: groupRow.selected || groupRow.playing
                  elide: Text.ElideRight
                }

                Text {
                  width: parent.width
                  visible: text !== ""
                  textFormat: Text.PlainText
                  text: groupRow.modelData.title
                    ? ((groupRow.modelData.source_name ? groupRow.modelData.source_name + " · " : "")
                      + groupRow.modelData.title
                      + (groupRow.modelData.artist ? " — " + groupRow.modelData.artist : ""))
                    : root.strings.t("sonos.stopped")
                  color: root.controller.dimmer
                  font.family: root.controller.fontFamily
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideRight
                }
              }

              Text {
                Layout.alignment: Qt.AlignVCenter
                Layout.preferredWidth: Style.space(32)
                textFormat: Text.PlainText
                text: root.controller.formatPercent(groupRow.modelData.group_volume)
                color: root.controller.dimmer
                font.family: root.controller.fontFamily
                font.pixelSize: Style.font.caption
                horizontalAlignment: Text.AlignRight
              }
            }

            MouseArea {
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onPositionChanged: function(mouse) {
                if (root.pointerGate.moved(groupRow, mouse))
                  root.controller.setDrawerCursor(groupRow.index)
              }
              onClicked: {
                root.controller.setDrawerCursor(groupRow.index)
                root.controller.selectedCoordUuid = groupRow.modelData.coord_uuid
              }
            }
          }
        }
      }

      PanelSeparator {
        visible: root.showGrouping
        width: parent.width
        foreground: root.controller.foreground
      }

      PanelSectionHeader {
        visible: root.showGrouping
        text: root.controller.activeGroup
          ? root.strings.t("sonos.section.groupWith").arg(root.controller.activeGroup.coord_name)
          : ""
        foreground: root.controller.foreground
        fontFamily: root.controller.fontFamily
      }

      Column {
        id: spCol
        visible: root.showGrouping
        width: parent.width
        spacing: Style.space(5)

        Repeater {
          model: root.visible && root.showGrouping ? root.controller.speakers : []

          CursorSurface {
            id: spRow
            required property var modelData
            required property int index

            readonly property int rowIndex: root.roomCount + index
            readonly property bool inGroup: Model.isSpeakerInGroup(root.controller.activeGroup, modelData)
            readonly property bool isCoord: root.controller.activeGroup !== null
              && root.controller.activeGroup.coord_uuid === modelData.uuid
            readonly property bool canToggleGroup: !(isCoord && root.controller.activeGroup !== null
              && (root.controller.activeGroup.member_uuids || []).length <= 1)

            width: spCol.width
            implicitHeight: spInner.implicitHeight + Style.space(10)
            radius: Style.spacing.labelGap
            hasCursor: root.controller.cursorActive
              && root.controller.focusSection === "drawer"
              && root.controller.selectedIndex === rowIndex
            current: inGroup
            bordered: inGroup
            foreground: root.controller.foreground
            accent: root.controller.accent
            currentFill: Style.normalFillFor(root.controller.foreground, root.controller.accent)
            onHasCursorChanged: if (hasCursor) root.ensureCursorVisible(spRow)

            Column {
              id: spInner
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(8)
              anchors.rightMargin: Style.space(8)
              spacing: Style.space(4)

              RowLayout {
                width: parent.width
                spacing: Style.space(8)

                BorderSurface {
                  Layout.preferredWidth: Style.space(18)
                  Layout.preferredHeight: Style.space(18)
                  Layout.alignment: Qt.AlignVCenter
                  radius: Style.space(4)
                  color: spRow.inGroup ? root.controller.accent : "transparent"
                  borderSpec: Border.controlSpec(spRow.inGroup ? "focus" : "normal", root.controller.foreground, root.controller.accent)
                  opacity: spRow.canToggleGroup ? 1.0 : 0.6

                  Text {
                    anchors.centerIn: parent
                    visible: spRow.inGroup
                    textFormat: Text.PlainText
                    text: "󰄬"
                    color: Commons.Color.background
                    font.family: root.controller.fontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                  }

                  MouseArea {
                    anchors.fill: parent
                    cursorShape: spRow.canToggleGroup ? Qt.PointingHandCursor : Qt.ArrowCursor
                    onClicked: {
                      root.controller.setDrawerCursor(spRow.rowIndex)
                      root.controller.toggleSpeakerMembership(spRow.modelData, spRow.inGroup, spRow.canToggleGroup)
                    }
                  }
                }

                Text {
                  Layout.fillWidth: true
                  Layout.minimumWidth: 0
                  Layout.alignment: Qt.AlignVCenter
                  textFormat: Text.PlainText
                  text: spRow.modelData.name
                  color: spRow.inGroup ? root.controller.foreground : root.controller.dim
                  font.family: root.controller.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: spRow.inGroup
                  elide: Text.ElideRight

                  MouseArea {
                    anchors.fill: parent
                    cursorShape: spRow.canToggleGroup ? Qt.PointingHandCursor : Qt.ArrowCursor
                    onClicked: {
                      root.controller.setDrawerCursor(spRow.rowIndex)
                      root.controller.toggleSpeakerMembership(spRow.modelData, spRow.inGroup, spRow.canToggleGroup)
                    }
                  }
                }

                Text {
                  Layout.alignment: Qt.AlignVCenter
                  Layout.preferredWidth: Style.space(32)
                  textFormat: Text.PlainText
                  text: root.controller.formatPercent(spVolSlider.liveValue)
                  color: root.controller.dimmer
                  font.family: root.controller.fontFamily
                  font.pixelSize: Style.font.caption
                  horizontalAlignment: Text.AlignRight
                }
              }

              RowLayout {
                visible: spRow.inGroup
                width: parent.width
                spacing: Style.space(6)

                Text {
                  Layout.alignment: Qt.AlignVCenter
                  Layout.preferredWidth: Style.space(18)
                  textFormat: Text.PlainText
                  text: Model.volumeIcon(spRow.modelData.volume, spRow.modelData.muted)
                  color: spRow.modelData.muted ? root.controller.dimmer : root.controller.dim
                  font.family: root.controller.fontFamily
                  font.pixelSize: Style.font.caption
                  horizontalAlignment: Text.AlignHCenter

                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                      root.controller.setDrawerCursor(spRow.rowIndex)
                      root.controller.toggleSpeakerMute(spRow.modelData)
                    }
                  }
                }

                SonosSlider {
                  id: spVolSlider
                  bar: root.controller.bar
                  Layout.fillWidth: true
                  Layout.minimumWidth: Style.space(60)
                  Layout.alignment: Qt.AlignVCenter
                  minimum: 0
                  maximum: 100
                  step: root.controller.volumeStep
                  integer: true
                  knobSize: Style.space(11)
                  trackHeight: Style.space(3)
                  value: spRow.modelData.volume || 0
                  opacity: spRow.modelData.muted ? 0.5 : 1.0
                  onMoved: function(v) {
                    root.controller.queueVolumeChange({
                      cmd: "set_speaker_volume",
                      speaker_uuid: spRow.modelData.uuid,
                      volume: Math.round(v)
                    })
                  }
                  onReleased: function(v) {
                    root.controller.sendCommand({
                      cmd: "set_speaker_volume",
                      speaker_uuid: spRow.modelData.uuid,
                      volume: Math.round(v)
                    })
                  }
                  onRightClicked: root.controller.toggleSpeakerMute(spRow.modelData)
                }
              }
            }

            // Passive hover tracking so moving across the speaker row updates
            // the unified cursor without blocking child clicks, hover, or sliders.
            HoverHandler {
              onPointChanged: {
                if (hovered && root.pointerGate.moved(spRow, point.position))
                  root.controller.setDrawerCursor(spRow.rowIndex)
              }
            }
          }
        }
      }
    }
  }
}
