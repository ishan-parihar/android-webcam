import QtQuick
import Quickshell
import qs.Ui
import qs.Commons
Panel {
  id: root
  moduleName: "ishanp.android-webcam"
  ipcTarget: "ishanp.android-webcam"
  // Standalone panel (omarchy-shell shell toggle ishanp.android-webcam) — just launches GUI
  Component.onCompleted: Quickshell.execDetached(["android-webcam"])
}
