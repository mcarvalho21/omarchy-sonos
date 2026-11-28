import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Queue (Up Next) drawer with pinned search bar and virtualized ListView.
Item {
  id: root

  required property QtObject controller
  required property QtObject strings
  required property PointerMoveGate pointerGate
  required property Item keyCatcher

  property alias searchField: queueSearchField

  readonly property int itemCount: controller.filteredQueue.length
  readonly property real listDesiredHeight: itemCount > 0
    ? (itemCount * Style.space(38) + Math.max(0, itemCount - 1) * Style.space(3))
    : statusText.implicitHeight
  readonly property real desiredHeight: searchRow.implicitHeight + Style.space(8) + listDesiredHeight

  function resetScroll() {
    qList.contentY = 0
  }

  ColumnLayout {
    anchors.fill: parent
    spacing: Style.space(8)

    RowLayout {
      id: searchRow
      Layout.fillWidth: true
      spacing: Style.space(6)

      TextField {
        id: queueSearchField
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        foreground: root.controller.foreground
        accent: root.controller.accent
        verticalPadding: Style.space(5)
        placeholderText: root.strings.t("sonos.search.queue")
        text: root.controller.queueQuery
        onTextChanged: root.controller.queueQuery = text
        Keys.onEscapePressed: {
          if (text !== "") text = ""
          else root.keyCatcher.forceActiveFocus()
        }
        Keys.onDownPressed: {
          root.keyCatcher.forceActiveFocus()
          if (root.itemCount > 0) root.controller.setDrawerCursor(0)
        }
      }

      Button {
        Layout.alignment: Qt.AlignVCenter
        iconText: "󰑐"
        bordered: true
        foreground: root.controller.foreground
        accent: root.controller.accent
        horizontalPadding: Style.space(7)
        verticalPadding: Style.space(5)
        onClicked: root.controller.fetchQueue(true)
      }
    }

    Text {
      id: statusText
      visible: root.itemCount === 0
      Layout.fillWidth: true
      textFormat: Text.PlainText
      text: root.controller.queueLoading && root.controller.queueItems.length === 0
        ? root.strings.t("sonos.loading")
        : (root.controller.queueQuery ? root.strings.t("sonos.empty.queueSearch") : root.strings.t("sonos.empty.queue"))
      color: root.controller.dim
      font.family: root.controller.fontFamily
      font.pixelSize: Style.font.bodySmall
      horizontalAlignment: Text.AlignHCenter
      padding: Style.space(12)
    }

    ListView {
      id: qList
      visible: root.itemCount > 0
      Layout.fillWidth: true
      Layout.fillHeight: true
      clip: true
      spacing: Style.space(3)
      boundsBehavior: Flickable.StopAtBounds
      interactive: contentHeight > height

      ScrollBar.vertical: ScrollBar {
        policy: qList.contentHeight > qList.height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
      }

      model: root.visible ? root.controller.filteredQueue : []
      currentIndex: (root.controller.cursorActive && root.controller.focusSection === "drawer")
        ? root.controller.selectedIndex
        : -1

      onCurrentIndexChanged: if (currentIndex >= 0) Qt.callLater(keepCurrentVisible)
      function keepCurrentVisible() {
        if (currentIndex >= 0 && currentIndex < count)
          positionViewAtIndex(currentIndex, ListView.Contain)
      }

      delegate: CursorSurface {
        id: qRow
        required property var modelData
        required property int index

        readonly property bool isCurrent: root.controller.activeGroup !== null
          && root.controller.activeGroup.track_num === modelData.index

        width: qList.width
        height: Style.space(38)
        radius: Style.spacing.labelGap
        hasCursor: root.controller.cursorActive
          && root.controller.focusSection === "drawer"
          && root.controller.selectedIndex === index
        current: isCurrent
        foreground: root.controller.foreground
        accent: root.controller.accent

        MouseArea {
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onPositionChanged: function(mouse) {
            if (root.pointerGate.moved(qRow, mouse))
              root.controller.setDrawerCursor(qRow.index)
          }
          onClicked: {
            root.controller.setDrawerCursor(qRow.index)
            root.controller.playQueueTrack(qRow.modelData.index)
          }
        }

        RowLayout {
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          anchors.leftMargin: Style.space(6)
          anchors.rightMargin: Style.space(8)
          spacing: Style.space(8)

          Text {
            Layout.alignment: Qt.AlignVCenter
            Layout.preferredWidth: Style.space(22)
            textFormat: Text.PlainText
            text: qRow.isCurrent ? "󰐊" : root.controller.formatNumber(qRow.modelData.index)
            color: qRow.isCurrent ? root.controller.accent : root.controller.dimmer
            font.family: root.controller.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignRight
          }

          Column {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.alignment: Qt.AlignVCenter
            spacing: Style.space(1)

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: qRow.modelData.title || ""
              color: root.controller.foreground
              font.family: root.controller.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: qRow.isCurrent
              elide: Text.ElideRight
            }

            Text {
              width: parent.width
              visible: text !== ""
              textFormat: Text.PlainText
              text: qRow.modelData.artist || qRow.modelData.album || ""
              color: root.controller.dimmer
              font.family: root.controller.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }

          Text {
            Layout.alignment: Qt.AlignVCenter
            Layout.preferredWidth: Style.space(36)
            textFormat: Text.PlainText
            text: qRow.modelData.duration_sec > 0 ? root.controller.formatDuration(qRow.modelData.duration_sec) : ""
            color: root.controller.dimmer
            font.family: root.controller.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignRight
          }
        }
      }
    }
  }
}
