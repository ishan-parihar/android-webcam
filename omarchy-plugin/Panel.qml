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
  // barForeground is supplied by the shell: /usr/share/omarchy/shell/Ui/Panel.qml
  // defines it as `bar ? bar.barForeground : Color.foreground`, so it is already
  // guarded and undefined-color is not a failure mode to defend against here.
  readonly property color foreground: barForeground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // Every setting shown here is read from the config FILE, never from what this
  // panel last clicked. The GUI app, the CLI and this panel all write the same
  // file, so a remembered click would drift from reality and lie - which is the
  // bug the bar's streaming dot had. cfgs is re-read on open and after every
  // write, and ConfigReader fails closed to a neutral "unknown" rather than to
  // a stale value.
  property var cfgs: ({})

  // Re-read the config whenever the panel opens, so the state shown is the
  // state on disk even if something else changed it while the panel was shut.
  Connections {
    target: root
    function onOpenedChanged() { if (root.opened) cfgReader.running = true; }
  }

  // python3 is used rather than jq/cat: ConfigReader validates the JSON and
  // falls back to {} on anything unexpected, so a corrupt or half-written file
  // shows as "unknown" instead of silently restoring the wrong toggles.
  Process {
    id: cfgReader
    command: ["python3", "-c",
      "import json,os,sys\n" +
      "p=os.path.expanduser('~/.config/android-webcam/config.json')\n" +
      "try:\n" +
      "    d=json.load(open(p))\n" +
      "    print(json.dumps({k:d.get(k) for k in ('torch','mic_to_host','camera_facing','resolution','with_preview')}))\n" +
      "except Exception:\n" +
      "    print('{}')\n"]
    stdout: StdioCollector {
      onStreamFinished: {
        var text = this.text.trim();
        try { root.cfgs = text ? JSON.parse(text) : ({}); }
        catch (e) { root.cfgs = ({}); }
      }
    }
  }

  function truthy(k) { return root.cfgs[k] === true; }
  function value(k, fallback) {
    var v = root.cfgs[k];
    return (v === undefined || v === null) ? fallback : v;
  }

  // Write through the CLI, never the file. `set` merges onto disk, so it
  // cannot revert android_port - the endpoint the PHONE pushes at runtime, which
  // a direct file write would clobber. It also coerces on/off to a real
  // boolean, which build_video_argv needs.
  function apply(key, value) {
    setProc.key = key; setProc.value = value;
    setProc.running = true;
  }

  Process {
    id: setProc
    property string key: ""
    property string value: ""
    command: ["android-webcam", "set", key, value]
    stdout: StdioCollector { onStreamFinished: cfgReader.running = true; }
  }

  Process { id: openGuiProc; command: ["android-webcam", "--gui"] }

  // Start/stop talk to the streaming process itself, not to config. The dot and
  // the button must agree, so both use the same check: the process NAME.
  // Never the -f flag: it matches command-line text, and the shell running the
    // check is itself a process containing those words, so it always reports
    // "streaming" no matter what is actually running.
    //
    // `pgrep -x scrcpy` cannot tell the WEBCAM from the screen mirror - both
    // are the same executable - so the dot could read "streaming" while the
    // phone was mirroring the desktop. `android-webcam is-streaming` asks the
    // app, which reads /proc/<pid>/cmdline and requires --v4l2-sink.
    Process {
      id: statusProc
      command: ["android-webcam", "is-streaming"]
    stdout: StdioCollector { onStreamFinished: root.streaming = (text.trim() === "on"); }
  }
  property bool streaming: false
  Timer { interval: 3000; running: true; repeat: true; triggeredOnStart: true
          onTriggered: if (!statusProc.running) statusProc.running = true }

  // `setsid -f` detaches from Quickshell, so the stream survives the panel
  // closing - a child of the panel's Process would be killed with it, making
  // Start a no-op the moment the user clicked away.
  //
  // `--no-window` is required and not optional. A bare `android-webcam` passes
  // no flag that main()'s allowlist recognises, so it falls through to
  // App().run() and opens the GTK window instead of streaming. That is the
  // same trap as the old `set` subcommand.
  Process {
    id: toggleProc
    command: ["bash", "-c",
      "if android-webcam is-streaming | grep -q on; then pkill -x scrcpy; echo stopped; " +
      "else setsid -f android-webcam --no-window; echo started; fi"]
  }

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
      spacing: Style.space(10)

      Text { text: root.streaming ? "● Streaming → /dev/video0" : "○ Idle — /dev/video0 free"
              color: root.foreground; font.family: root.fontFamily
              font.pixelSize: Style.font.title; font.bold: true }

      Text { text: "Camera, resolution and torch are read by scrcpy at startup, so a change here takes effect on the next start."
              color: Qt.darker(root.foreground, 1.3); font.family: root.fontFamily
              font.pixelSize: Style.font.caption; wrapMode: Text.Wrap; width: parent.width }

      PanelSeparator { foreground: root.foreground }

      // --- start / stop -----------------------------------------------------
      Rectangle {
        width: parent.width; height: 42; radius: Style.cornerRadius
        color: Util.alpha(Color.accent, 0.15)
        Rectangle {
          anchors.fill: parent; radius: parent.radius
          color: Util.alpha(Color.foreground, 0.08)
          visible: mouse.containsMouse
        }
        MouseArea {
          id: mouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
          onClicked: toggleProc.running = true
        }
        Text {
          anchors.centerIn: parent
          text: root.streaming ? "󰅖  Stop webcam" : "󰄀  Start webcam"
          color: Color.accent; font.family: root.fontFamily; font.bold: true
        }
      }

      // --- toggles ----------------------------------------------------------
      // Each row is: label, state read from the FILE, and a switch. `onToggled`
      // writes and then re-reads, so the switch shows what disk now says rather
      // than what the user just asked for - if the write failed, it snaps back.
      Row {
        width: parent.width; spacing: Style.space(8)
        Text { text: "Torch"; color: root.foreground; font.family: root.fontFamily
               font.pixelSize: Style.font.body; width: parent.width - 90; verticalAlignment: Text.AlignVCenter }
        ToggleSwitch {
          checked: root.truthy("torch")
          hasCursor: true
          foreground: root.foreground
          accent: Color.accent
          onToggled: root.apply("torch", checked ? "on" : "off")
        }
      }

      Row {
        width: parent.width; spacing: Style.space(8)
        Text { text: "Phone mic → host"; color: root.foreground; font.family: root.fontFamily
               font.pixelSize: Style.font.body; width: parent.width - 90; verticalAlignment: Text.AlignVCenter }
        ToggleSwitch {
          checked: root.truthy("mic_to_host")
          hasCursor: true
          foreground: root.foreground
          accent: Color.accent
          onToggled: root.apply("mic_to_host", checked ? "on" : "off")
        }
      }

      Row {
        width: parent.width; spacing: Style.space(8)
        Text { text: "On-screen preview"; color: root.foreground; font.family: root.fontFamily
               font.pixelSize: Style.font.body; width: parent.width - 90; verticalAlignment: Text.AlignVCenter }
        ToggleSwitch {
          checked: root.truthy("with_preview")
          hasCursor: true
          foreground: root.foreground
          accent: Color.accent
          onToggled: root.apply("with_preview", checked ? "on" : "off")
        }
      }

      PanelSeparator { foreground: root.foreground }

      // --- camera -----------------------------------------------------------
      Text { text: "Camera"; color: Qt.darker(root.foreground, 1.3); font.family: root.fontFamily
             font.pixelSize: Style.font.caption; font.bold: true }
      Row {
        width: parent.width; spacing: Style.space(8)
        Text { text: "Facing"; color: root.foreground; font.family: root.fontFamily
               font.pixelSize: Style.font.body; width: parent.width - 90; verticalAlignment: Text.AlignVCenter }
        Row {
          spacing: Style.space(6)
          Repeater {
            model: ["back", "front"]
            delegate: Rectangle {
              required property string modelData
              required property int index
              readonly property bool active: root.value("camera_facing", "back") === modelData
              width: 54; height: 28; radius: Style.cornerRadius
              color: active ? Util.alpha(Color.accent, 0.25) : "transparent"
              border.width: active ? 1 : 0
              border.color: Color.accent
              MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                          onClicked: root.apply("camera_facing", modelData) }
              Text { anchors.centerIn: parent; text: modelData
                     color: active ? root.foreground : Qt.darker(root.foreground, 1.4)
                     font.family: root.fontFamily; font.pixelSize: Style.font.caption
                     font.bold: active }
            }
          }
        }
      }

      Row {
        width: parent.width; spacing: Style.space(8)
        Text { text: "Resolution"; color: root.foreground; font.family: root.fontFamily
               font.pixelSize: Style.font.body; width: parent.width - 90; verticalAlignment: Text.AlignVCenter }
        Row {
          spacing: Style.space(6)
          Repeater {
            model: ["720p", "1080p"]
            delegate: Rectangle {
              required property string modelData
              readonly property bool active: root.value("resolution", "1080p") === modelData
              width: 54; height: 28; radius: Style.cornerRadius
              color: active ? Util.alpha(Color.accent, 0.25) : "transparent"
              border.width: active ? 1 : 0
              border.color: Color.accent
              MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                          onClicked: root.apply("resolution", modelData) }
              Text { anchors.centerIn: parent; text: modelData
                     color: active ? root.foreground : Qt.darker(root.foreground, 1.4)
                     font.family: root.fontFamily; font.pixelSize: Style.font.caption
                     font.bold: active }
            }
          }
        }
      }

      PanelSeparator { foreground: root.foreground }

      Rectangle {
        width: parent.width; height: 40; radius: Style.cornerRadius
        color: Util.alpha(Color.accent, 0.15)
        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                    onClicked: openGuiProc.running = true }
        Text { anchors.centerIn: parent; text: "󰄀  Open full GUI"
               color: Color.accent; font.family: root.fontFamily; font.bold: true }
      }
    }
  }
}