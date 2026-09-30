import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

Panel {
  id: root
  moduleName: "ishanp.android-webcam"
  ipcTarget: "ishanp.android-webcam"
  // The bar widget owns the single IPC target; this summoned panel is
  // controlled through the shell's panel loader and must not register a
  // second handler for the same target.
  manageIpc: false
  readonly property color foreground: barForeground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // Panel opened via `omarchy-shell shell toggle ishanp.android-webcam`
  // Shows quick actions and a button to open the full GTK GUI

  Process { id: openGuiProc; command: ["android-webcam", "--gui"] }

  KeyboardPanel {
    id: panel
    anchorItem: null
    owner: root
    bar: root.bar
    open: root.opened
    contentWidth: panel.fittedContentWidth(400)
    contentHeight: panel.fittedContentHeight(col.implicitHeight, 600)
    Column {
      id: col
      width: panel.availableWidth
      spacing: Style.space(12)
      Text { text: "Android Webcam"; color: root.foreground; font.family: root.fontFamily; font.pixelSize: Style.font.title; font.bold: true }
      Text { text: "Native scrcpy camera → /dev/video0. Back/front, 720p/1080p, torch, mic."; color: Qt.darker(root.foreground,1.3); font.family: root.fontFamily; font.pixelSize: Style.font.caption; wrapMode: Text.Wrap; width: parent.width }
      PanelSeparator { foreground: root.foreground }
      Rectangle { width: parent.width; height: 40; radius: Style.cornerRadius; color: Util.alpha(Color.accent,0.15)
        Text { text: "󰄀  Open full GUI"; color: Color.accent; font.family: root.fontFamily; font.bold: true; anchors.centerIn: parent }
        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: openGuiProc.running = true }
      }
      Text { text: "Or use bar widget (󰕧) for quick start: Back 720p / Front 720p / Back 1080p / Torch / Mic."; color: Qt.darker(root.foreground,1.4); font.family: root.fontFamily; font.pixelSize: Style.font.caption; wrapMode: Text.Wrap; width: parent.width }
      Text { text: "Keys: SUPER+SHIFT+W (webcam GUI), SUPER+SHIFT+A (screen), I alias. Test at webcamtests.com while streaming."; color: Qt.darker(root.foreground,1.5); font.family: root.fontFamily; font.pixelSize: Style.font.caption; wrapMode: Text.Wrap; width: parent.width }
    }
  }
}
