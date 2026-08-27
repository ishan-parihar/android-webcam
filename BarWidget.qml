import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

Panel {
  id: root
  moduleName: "ishanp.android-webcam"
  ipcTarget: "ishanp.android-webcam"
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  property bool streaming: false
  property string facing: "back"
  property string res: "720p"

  Process {
    id: statusProc
    command: ["bash","-c","pgrep -f 'scrcpy.*camera.*v4l2-sink' >/dev/null && echo on || echo off"]
    stdout: StdioCollector { onStreamFinished: root.streaming = (text.trim() === "on"); /* also parse config for label */ }
  }
  Timer { interval: 3000; running: true; repeat: true; triggeredOnStart: true; onTriggered: if (!statusProc.running) statusProc.running = true }

  Process { id: launchProc; command: ["android-webcam"] }
  Process { id: stopProc; command: ["bash","-c","pkill -f 'scrcpy.*camera.*v4l2-sink' ; echo stopped"] ; stdout: StdioCollector { onStreamFinished: statusProc.running = true } }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.streaming ? "󰕧●" : "󰕧"
    // color hint when streaming
    onPressed: function(btn){
      if (btn === Qt.RightButton) {
        if (root.streaming) stopProc.running = true
        else launchProc.running = true
      } else {
        root.toggle()
      }
    }
    onWheelMoved: function(delta){ /* wheel cycles front/back maybe */ }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    contentWidth: panel.fittedContentWidth(360)
    contentHeight: panel.fittedContentHeight(col.implicitHeight, 520)
    Column {
      id: col
      width: panel.availableWidth
      spacing: Style.space(12)
      Text { text: root.streaming ? "● Streaming → /dev/video0" : "○ Idle — /dev/video0 free"; color: root.bar.foreground; font.family: root.bar.fontFamily; font.pixelSize: Style.font.title; font.bold: true }
      Text { text: "Native scrcpy camera → v4l2loopback. Replaces Iriun."; color: Qt.darker(root.bar.foreground,1.3); font.family: root.bar.fontFamily; font.pixelSize: Style.font.caption; wrapMode: Text.Wrap; width: parent.width }
      PanelSeparator { foreground: root.bar.foreground }
      Row { width: parent.width; spacing: Style.space(8)
        Rectangle { width: (parent.width-8)/2; height: 36; radius: Style.cornerRadius; color: Util.alpha(root.bar.foreground,0.08)
          Text { text: "󰄀  Open GUI"; color: root.bar.foreground; font.family: root.bar.fontFamily; font.bold: true; anchors.centerIn: parent }
          MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: launchProc.running = true }
        }
        Rectangle { width: (parent.width-8)/2; height: 36; radius: Style.cornerRadius; color: root.streaming ? Util.alpha(Color.accent,0.18) : Util.alpha(root.bar.foreground,0.06); border.width: 1; border.color: Util.alpha(root.bar.foreground,0.12)
          Text { text: root.streaming ? "■  Stop" : "▶  Quick Start (back 720p)"; color: root.streaming ? Color.accent : root.bar.foreground; font.family: root.bar.fontFamily; font.bold: true; anchors.centerIn: parent; elide: Text.ElideRight; width: parent.width-12; horizontalAlignment: Text.AlignHCenter }
          MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: {
            if (root.streaming) stopProc.running = true
            else { Quickshell.execDetached(["bash","-c","android-webcam --back --720 &"]); statusProc.running = true }
          }}
        }
      }
      PanelSeparator { foreground: root.bar.foreground }
      Grid { columns: 2; width: parent.width; columnSpacing: Style.space(8); rowSpacing: Style.space(8)
        Rectangle { width: (parent.width-8)/2; height: 32; radius: Style.cornerRadius; color: Util.alpha(root.bar.foreground,0.06)
          Text { text: "Front 720p"; color: root.bar.foreground; font.family: root.bar.fontFamily; anchors.centerIn: parent }
          MouseArea { anchors.fill: parent; onClicked: Quickshell.execDetached(["bash","-c","android-webcam --front --720 &"]) }
        }
        Rectangle { width: (parent.width-8)/2; height: 32; radius: Style.cornerRadius; color: Util.alpha(root.bar.foreground,0.06)
          Text { text: "Back 1080p"; color: root.bar.foreground; font.family: root.bar.fontFamily; anchors.centerIn: parent }
          MouseArea { anchors.fill: parent; onClicked: Quickshell.execDetached(["bash","-c","android-webcam --back --1080 &"]) }
        }
        Rectangle { width: (parent.width-8)/2; height: 32; radius: Style.cornerRadius; color: Util.alpha(root.bar.foreground,0.06)
          Text { text: "Back + Torch"; color: root.bar.foreground; font.family: root.bar.fontFamily; anchors.centerIn: parent }
          MouseArea { anchors.fill: parent; onClicked: Quickshell.execDetached(["bash","-c","android-webcam --back --720 --torch &"]) }
        }
        Rectangle { width: (parent.width-8)/2; height: 32; radius: Style.cornerRadius; color: Util.alpha("#ff6b35",0.12)
          Text { text: "Mic ON (echo risk)"; color: "#ff6b35"; font.family: root.bar.fontFamily; anchors.centerIn: parent }
          MouseArea { anchors.fill: parent; onClicked: Quickshell.execDetached(["bash","-c","android-webcam --back --720 --with-audio &"]) }
        }
      }
      Text { text: "Mic via --audio-source=mic → PulseAudio 'scrcpy'. Lower speakers or use headphones."; color: Qt.darker(root.bar.foreground,1.4); font.family: root.bar.fontFamily; font.pixelSize: Style.font.caption; wrapMode: Text.Wrap; width: parent.width }
      Text { text: "Keys: SUPER+SHIFT+W (webcam), SUPER+SHIFT+A (screen), I alias. Test at webcamtests.com while streaming."; color: Qt.darker(root.bar.foreground,1.5); font.family: root.bar.fontFamily; font.pixelSize: Style.font.caption; wrapMode: Text.Wrap; width: parent.width }
    }
  }
}
