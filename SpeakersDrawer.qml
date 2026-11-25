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
          model: root.visible ? root.controller.groupUuids : []

          CursorSurface {
            id: groupRow
            required property var modelData
            required property int index

            readonly property var group: root.controller.groupByUuid(modelData) || ({
              coord_uuid: modelData,
              coord_name: "",
              room_label: "",
              member_names: [],
              member_uuids: [],
              playback_status: "Stopped",
              title: "",
              artist: "",
              source_name: "",
              source_icon: "",
              group_volume: 0,
              group_muted: false
            })
            readonly property bool selected: root.controller.activeGroup !== null
              && root.controller.activeGroup.coord_uuid === group.coord_uuid
            readonly property bool playing: group.playback_status === "Playing"

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
                text: groupRow.playing ? (groupRow.group.source_icon || "󰐊")
                  : (groupRow.group.playback_status === "Paused" ? "󰏤" : "󰓄")
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
                  text: (Array.isArray(groupRow.group.member_names) && groupRow.group.member_names.length > 0)
                    ? groupRow.group.member_names.join(" + ")
                    : groupRow.group.room_label
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
                  text: groupRow.group.title
                    ? ((groupRow.group.source_name ? groupRow.group.source_name + " · " : "")
                      + groupRow.group.title
                      + (groupRow.group.artist ? " — " + groupRow.group.artist : ""))
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
                text: root.controller.formatPercent(root.controller.groupVolume(groupRow.group))
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
                root.controller.selectedCoordUuid = groupRow.group.coord_uuid
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
          model: root.visible && root.showGrouping ? root.controller.speakerUuids : []

          CursorSurface {
            id: spRow
            required property var modelData
            required property int index

            readonly property var speaker: root.controller.speakerByUuid(modelData) || ({
              uuid: modelData,
              name: "",
              volume: 0,
              muted: false
            })
            readonly property int rowIndex: root.roomCount + index
            readonly property bool inGroup: Model.isSpeakerInGroup(root.controller.activeGroup, speaker)
            readonly property bool isCoord: root.controller.activeGroup !== null
              && root.controller.activeGroup.coord_uuid === speaker.uuid
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
                      root.controller.toggleSpeakerMembership(spRow.speaker, spRow.inGroup, spRow.canToggleGroup)
                    }
                  }
                }

                Text {
                  Layout.fillWidth: true
                  Layout.minimumWidth: 0
                  Layout.alignment: Qt.AlignVCenter
                  textFormat: Text.PlainText
                  text: spRow.speaker.name
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
                      root.controller.toggleSpeakerMembership(spRow.speaker, spRow.inGroup, spRow.canToggleGroup)
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
                  text: Model.volumeIcon(root.controller.memberVolume(spRow.speaker), spRow.speaker.muted)
                  color: spRow.speaker.muted ? root.controller.dimmer : root.controller.dim
                  font.family: root.controller.fontFamily
                  font.pixelSize: Style.font.caption
                  horizontalAlignment: Text.AlignHCenter

                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                      root.controller.setDrawerCursor(spRow.rowIndex)
                      root.controller.toggleSpeakerMute(spRow.speaker)
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
                  value: root.controller.memberVolume(spRow.speaker)
                  opacity: spRow.speaker.muted ? 0.5 : 1.0
                  onMoved: function(v) {
                    root.controller.previewMemberVolume(spRow.speaker, v, false)
                    root.controller.queueVolumeChange({
                      cmd: "set_speaker_volume",
                      speaker_uuid: spRow.speaker.uuid,
                      volume: Math.round(v)
                    })
                  }
                  onReleased: function(v) {
                    root.controller.previewMemberVolume(spRow.speaker, v, false)
                    root.controller.sendCommand({
                      cmd: "set_speaker_volume",
                      speaker_uuid: spRow.speaker.uuid,
                      volume: Math.round(v)
                    })
                  }
                  onRightClicked: root.controller.toggleSpeakerMute(spRow.speaker)
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
