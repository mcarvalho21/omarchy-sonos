import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Playlists & Favorites drawer with pinned search bar and virtualized ListView.
Item {
  id: root

  required property QtObject controller
  required property QtObject strings
  required property PointerMoveGate pointerGate
  required property Item keyCatcher

  property alias searchField: playlistSearchField

  readonly property int itemCount: controller.filteredLibrary.length
  readonly property real listDesiredHeight: itemCount > 0
    ? (plList.contentHeight > 0 ? plList.contentHeight : itemCount * Style.space(48))
    : statusText.implicitHeight
  readonly property real desiredHeight: searchRow.implicitHeight + Style.space(8) + listDesiredHeight

  function resetScroll() {
    plList.contentY = 0
  }

  ColumnLayout {
    anchors.fill: parent
    spacing: Style.space(8)

    RowLayout {
      id: searchRow
      Layout.fillWidth: true
      spacing: Style.space(6)

      TextField {
        id: playlistSearchField
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        foreground: root.controller.foreground
        accent: root.controller.accent
        verticalPadding: Style.space(5)
        placeholderText: root.strings.t("sonos.search.playlists")
        text: root.controller.playlistQuery
        onTextChanged: root.controller.playlistQuery = text
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
        onClicked: root.controller.fetchLibrary(true)
      }
    }

    Text {
      id: statusText
      visible: root.itemCount === 0
      Layout.fillWidth: true
      textFormat: Text.PlainText
      text: root.controller.libraryLoading && root.controller.libraryItems.length === 0
        ? root.strings.t("sonos.loading")
        : root.strings.t("sonos.empty.playlists")
      color: root.controller.dim
      font.family: root.controller.fontFamily
      font.pixelSize: Style.font.bodySmall
      horizontalAlignment: Text.AlignHCenter
      padding: Style.space(12)
    }

    ListView {
      id: plList
      visible: root.itemCount > 0
      Layout.fillWidth: true
      Layout.fillHeight: true
      clip: true
      spacing: Style.space(4)
      boundsBehavior: Flickable.StopAtBounds
      interactive: contentHeight > height

      ScrollBar.vertical: ScrollBar {
        policy: plList.contentHeight > plList.height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
      }

      model: root.visible ? root.controller.filteredLibrary : []
      currentIndex: (root.controller.cursorActive && root.controller.focusSection === "drawer")
        ? root.controller.selectedIndex
        : -1

      onCurrentIndexChanged: if (currentIndex >= 0) Qt.callLater(keepCurrentVisible)
      function keepCurrentVisible() {
        if (currentIndex >= 0 && currentIndex < count)
          positionViewAtIndex(currentIndex, ListView.Contain)
      }

      delegate: CursorSurface {
        id: plRow
        required property var modelData
        required property int index

        width: plList.width
        height: Style.space(44)
        radius: Style.spacing.labelGap
        hasCursor: root.controller.cursorActive
          && root.controller.focusSection === "drawer"
          && root.controller.selectedIndex === index
        foreground: root.controller.foreground
        accent: root.controller.accent

        MouseArea {
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onPositionChanged: function(mouse) {
            if (root.pointerGate.moved(plRow, mouse))
              root.controller.setDrawerCursor(plRow.index)
          }
          onClicked: {
            root.controller.setDrawerCursor(plRow.index)
            root.controller.playLibraryItem(plRow.modelData, false)
          }
        }

        RowLayout {
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          anchors.leftMargin: Style.space(6)
          anchors.rightMargin: Style.space(6)
          spacing: Style.space(8)

          BorderSurface {
            Layout.preferredWidth: Style.space(32)
            Layout.preferredHeight: Style.space(32)
            Layout.alignment: Qt.AlignVCenter
            radius: Style.space(4)
            color: Style.selectedFillFor(root.controller.foreground, root.controller.accent)
            borderSpec: Border.controlSpec("normal", root.controller.foreground, root.controller.accent)

            SizedImage {
              anchors.fill: parent
              anchors.margins: 1
              crop: true
              url: plRow.modelData.art_url || ""
              visible: url !== ""
            }

            Text {
              anchors.centerIn: parent
              visible: !plRow.modelData.art_url
              textFormat: Text.PlainText
              text: "󰲸"
              color: root.controller.dim
              font.family: root.controller.fontFamily
              font.pixelSize: Style.font.body
            }
          }

          Column {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.alignment: Qt.AlignVCenter
            spacing: Style.space(1)

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: plRow.modelData.title || ""
              color: root.controller.foreground
              font.family: root.controller.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
              elide: Text.ElideRight
            }

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: plRow.modelData.description || (plRow.modelData.kind === "playlist"
                ? root.strings.t("sonos.playlist")
                : root.strings.t("sonos.favorite"))
              color: root.controller.dimmer
              font.family: root.controller.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }

          Button {
            Layout.alignment: Qt.AlignVCenter
            text: root.strings.t("sonos.action.playNext")
            bordered: true
            foreground: root.controller.foreground
            accent: root.controller.accent
            fontSize: Style.font.caption
            horizontalPadding: Style.space(7)
            verticalPadding: Style.space(3)
            onClicked: {
              root.controller.setDrawerCursor(plRow.index)
              root.controller.playLibraryItem(plRow.modelData, true)
            }
          }
        }
      }
    }
  }
}
