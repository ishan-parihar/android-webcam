import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

import qs.Commons as Commons
Panel {
  id: root
  moduleName: "ishanp.android-webcam"
  ipcTarget: "ishanp.android-webcam"
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  property bool streaming: false
  property string facing: "back"
  property string res: "720p"

  Process {
    id: statusProc
    // Match the process NAME, never command-line text.
    //
    // `pgrep -f 'scrcpy.*camera.*v4l2-sink'` was always true from inside this
    // shell: -f matches the whole command line, and the shell running the check
      // is itself a process whose command line contains those words. Measured on
      // the installed build with NO scrcpy alive (confirmed by comparing every
      // /proc/<pid>/exe): the bar showed a filled "streaming" dot anyway.
      //
      // `pgrep -x scrcpy` fixed the self-match, but it cannot tell the WEBCAM
      // from the screen mirror - both are the same executable, so the dot read
      // "streaming" while the phone was mirroring the desktop. `android-webcam
      // is-streaming` asks the app's own code, which reads /proc/<pid>/cmdline
      // and requires --v4l2-sink (only the webcam carries it). It is invoked
      // directly rather than through a shell so the pattern never appears in a
      // command line that a -f match could pick up.
      command: ["android-webcam", "is-streaming"]
    stdout: StdioCollector { onStreamFinished: root.streaming = (text.trim() === "on"); /* also parse config for label */ }
  }
  Timer { interval: 3000; running: true; repeat: true; triggeredOnStart: true; onTriggered: if (!statusProc.running) statusProc.running = true }

  // Launch via setsid -f so the process is fully detached from Quickshell's
  // process group.  When Quickshell restarts (monitor change, theme reload,
  // etc.) the webcam must keep running, so we can't be a child of QML.
  //
  // --no-window is required, not optional: a bare `android-webcam` passes no
  // flag that main()'s allowlist recognises, so it falls through to
  // App().run() and opens the GTK window instead of streaming. Clicking the
  // bar icon would pop a window and start nothing.
  Process { id: launchProc; command: ["setsid","-f","android-webcam","--no-window"] }
  // Same self-match hazard as statusProc: `pkill -f` would match the shell
  // running it, so this could kill its own caller - or any terminal or editor
  // whose command line mentions scrcpy. `pkill -x` matches the name only.
  // A preset is "write the settings, then start". `set` is a separate process
  // that must finish before the stream is launched, or the stream would build
  // its argv from the old config. Chain with && so a rejected key (a typo, an
  // unknown setting) stops the start instead of silently running the previous
  // configuration.
  Process {
    id: presetProc
    property var args: []
    command: ["bash", "-c",
      "android-webcam set " + args.join(" ") +
      " && setsid -f android-webcam --no-window"]
  }
  // Stops only the WEBCAM. `pkill -x scrcpy` is camera-blind: android-screen is
  // a real scrcpy wrapper on this machine, so a blanket kill also tears down a
  // screen-mirror session. `stop` signals only the pids whose command line
  // carries --v4l2-sink. Invoked directly, not through a shell, so no pattern
  // ever appears in a command line that a -f match could pick up.
  Process { id: stopProc; command: ["android-webcam", "stop"] ; stdout: StdioCollector { onStreamFinished: statusProc.running = true } }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.streaming ? "󰕧●" : "󰕧"
    // color hint when streaming
    //
    // Left and right button both toggle, and both branches are written out.
    // The previous version called `root.toggle()`, which is defined nowhere -
    // not here and not in the shell's BarWidget base - so a left click did
    // nothing at all, silently. Right-click worked, which is why it read as
    // "not fully efficacious" rather than as an error.
    onPressed: function(btn){
      if (root.streaming) stopProc.running = true
      else launchProc.running = true
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
      Text { text: root.streaming ? "● Streaming → /dev/video0" : "○ Idle — /dev/video0 free"; color: root.foreground; font.family: root.fontFamily; font.pixelSize: Style.font.title; font.bold: true }
      Text { text: "Native scrcpy camera → v4l2loopback. Replaces Iriun."; color: Qt.darker(root.foreground,1.3); font.family: root.fontFamily; font.pixelSize: Style.font.caption; wrapMode: Text.Wrap; width: parent.width }
      PanelSeparator { foreground: root.foreground }
      Row { width: parent.width; spacing: Style.space(8)
        Rectangle { width: (parent.width-8)/2; height: 36; radius: Style.cornerRadius; color: Util.alpha(root.foreground,0.08)
          Text { text: "󰄀  Open GUI"; color: root.foreground; font.family: root.fontFamily; font.bold: true; anchors.centerIn: parent }
          MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: launchProc.running = true }
        }
        Rectangle { width: (parent.width-8)/2; height: 36; radius: Style.cornerRadius; color: root.streaming ? Util.alpha(Commons.Color.accent,0.18) : Util.alpha(root.foreground,0.06); border.width: 1; border.color: Util.alpha(root.foreground,0.12)
          Text { text: root.streaming ? "■  Stop" : "▶  Quick Start (back 720p)"; color: root.streaming ? Commons.Color.accent : root.foreground; font.family: root.fontFamily; font.bold: true; anchors.centerIn: parent; elide: Text.ElideRight; width: parent.width-12; horizontalAlignment: Text.AlignHCenter }
          MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: {
            if (root.streaming) stopProc.running = true
                else { presetProc.args = ["camera_facing", "back", "resolution", "720p"]
                       presetProc.running = true; statusProc.running = true }
          }}
        }
      }
      PanelSeparator { foreground: root.foreground }
      Grid { columns: 2; width: parent.width; columnSpacing: Style.space(8); rowSpacing: Style.space(8)
        Rectangle { width: (parent.width-8)/2; height: 32; radius: Style.cornerRadius; color: Util.alpha(root.foreground,0.06)
          Text { text: "Front 720p"; color: root.foreground; font.family: root.fontFamily; anchors.centerIn: parent }
              // Presets go through `set` FIRST, then start. The old form passed
              // `--back --720` to android-webcam, and cli() applies those to an
              // in-memory cfg that it never saves - so the stream came up right
              // but config.json still said the old facing and resolution, and
              // the panel then displayed settings that were not in use. State
              // that is not recorded is state the next surface cannot show.
              MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: {
                presetProc.args = ["camera_facing", "front", "resolution", "720p",
                                   "torch", "off", "mic_to_host", "off"]
                presetProc.running = true
              } }
        }
        Rectangle { width: (parent.width-8)/2; height: 32; radius: Style.cornerRadius; color: Util.alpha(root.foreground,0.06)
          Text { text: "Back 1080p"; color: root.foreground; font.family: root.fontFamily; anchors.centerIn: parent }
              MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: {
                presetProc.args = ["camera_facing", "back", "resolution", "1080p",
                                   "torch", "off", "mic_to_host", "off"]
                presetProc.running = true
              } }
        }
        Rectangle { width: (parent.width-8)/2; height: 32; radius: Style.cornerRadius; color: Util.alpha(root.foreground,0.06)
          Text { text: "Back + Torch"; color: root.foreground; font.family: root.fontFamily; anchors.centerIn: parent }
              MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: {
                presetProc.args = ["camera_facing", "back", "resolution", "720p",
                                   "torch", "on", "mic_to_host", "off"]
                presetProc.running = true
              } }
        }
        Rectangle { width: (parent.width-8)/2; height: 32; radius: Style.cornerRadius; color: Util.alpha("#ff6b35",0.12)
          Text { text: "Mic ON"; color: root.foreground; font.family: root.fontFamily; anchors.centerIn: parent }
              MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: {
                // torch off, explicitly. Without it "Back + Torch" leaves the
                // torch lit for every later start, because a preset that sets
                // only what it changes inherits everything else - so the torch
                // would silently follow you into this preset and every start
                // after it.
                presetProc.args = ["camera_facing", "back", "resolution", "720p",
                                   "torch", "off", "mic_to_host", "on"]
                presetProc.running = true
              } }
        }
      }
      Text { text: "Mic ON routes the phone mic through a null sink, so it does not play through your speakers."; color: Qt.darker(root.foreground,1.4); font.family: root.fontFamily; font.pixelSize: Style.font.caption; wrapMode: Text.Wrap; width: parent.width }
      Text { text: "Keys: SUPER+SHIFT+W (webcam), SUPER+SHIFT+A (screen), I alias. Test at webcamtests.com while streaming."; color: Qt.darker(root.foreground,1.5); font.family: root.fontFamily; font.pixelSize: Style.font.caption; wrapMode: Text.Wrap; width: parent.width }
    }
  }
}
