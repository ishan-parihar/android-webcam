package dev.ishanp.androidwebcam;

import android.app.Activity;
import android.content.Context;
import android.graphics.Color;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Bundle;
import android.os.PowerManager;
import android.os.Handler;
import android.os.Looper;
import android.util.TypedValue;
import android.view.View;
import android.view.WindowManager;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Enumeration;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

/**
 * Phone-side agent for android-webcam.
 *
 * Goal: after the user opens this app ONCE, the host is permanently trusted and
 * adbd is pinned to a fixed port. No pairing screen, no pasted key, no console.
 *
 * How it gets there, in order:
 *   1. NsdManager browse for the host's advertised _androidwebcam._tcp service.
 *      This is the platform discovery API (API 16+) and needs no dependency.
 *   2. If that finds nothing, fall back to a bounded scan of OUR OWN subnet on
 *      the provisioning port. Computed from the real interface prefix length,
 *      so a /16 or a non-192.168.x network still works.
 *   3. Fetch the host's adb PUBLIC key (public key only - it is not a secret
 *      and cannot authenticate without the matching private key on the host).
 *   4. Append it to /data/misc/adb/adb_keys (needs root), pin adbd to 5555,
 *      write a boot hook so both survive a reboot.
 *   5. VERIFY by reading /proc/net/tcp for a LISTEN socket on 5555. Only then
 *      report success. `start adbd` silently no-ops behind a pending root
 *      prompt, so a green message without this check is a lie.
 */
public class MainActivity extends Activity {

    /** Kept in sync with adb-webcam's default; must match the host. */
    private static final int ADB_PORT = 5555;
    /** True only when a real LISTEN row was parsed - not merely when the
     *  read returned cleanly. An unreadable /proc/net/tcp must never be
     *  mistaken for "nothing is listening". */
    private boolean readOk = false;
    private PowerManager.WakeLock wifiLock;
    private static final String KEYS = "/data/misc/adb/adb_keys";
    private static final String SERVICE_TYPE = "_androidwebcam._tcp.";

    private final ExecutorService bg = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());

    private TextView statusView;
    private TextView detailView;
    private TextView addrView;
    private ProgressBarHolder spinner;
    private LinearLayout manualBox;
    private EditText keyInput;
    private Button setupBtn;

    /** Tiny holder so the UI layer stays free of AndroidX ProgressBar styling. */
    private static class ProgressBarHolder {
        android.widget.ProgressBar bar;
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Log2.init(this);
        setContentView(buildUi());
        // Do the whole job automatically on launch. The user's only action is
        // opening the app.
        autoConnect();
    }

    // ---------------------------------------------------------------- UI

    private View buildUi() {
        ScrollView scroll = new ScrollView(this);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(20);
        col.setPadding(pad, pad, pad, pad);

        TextView title = text("Android Webcam", 26);
        title.setTextColor(Color.parseColor("#1a1a1a"));
        col.addView(title);

        TextView sub = text(
            "Turns this phone into your computer's webcam. Just open this app once.", 14);
        sub.setTextColor(Color.parseColor("#555555"));
        sub.setPadding(0, dp(8), 0, dp(16));
        col.addView(sub);

        // The host operator needs this exact value; it is also what the host
        // app auto-discovers over mDNS.
        addrView = text("", 14);
        addrView.setTextColor(Color.parseColor("#2E7D32"));
        col.addView(addrView);

        spinner = new ProgressBarHolder();
        android.widget.ProgressBar pb = new android.widget.ProgressBar(this);
        spinner.bar = pb;
        col.addView(pb, lpTop(dp(12)));

        statusView = text("", 16);
        statusView.setPadding(0, dp(12), 0, dp(8));
        col.addView(statusView);

        detailView = text("", 12);
        detailView.setTextColor(Color.parseColor("#666666"));
        detailView.setPadding(0, dp(16), 0, 0);
        col.addView(detailView);

        // Diagnostics live IN the app, not only on the host: the whole premise
        // of the failure is that the phone is not yet trusted, so anything that
        // needs adb is unreachable exactly when the log is needed most.
        Button copyLog = new Button(this);
        copyLog.setText("Copy diagnostics");
        copyLog.setOnClickListener(v -> {
            String body = readLog();
            android.content.ClipboardManager cm =
                    (android.content.ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            cm.setPrimaryClip(android.content.ClipData.newPlainText("awcam-log", body));
            statusView.setText("Diagnostics copied. Paste them here.");
        });
        col.addView(copyLog, lpTop(dp(8)));

        // Manual path, collapsed by default. Exists only so a user on an exotic
        // network is never hard-blocked.
        manualBox = new LinearLayout(this);
        manualBox.setOrientation(LinearLayout.VERTICAL);
        manualBox.setVisibility(View.GONE);
        manualBox.setPadding(0, dp(24), 0, 0);

        Button reveal = new Button(this);
        reveal.setText("Show manual options");
        reveal.setOnClickListener(v -> manualBox.setVisibility(
            manualBox.getVisibility() == View.GONE ? View.VISIBLE : View.GONE));
        col.addView(reveal);

        manualBox.addView(text("Host public key (only if auto-discovery fails)", 13));
        keyInput = new EditText(this);
        keyInput.setHint("QAAA...");
        keyInput.setSingleLine(false);
        keyInput.setMinLines(2);
        manualBox.addView(keyInput, lp(dp(8)));

        setupBtn = new Button(this);
        setupBtn.setText("Set up connection");
        setupBtn.setOnClickListener(v -> {
            String k = keyInput.getText().toString().trim();
            if (k.isEmpty()) { statusView.setText("Paste the key first."); return; }
            // Manual paste: no host address is known, so no /phone push.
            // reportToHost is total and no-ops on null.
            provision(k, "manual entry", null);
        });
        manualBox.addView(setupBtn, lpTop(dp(12)));
        col.addView(manualBox);

        scroll.addView(col);
        return scroll;
    }

    /** The log file's contents, for the in-app copy button. */
    private String readLog() {
        java.io.File f = new java.io.File(Log2.logPath());
        if (!f.exists()) return "(no log yet — open the app and let it try)";
        try {
            return new String(java.nio.file.Files.readAllBytes(f.toPath()),
                    java.nio.charset.StandardCharsets.UTF_8);
        } catch (Exception e) {
            return "(log unreadable: " + e + ")";
        }
    }

    // ------------------------------------------------------ root helpers

    private boolean hasRoot() {
        try {
            Process p = new ProcessBuilder("su", "-c", "id -u").redirectErrorStream(true).start();
            BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream()));
            String out = r.readLine();
            p.waitFor();
            return out != null && out.trim().equals("0");
        } catch (Exception e) {
            return false;
        }
    }

    /** Run a shell command as root, return combined stdout (or "" on failure). */
    private String su(String cmd) {
        try {
            Process p = new ProcessBuilder("su", "-c", cmd).redirectErrorStream(true).start();
            BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream()));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = r.readLine()) != null) sb.append(line).append('\n');
            p.waitFor();
            return sb.toString().trim();
        } catch (Exception e) {
            return "";
        }
    }

    private String detectHookDir() {
        if (su("test -d /data/adb/post-fs-data.d && echo y").contains("y")) {
            return "/data/adb/post-fs-data.d";
        }
        if (su("test -d /data/adb/service.d && echo y").contains("y")) {
            return "/data/adb/service.d";
        }
        return "/data/adb/post-fs-data.d";
    }

    // ------------------------------------------------------------ network

    /**
     * The WiFi interface's IPv4 address.
     *
     * This deliberately does NOT return "the first non-loopback IPv4". On
     * Android that is usually the CELLULAR interface (or a VPN/tethering one),
     * so the scan would sweep the wrong subnet and never see the computer.
     * Only a wlan* interface is accepted; if none is up we fall back to the
     * first routable IPv4 so the app still works on a non-standard ROM.
     */
    private String myIp() { return wifiIface() == null ? firstRoutableV4() : v4(wifiIface()); }

    /** The interface's IPv4 as a string, or null if it has none. */
    private String v4(NetworkInterface n) {
        try {
            for (java.net.InterfaceAddress ia : n.getInterfaceAddresses()) {
                InetAddress a = ia.getAddress();
                if (a instanceof Inet4Address && !a.isLinkLocalAddress()) {
                    return a.getHostAddress();
                }
            }
        } catch (Exception ignored) {}
        return null;
    }

    private NetworkInterface wifiIface() {
        try {
            Enumeration<NetworkInterface> ifs = NetworkInterface.getNetworkInterfaces();
            while (ifs.hasMoreElements()) {
                NetworkInterface n = ifs.nextElement();
                if (!n.isUp() || n.isLoopback()) continue;
                if (n.getName().startsWith("wlan") && v4(n) != null) return n;
            }
        } catch (Exception ignored) {}
        return null;
    }

    private String firstRoutableV4() {
        try {
            Enumeration<NetworkInterface> ifs = NetworkInterface.getNetworkInterfaces();
            while (ifs.hasMoreElements()) {
                NetworkInterface n = ifs.nextElement();
                if (!n.isUp() || n.isLoopback()) continue;
                String s = v4(n);
                if (s != null) return s;
            }
        } catch (Exception ignored) {}
        return null;
    }

    /**
     * The real network prefix length, so a fallback scan covers the subnet the
     * phone is actually on instead of assuming a /24.
     *
     * getNetworkPrefixLength() lives on InterfaceAddress, NOT on
     * NetworkInterface - verified against platforms/android-36 with javap.
     */
    private int prefixLength() {
        try {
            // Same interface myIp() reports, so the scan always covers the
            // subnet whose address the user is looking at.
            NetworkInterface w = wifiIface();
            if (w != null) {
                for (java.net.InterfaceAddress ia : w.getInterfaceAddresses()) {
                    InetAddress a = ia.getAddress();
                    if (a instanceof Inet4Address && !a.isLinkLocalAddress()) {
                        return ia.getNetworkPrefixLength();
                    }
                }
            }
        } catch (Exception ignored) {}
        return 24;
    }

    /**
     * Every port this phone is actually LISTENing on, from /proc/net/tcp.
     *
     * World-readable, so this costs no root - unlike shelling out to su twenty
     * times, which re-prompts for the Magisk/KernelSU dialog each round.
     *
     * Returns ALL listening ports, not just the adb one. Asserting a fixed 5555
     * is wrong on this device: Android's wireless debugging binds a RANDOM
     * port (observed 43203), so a check for 5555 could never pass and the app
     * reported "not listening" on a phone that was serving fine.
     */
    /** A fresh read, so the caller's check is never based on a stale flag. */
    private boolean freshReadOk() {
        listeningPorts();
        return readOk;
    }

    private List<Integer> listeningPorts() {
        List<Integer> out = new ArrayList<>();
        readOk = false;
        for (String f : new String[]{"/proc/net/tcp", "/proc/net/tcp6"}) {
            String txt = bareRead(f);
            // The discriminator is ZERO LISTEN ROWS, not a thrown exception.
            // On the device the bare read SUCCEEDS and yields only a header,
            // so keying the retry on an exception never fires and we would
            // still print ports=[] and fall back to 5555.
            if (countListenRows(txt) == 0) {
                String r = su("cat " + f + " 2>/dev/null");
                if (r == null) r = "";
                int rows = countListenRows(r);
                Log2.i("bare read of " + f + " gave 0 LISTEN rows; su read "
                        + r.length() + " bytes -> " + rows + " rows");
                if (rows > 0) txt = r;
            }
            for (int p : parseListenPorts(txt)) {
                out.add(p);
                readOk = true;      // a real socket row was parsed
            }
        }
        return out;
    }

    private String bareRead(String f) {
        StringBuilder sb = new StringBuilder();
        try (BufferedReader r = new BufferedReader(new InputStreamReader(
                new java.io.FileInputStream(f)))) {
            String line; boolean first = true;
            while ((line = r.readLine()) != null) {
                if (first) { first = false; continue; }
                sb.append(line).append('\n');
            }
        } catch (Exception e) {
            return "";
        }
        return sb.toString();
    }

    private int countListenRows(String txt) {
        int n = 0;
        if (txt == null) return 0;
        for (String line : txt.split("\n")) {
            if (line.trim().isEmpty()) continue;
            String[] col = line.trim().split("\\s+");
            if (col.length >= 4 && col[3].equals("0A")) n++;
        }
        return n;
    }

    private List<Integer> parseListenPorts(String txt) {
        List<Integer> out = new ArrayList<>();
        if (txt == null) return out;
        for (String line : txt.split("\n")) {
            if (line.trim().isEmpty()) continue;
            String[] col = line.trim().split("\\s+");
            if (col.length < 4 || !col[3].equals("0A")) continue;
            int i = col[1].lastIndexOf(':');
            if (i < 0) continue;
            try { out.add(Integer.parseInt(col[1].substring(i + 1), 16)); }
            catch (NumberFormatException ignored) {}
        }
        return out;
    }

    /**
     * The port adbd is ACTUALLY on.
     *
     * Trust order matters here. getprop service.adb.tcp.port returns whatever
     * was last SET - and provision() sets it to 5555 itself - so it reports
     * 5555 on a phone whose adbd is really listening on 43203. Reading a value
     * your own code wrote is not evidence. Real LISTEN sockets in
     * /proc/net/tcp are the ground truth; getprop is only a fallback hint.
     */
    private int actualAdbPort() {
        // Prefer a high port: on Android 13 wireless debugging adbd binds a
        // RANDOM high port, and 5555 is frequently ALSO open (it is the
        // wireless-debugging/MDM port). Picking 5555 when both are listening
        // reports a port the host cannot connect to.
        int best = -1;
        // One read, captured. readOk is set as a side effect of
        // listeningPorts(), so calling it again in a log line leaves the flag
        // reflecting whichever call ran last; every check below must use the
        // SAME snapshot as the loop.
        List<Integer> ports = listeningPorts();
        for (int p : ports) {
            // 5555 included (a real, common adbd port), plus the wireless
            // debugging range. Logging the full list keeps a wrong pick
            // visible in diagnostics instead of silently asserting it.
            if (p == ADB_PORT || (p >= 30000 && p <= 49999)) {
                if (best < 0 || p > best) best = p;
            }
        }
        if (best > 0) {
            Log2.i("actualAdbPort=" + best + " of listening=" + ports);
            return best;
        }
        // REFUSE to publish a guess. getprop is whatever was last SET, and
        // provision() sets it to 5555 itself moments before this runs, so
        // returning it (or ADB_PORT) here is how a 43203 phone announces
        // 5555 to the host - the exact bug the /phone push removes. When the
        // measurement failed, "don't report" is the only safe answer.
        Log2.i("no adb port in listening set " + ports
                + " readOk=" + readOk + " -> UNVERIFIED, will not report a port");
        return -1;
    }

    /** Bring adbd up on a fixed port with root. Returns once the command ran. */
    private void startAdbd() {
        String r = su("setprop service.adb.tcp.port " + ADB_PORT + "; "
                    + "setprop persist.adb.tcp.port " + ADB_PORT + "; "
                    + "stop adbd; start adbd;");
        Log2.i("startAdbd rc=[" + r.trim() + "]");
    }

    private boolean adbdListening() {
        for (int p : listeningPorts()) {
            // Narrower than "any high port": an unrelated service on 40xxx
            // must not be reported as adbd being up.
            if (p == ADB_PORT || (p >= 30000 && p <= 49999)) return true;
        }
        return false;
    }

    /**
     * Browse for the host's advertised service. This is the whole point: the
     * host app advertises itself, so the phone never has to guess an address.
     * Returns "host:port" or null.
     */
    private String discoverViaNsd() {
        final NsdManager mgr = (NsdManager) getSystemService(Context.NSD_SERVICE);
        if (mgr == null) return null;
        final CountDownLatch found = new CountDownLatch(1);
        final String[] hit = new String[1];

        // NOTE: NsdManager delivers ALL of these callbacks on the main looper.
        // So no callback may block - a wait() inside onServiceFound would
        // prevent onServiceResolved from ever being delivered, and discovery
        // would silently time out. Callbacks only record; the wait happens once,
        // on this background thread.
        NsdManager.DiscoveryListener dl = new NsdManager.DiscoveryListener() {
            @Override public void onStartDiscoveryFailed(String t, int code) { found.countDown(); }
            @Override public void onStopDiscoveryFailed(String t, int code) { found.countDown(); }
            @Override public void onDiscoveryStarted(String t) {}
            @Override public void onDiscoveryStopped(String t) { found.countDown(); }

            @Override public void onServiceFound(NsdServiceInfo info) {
                // Only the host's own provisioning service, not adb's own.
                String t = info.getServiceType();
                if (t == null || !t.contains("androidwebcam")) return;
                try {
                    mgr.resolveService(info, new NsdManager.ResolveListener() {
                        @Override public void onResolveFailed(NsdServiceInfo i, int c) { found.countDown(); }
                        @Override public void onServiceResolved(NsdServiceInfo i) {
                            InetAddress h = i.getHost();
                            if (h != null) hit[0] = h.getHostAddress() + ":" + i.getPort();
                            found.countDown();
                        }
                    });
                } catch (Exception e) {
                    found.countDown();
                }
            }

            @Override public void onServiceLost(NsdServiceInfo info) {}
        };

        boolean started = false;
        try {
            // The constant is PROTOCOL_DNS_SD (verified against
            // platforms/android-36 with javap), not PROTO_DNS.
            mgr.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, dl);
            started = true;
            found.await(10, TimeUnit.SECONDS);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        } catch (Exception ignored) {
            // Discovery can fail outright on some ROMs; the scan fallback covers it.
        } finally {
            if (started) {
                try { mgr.stopServiceDiscovery(dl); } catch (Exception ignored) {}
            }
        }
        return hit[0];
    }

    /** Fetch the host's adb PUBLIC key from its provisioning endpoint. */
    private String fetchKeyFrom(String hostAndPort) {
        String[] hp = hostAndPort.split(":");
        String host = hp[0];
        int port = hp.length > 1 ? Integer.parseInt(hp[1]) : 7911;
        String url = "http://" + host + ":" + port + "/adbkey.pub";
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(2500);
            c.setReadTimeout(4000);
            // Some Android builds block the default Java User-Agent on
            // cleartext HTTP, or route it through a captive-portal probe. A
            // plain token avoids both.
            c.setRequestProperty("User-Agent", "android-webcam/1.0");
            c.setRequestProperty("Accept", "*/*");
            int code = c.getResponseCode();
            if (code != 200) {
                Log2.i("GET " + url + " -> HTTP " + code);
                return null;
            }
            try (InputStream in = c.getInputStream()) {
                String s = new String(in.readAllBytes(), StandardCharsets.UTF_8).trim();
                boolean ok = s.startsWith("Q") && s.length() > 100;
                Log2.i("GET " + url + " -> 200, " + s.length() + " bytes, valid=" + ok);
                return ok ? s : null;
            }
        } catch (Exception e) {
            // A TIMEOUT means the SYN got NO answer. That is generic, and has
            // TWO candidate causes: the packet left via the wrong interface
            // (this device also holds ccmni1 = 100.88.225.39/8, a CGNAT range,
            // which the kernel may prefer for a 192.168.1.x destination), or
            // something dropped it. Nothing listening would instead be
            // ConnectException ("refused"). mDNS resolving the host only proves
            // multicast UDP reached the phone - it says nothing about a TCP
            // reply coming back. The route line decides which of the two it is,
            // and it is free; check it BEFORE going looking for a firewall.
            Log2.i("GET " + url + " -> FAILED " + e.getClass().getSimpleName()
                    + ": " + e.getMessage());
            Log2.i("  route: " + su("ip route get " + host).trim().replace('\n', ' '));
            Log2.i("  neigh: " + su("ip neigh show " + host).trim());
            return null;
        }
    }

    /** Candidate hosts on our own subnet, bounded and excluding ourselves. */
    /**
     * Candidate hosts on the phone's own subnet.
     *
     * The step matters: a /16 network has 65k hosts, so probing every one is
     * absurd, but the previous code computed a /16 mask and then still stepped
     * by ONE, which only ever probed 192.168.0.x and could never reach a host at
     * 192.168.1.13. Step by the block size so the sweep actually covers the
     * subnet, and cap the total so a /16 cannot turn into a 65k scan.
     */
    private List<String> scanSubnet(String myAddr) {
        List<String> out = new ArrayList<>();
        int plen = 24;   // see note below
        int mine = inetToInt(myAddr);
        int mask = 0xFFFFFF00;
        int net = mine & mask;
        int broadcast = net | ~mask;
        // A /24 is what WiFi actually hands out, and it is the only case we
        // can sweep exhaustively. A /16 would need 65k probes (a port scan of
        // the whole network); a /16 with a 256-step walks the wrong axis and
        // probes x.x.0.0 addresses instead of the /24 the host is on. So clamp
        // to /24: it covers the real case and fails visibly otherwise.
        int step = 1;
        int n = 0;
        for (int c = net + step; c < broadcast && n < 300; c += step) {
            if (c == mine) continue;
            out.add(intToInet(c));
            n++;
        }
        // WiFi hands out a /24 and that is the only case we sweep
        // exhaustively. A wider prefix needs tens of thousands of probes (a
        // port scan of the whole network), and a block-step over one silently
        // covers just the first /24 - which looks like "nothing is there"
        // rather than "we never looked". The real prefix is logged so the
        // clamp is visible instead of hidden in the mask.
        Log2.i("scanSubnet: ip=" + myAddr + " realPrefix=/" + prefixLength()
                + " sweeping /24 -> " + out.size() + " candidates");
        return out;
    }

    private static int inetToInt(String ip) {
        String[] p = ip.split("\\.");
        return (Integer.parseInt(p[0]) << 24) | (Integer.parseInt(p[1]) << 16)
             | (Integer.parseInt(p[2]) << 8) | Integer.parseInt(p[3]);
    }

    private static String intToInet(int v) {
        return ((v >> 24) & 255) + "." + ((v >> 16) & 255) + "."
             + ((v >> 8) & 255) + "." + (v & 255);
    }

    /**
     * One sweep of the phone's own subnet for a host serving the key.
     * Returns "ip:port" or null. Logging here is what makes a failed run
     * diagnosable instead of a guess.
     */
    /** A candidate host in the phone's own /24, for the control probe. */
    private String deriveHost() {
        String a = myIp();
        return (a != null && a.contains(".")) ? a.substring(0, a.lastIndexOf('.')) + ".13" : null;
    }

    /** 0 = connected, -1 = refused, -2 = timed out. The outcome is the data. */
    private int probeHostPort(String host, int port) throws Exception {
        try (Socket s = new Socket()) {
            s.connect(new InetSocketAddress(host, port), 2500);
            return 0;
        } catch (java.net.SocketTimeoutException e) {
            return -2;
        } catch (java.net.ConnectException e) {
            return -1;
        }
    }

    private String sweepForHost(String addr, String gateway) {
            // Probe the SUBNET GATEWAY, derived from the phone's own address
            // rather than hardcoded, so this works on any network. This does
            // not probe the host PC - on a typical router the host is not .1 -
            // it answers one question only: can the phone's TCP stack reach
            // the LAN at all? The per-exception tally below is the measurement
            // that covers the real host.
            // hostIp is null when the phone has no usable IPv4 (cellular-only),
            // and InetSocketAddress(null, port) throws into the catch-all,
            // logging a misleading "gateway probe null:7911 failed" on every
            // sweep. Skip the probe rather than report a failure that is
            // really a missing argument.
            if (gateway == null) {
                Log2.i("gateway probe skipped: no gateway address derivable");
            } else {
            // The decisive control, run BEFORE the 7911 probe. Probe the host
            // on a port that is definitely OPEN (sshd) and one that is
            // definitely CLOSED. Only a connect-in to the host proves the
            // phone can reach it - the host dialling out proves nothing about
            // the reverse direction, which is the direction the sweep uses.
            //   22 connects, 631 times out  -> host reachable, that port filtered
            //   22 times out                 -> cannot reach the host at all
            // A closed port normally answers with an RST ("refused"); a
            // timeout instead means nothing came back, which is what a
            // filtered path looks like from inside.
            int lastCode = 0;
            try {
                lastCode = probeHostPort(deriveHost(), 22);
                Log2.i("control probe " + deriveHost() + ":22 (sshd, OPEN on this host) -> " + lastCode);
            } catch (Exception e) {
                Log2.i("control probe " + deriveHost() + ":22 failed: " + e);
            }
            try {
                // 31337 is verified CLOSED on this host, so a timeout here is
                // meaningful: a closed port should answer with an RST
                // ("refused"), and silence instead means the path drops.
                Log2.i("control probe " + deriveHost() + ":31337 (verified CLOSED) -> "
                        + probeHostPort(deriveHost(), 31337));
            } catch (Exception e) {
                Log2.i("control probe " + deriveHost() + ":31337 failed: " + e);
            }
            // ARP state of the gateway: REACHABLE/STALE means the phone CAN
            // resolve the LAN, so a timeout is filtering. FAILED/INCOMPLETE
            // means it genuinely cannot reach the /24.
            Log2.i("  neigh gw: " + su("ip neigh show " + gateway).trim());
            try (Socket s = new Socket()) {
                s.connect(new InetSocketAddress(gateway, 7911), 3000);
                Log2.i("gateway probe " + gateway + ":7911 CONNECTED");
            } catch (java.net.SocketTimeoutException e) {
                Log2.i("gateway probe " + gateway + ":7911 TIMEOUT - packets dropped; route: "
                        + su("ip route get " + gateway).trim().replace('\n', ' '));
            } catch (java.net.ConnectException e) {
                Log2.i("gateway probe " + gateway + ":7911 REFUSED - LAN reachable, "
                        + "nothing on 7911; route: "
                        + su("ip route get " + gateway).trim().replace('\n', ' '));
            } catch (Exception e) {
                Log2.i("gateway probe " + gateway + ":7911 failed: " + e
                        + "; route: " + su("ip route get " + gateway).trim().replace('\n', ' '));
            }   // end gateway != null
            }
            ExecutorService pool = Executors.newFixedThreadPool(64);
        try {
            List<String> cands = scanSubnet(addr);
            // Tally WHY each candidate failed. An empty `live` list cannot tell
            // a host-listener problem from a routing problem; the distribution
            // can: 253 x ConnectException means the LAN answers, 253 x
            // NoRouteToHostException confirms the wrong interface.
            final java.util.Map<String,Integer> why =
                    new java.util.concurrent.ConcurrentHashMap<>();
            List<Future<Boolean>> fs = new ArrayList<>();
            for (final String c : cands) {
                fs.add(pool.submit(() -> {
                    try (Socket s = new Socket()) {
                        s.connect(new InetSocketAddress(c, 7911), 800);
                        return true;
                    } catch (Exception e) {
                        String k = e.getClass().getSimpleName();
                        why.merge(k, 1, Integer::sum);
                        return false;
                    }
                }));
            }
            List<String> live = new ArrayList<>();
            for (int i = 0; i < fs.size(); i++) {
                try { if (fs.get(i).get()) live.add(cands.get(i)); } catch (Exception ignored) {}
            }
            Log2.i("scan: tried " + cands.size() + " addrs, live on 7911 = " + live);
            Log2.i("scan: failure reasons " + cands.size() + " candidates -> " + why);
            // Put the outcome ON SCREEN. "Looking again" with no visible reason
            // is the failure mode this whole change exists to eliminate.
            final int tried = cands.size();
            final List<String> liveCopy = new ArrayList<>(live);
            ui.post(() -> statusView.setText(
                    tried + " addresses tried on port 7911.\n"
                    + (liveCopy.isEmpty()
                        ? "Nothing answered.\n\nThe computer is not offering the key: press "
                          + "“Let phone in” on it, and check this phone is on the SAME WiFi."
                        : "Answered: " + liveCopy
                          + "\n\nTrying to fetch the key…")));
            if (live.isEmpty()) {
                Log2.i("scan: nothing listening on 7911. adbdListening="
                        + adbdListening() + " ports=" + listeningPorts()
                        + " - check the host app has 'Let phone in' pressed, and that "
                        + "this phone is on the SAME wifi/subnet as the host.");
                return null;
            }
            for (String c : live) {
                String k = fetchKeyFrom(c + ":7911");
                if (k != null) return c + ":7911";
            }
            return null;
        } finally {
            pool.shutdownNow();
        }
    }

    // ------------------------------------------------------------- flow

    /**
     * Keep the screen on and stop the WiFi radio sleeping for the duration of
     * discovery/provisioning. Android 7+ doze ignores FLAG_KEEP_SCREEN_ON
     * once the screen is off, so the WiFi lock is the part that actually
     * keeps the association alive; both are set because they cover different
     * states (screen lit vs radio idle).
     */
    private void keepAwake(boolean on) {
        try {
            if (on) {
                getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
                if (pm != null) {
                    // Its own try: a SecurityException here must NOT abort the
                    // su call below, which is the part that actually holds
                    // WiFi open across a scan. Losing the lock is survivable;
                    // losing the su setting is what made scans untrustworthy.
                    try {
                        wifiLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,
                                                  "android-webcam:discovery");
                        wifiLock.setReferenceCounted(false);
                        wifiLock.acquire(10 * 60 * 1000L);   // safety timeout
                    } catch (Exception le) {
                        Log2.i("wake lock unavailable (" + le + "); "
                                + "continuing - the su setting below is what matters");
                    }
                }
                // The su setting is the part that survives an AP deauth: the
                // screen flag only applies while this app is foreground, and
                // the wake lock only keeps the CPU up. Log the return so a
                // pasted log proves the setting actually applied.
                String r = su("svc wifi enable; settings put global wifi_sleep_never_warn 1;"
                            + " settings get global wifi_sleep_never_warn");
                Log2.i("keepAwake on: lock=" + (wifiLock != null)
                        + " wifi_sleep_never_warn=" + r.trim()
                        + " (1 = wifi will not sleep mid-scan)");
            } else {
                getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
                Log2.i("keepAwake off: screen flag cleared, lock released");
            }
        } catch (Exception e) {
            Log2.i("keepAwake(" + on + ") failed: " + e);
        }
    }

    @Override protected void onDestroy() {
        keepAwake(false);
        super.onDestroy();
    }

    private void autoConnect() {
        // Hold the screen and the WiFi radio awake for the whole discovery +
        // provisioning window. Measured repeatedly this session: the phone
        // answers every probe during one run and is unreachable by the next
        // (ARP REACHABLE then FAILED, ICMP 0 received, no TCP port answering
        // at all). That is the signature of an idle device being dropped by
        // the AP - and it makes every measurement untrustworthy, because the
        // endpoint vanishes between runs. Fix it here rather than asking the
        // user to babysit the screen.
        keepAwake(true);
        final String addr = myIp();
        Log2.dumpInterfaces();
        int _p = actualAdbPort();
        Log2.i("listening ports: " + listeningPorts() + " readOk=" + readOk
                + " actualAdbPort=" + (_p > 0 ? "" + _p : "UNVERIFIED"));
        Log2.i("launch: myIp=" + addr + " hasRoot=" + hasRoot()
                + " adbdListening=" + adbdListening() + " keys=" + countKeys()
                + " adbPort=" + (_p > 0 ? "" + _p : "UNVERIFIED")
                + " logPath=" + Log2.logPath());
        Log2.i("interfaces chosen: wifi="
                + (wifiIface() == null ? "none" : wifiIface().getName())
                + " realPrefix=/" + prefixLength());
        final int launchPort = actualAdbPort();
        ui.post(() -> addrView.setText("This phone: "
                + (addr == null ? "no WiFi address"
                   : addr + (launchPort > 0 ? ":" + launchPort : ":port unknown"))));

        bg.execute(() -> {
            // Fast path: already provisioned on a PREVIOUS run. It must check
            // that a trusted key is actually present, not merely that adbd
            // happens to be listening: 5555 is also where the phone's own
            // wireless-debugging daemon listens, so "adbd is up" alone would
            // report Ready with an empty trust store - the exact stuck state.
            //
            // If adbd is not running, START IT before scanning. Measured on
            // device: with adbd down, listeningPorts() returns [] and every
            // sweep reports "253 addrs tried, live on 7911 = []" - the phone
            // was knocking on a closed door, and the failure message blamed
            // the host ("Is 'Trust this computer' pressed?"). There is
            // nothing to provision until adbd is up, so bring it up first.
            // Only restart adbd when the port read is TRUSTWORTHY. isEmpty()
            // is the wrong test: it is ALSO what a trustworthy read returns
            // when nothing is listening, so keying the guard on it would skip
            // the restart on a genuinely stopped adbd and run it on a blocked
            // read. readOk means a real LISTEN row was parsed.
            if (hasRoot() && !freshReadOk()) {
                Log2.i("socket read unavailable (no LISTEN rows via bare or su); "
                        + "leaving adbd alone rather than pinning 5555");
            } else if (hasRoot() && !adbdListening()) {
                Log2.i("adbd not running; starting it on " + ADB_PORT);
                startAdbd();
                // adbd takes a moment to bind; poll rather than assume.
                for (int i = 0; i < 10 && !adbdListening(); i++) {
                    try { Thread.sleep(400); } catch (InterruptedException ignored) {}
                }
                Log2.i("after start: adbdListening=" + adbdListening()
                        + " ports=" + listeningPorts());
            }

            // countKeys() shells out to su, so call it once and reuse the value.
            if (hasRoot() && adbdListening()) {
                int keys = countKeys();
                if (keys > 0) {
                    ui.post(() -> done("Ready — your computer can use this phone as a "
                            + "webcam. " + keys + " trusted computer(s)."));
                    return;
                }
                // Listening but EMPTY: the port belongs to the phone's own
                // wireless-debugging adbd. Do NOT stop here - fall through and
                // provision, because the host is likely already reachable and the
                // whole point is that the user does nothing.
            }

            if (addr == null) {
                ui.post(() -> statusView.setText(
                        "This phone is not on WiFi. Join the same WiFi as your computer, then reopen."));
                return;
            }

        say("Looking for your computer…");
        Log2.i("start discovery; myIp=" + addr + " prefix=/" + prefixLength());
        // The scan is the PRIMARY path: it is the one that is actually
        // proven to work. mDNS is tried first because when it works it is
        // instant and unambiguous, but a host may not publish a resolvable
        // mDNS record at all, so it must never be the only way in.
        String host = discoverViaNsd();
        // The gateway, derived from the phone's OWN address so this works on
        // any subnet instead of baking in one network. Used only to answer
        // "can the phone reach the LAN at all" - the host PC is not necessarily
        // the gateway, so this is not a probe of the host.
        final String hostIp = (host != null) ? host
                : (addr != null && addr.contains(".")
                   ? addr.substring(0, addr.lastIndexOf('.')) + ".1" : null);
        String how = "automatic discovery (mDNS)";
        Log2.i("nsd -> " + (host == null ? "nothing" : host));

            if (host == null) {
                say("Scanning this network for your computer…");
                host = sweepForHost(addr, hostIp);
                how = "network scan";
            }

            if (host == null) {
                // A single sweep can miss: the host may not have been pressed
                // yet, or the two devices may have just been switched to the
                // same WiFi. Retry a few times over ~30s rather than dropping
                // straight to the paste prompt on one miss.
                for (int attempt = 2; attempt <= 4 && host == null; attempt++) {
                    say("Not yet — looking again (" + attempt + "/4)…");
                    Log2.i("retry " + attempt + " sweep");
                    try { Thread.sleep(7000); } catch (InterruptedException ignored) {
                        Thread.currentThread().interrupt();
                    }
                    host = sweepForHost(addr, hostIp);
                    if (host != null) how = "network scan (retry " + attempt + ")";
                }
            }

            if (host == null) {
                Log2.i("FAILED: no host found via nsd or 4 sweeps");
                ui.post(() -> {
                    statusView.setText("Couldn't find your computer.\n\n"
                            + "On the computer, open the Android Webcam app and press "
                            + "“Trust this computer”, then reopen this app.");
                    detailView.setText("Log: " + Log2.logPath());
                    spinner.bar.setVisibility(View.GONE);
                    manualBox.setVisibility(View.VISIBLE);
                });
                keepAwake(false);   // discovery done: stop holding the device
                return;
            }

            String key = fetchKeyFrom(host);
            if (key == null) {
                final String fHost = host;
                ui.post(() -> {
                    statusView.setText("Found your computer at " + fHost
                            + " but it didn't hand over a key.");
                    spinner.bar.setVisibility(View.GONE);
                });
                keepAwake(false);   // discovery done: stop holding the device
                return;
            }

            say("Found your computer at " + host + " (" + how + ")");
            Log2.i("provisioning with key from " + host);
            provision(key, how, host);
            // NO release here. We are already running ON the single-thread
            // bg executor, and provision() queues its work onto that same
            // executor - so a release at this line fires before provisioning
            // has done anything. The release lives at the end of
            // provision()'s own body instead.
        });
    }

    /**
     * Publish this phone's adb endpoint as _adb-tcp._tcp so the host can find
     * the real ip:port. The built-in _adb-tls-connect service is only published
     * while Wireless debugging is actively running; this stays published after
     * adbd is pinned to a fixed port.
     */
    private void advertiseAdb(int port) {
        try {
            NsdManager mgr = (NsdManager) getSystemService(Context.NSD_SERVICE);
            if (mgr == null) return;
            NsdServiceInfo info = new NsdServiceInfo();
            info.setServiceName("android-webcam-adb");
            info.setServiceType("_adb-tcp._tcp.");
            info.setPort(port);
            mgr.registerService(info, NsdManager.PROTOCOL_DNS_SD,
                    new NsdManager.RegistrationListener() {
                        @Override public void onServiceRegistered(NsdServiceInfo i) {
                            Log2.i("registered _adb-tcp._tcp on port " + i.getPort());
                        }
                        @Override public void onRegistrationFailed(NsdServiceInfo i, int c) {
                            Log2.i("adb advertisement failed code=" + c);
                        }
                        @Override public void onServiceUnregistered(NsdServiceInfo i) {}
                        @Override public void onUnregistrationFailed(NsdServiceInfo i, int c) {}
                    });
        } catch (Exception e) {
            Log2.i("advertise failed: " + e);
        }
    }

    private int countKeys() {
        String s = su("grep -c . " + KEYS + " 2>/dev/null || echo 0");
        try { return Integer.parseInt(s.trim()); } catch (Exception e) { return 0; }
    }

    private void say(String msg) {
        ui.post(() -> statusView.setText(msg));
    }

    private void done(String msg) {
        ui.post(() -> {
            statusView.setText(msg);
            spinner.bar.setVisibility(View.GONE);
            detailView.setText("Computer: run the Android Webcam app and press Start.");
        });
    }

    /** Tell the host where this phone actually is, so it does not have to guess. */
    private void reportToHost(String hostAndPort) {
        // Total: the null/empty check and BOTH parses live inside the try, so
        // no future call site can throw out of here. An NPE here would abort
        // provision() on the bg thread AFTER the trust key was written, which
        // is strictly worse than the symptom this method exists to fix.
        try {
            if (hostAndPort == null || hostAndPort.isEmpty()) {
                Log2.i("POST /phone skipped: no host address known");
                return;
            }
            String[] hp = hostAndPort.split(":");
            String host = hp[0];
            int port = hp.length > 1 ? Integer.parseInt(hp[1].trim()) : 7911;
            int myPort = actualAdbPort();
            if (myPort <= 0) {
                Log2.i("POST /phone skipped: adb port unverified (" + myPort
                        + "); reporting a guess would point the host at a port "
                        + "that is not listening");
                return;
            }
            String body = "{\"ip\":\"" + myIp() + "\",\"port\":" + myPort + "}";
            HttpURLConnection c = (HttpURLConnection)
                    new URL("http://" + host + ":" + port + "/phone").openConnection();
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setConnectTimeout(3000);
            c.setReadTimeout(4000);
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("User-Agent", "android-webcam/1.0");
            try (java.io.OutputStream os = c.getOutputStream()) {
                os.write(body.getBytes(StandardCharsets.UTF_8));
            }
            int code = c.getResponseCode();
            Log2.i("POST /phone ip=" + myIp() + " port=" + myPort + " -> " + code);
        } catch (Exception e) {
            Log2.i("POST /phone failed: " + e);
        }
    }

    // -------------------------------------------------------- provision

    private void provision(String key, String how, String hostFrom) {
        bg.execute(() -> {
            if (!hasRoot()) {
                ui.post(() -> {
                    statusView.setText("Found your computer, but this app needs root access "
                            + "to register the trust key.\n\nOpen the app's settings and grant "
                            + "root, then tap “Set up connection” below.");
                    spinner.bar.setVisibility(View.GONE);
                    manualBox.setVisibility(View.VISIBLE);
                });
                keepAwake(false);   // no root: cannot provision, stop holding
                return;
            }

            String hook = detectHookDir();
            // Strip the trailing comment and single-quote for the shell.
            String k = key.split("\\s+")[0].replaceAll("(@.*)$", "")
                           .replace("'", "'\\''");
            StringBuilder cmd = new StringBuilder();
            cmd.append("mkdir -p $(dirname ").append(KEYS).append("); ");
            cmd.append("[ -f ").append(KEYS).append(" ] || : > ").append(KEYS).append("; ");
            // IDEMPOTENCY: append ONLY when absent. An unconditional append adds
            // a duplicate line on every run and grows adb_keys without bound.
            cmd.append("grep -qxF '").append(k).append("' ").append(KEYS)
               .append(" 2>/dev/null || printf '%s\\n' '").append(k).append("' >> ")
               .append(KEYS).append("; ");
            // adbd silently ignores a mislabelled/wrongly-owned trust store, which
            // looks identical to "pairing still broken".
            cmd.append("chown root:shell ").append(KEYS).append(" 2>/dev/null; ");
            cmd.append("chmod 0640 ").append(KEYS).append(" 2>/dev/null; ");
            cmd.append("restorecon ").append(KEYS).append(" 2>/dev/null; ");
            // Pin the port. BOTH names: AOSP adbd reads service.*, init's restart
            // trigger keys on the same one. Setting only persist.* can leave adbd
            // not listening at all, silently.
            cmd.append("setprop service.adb.tcp.port ").append(ADB_PORT).append("; ");
            cmd.append("setprop persist.adb.tcp.port ").append(ADB_PORT).append("; ");
            cmd.append("stop adbd; start adbd; ");
            // Persist at boot into whichever hook dir this ROM uses.
            cmd.append("mkdir -p ").append(hook).append("; ");            cmd.append("printf '%s\\n' '#!/system/bin/sh' ")
               .append("'setprop service.adb.tcp.port ").append(ADB_PORT).append("' ")
               .append("'setprop persist.adb.tcp.port ").append(ADB_PORT).append("' ")
               .append("'stop adbd' 'start adbd' > ").append(hook).append("/adb_tcp.sh; ");
            cmd.append("chmod 0755 ").append(hook).append("/adb_tcp.sh");
            su(cmd.toString());
            // Advertise the port we ACTUALLY ended up on. The host has no way
            // to learn it otherwise: this adb build has no mdns support and
            // avahi often sees nothing, so the host would otherwise guess 5555
            // against a phone listening on a random port and fail forever.
            final int advPort = actualAdbPort();
            if (advPort > 0) {
                advertiseAdb(advPort);
                Log2.i("advertising adb on port " + advPort);
            } else {
                Log2.i("not advertising: adb port unverified");
            }
            // Close the loop: tell the host this phone's real ip:port so it
            // connects here instead of guessing 5555 against a random port.
            reportToHost(hostFrom);

            boolean wrote = su("grep -qxF '" + k + "' " + KEYS + " && echo yes || echo no")
                            .contains("yes");
            boolean hookWrote = su("test -s " + hook + "/adb_tcp.sh && echo yes || echo no")
                            .contains("yes");

            // VERIFY before claiming success. `start adbd` can no-op behind a
            // pending root prompt, so a green message without this is a lie.
            boolean listening = false;
            for (int i = 0; i < 24 && !listening; i++) {
                listening = adbdListening();
                if (!listening) {
                    try { Thread.sleep(250); } catch (InterruptedException ignored) {}
                }
            }
            final boolean isListening = listening;
            // Report the port the DEVICE is actually on, not the one we asked
            // for. On Android 13 wireless debugging keeps a random port, and
            // `setprop service.adb.tcp.port 5555` may not take effect at all.
            final int realPort = actualAdbPort();
            final String fHow = how;
            ui.post(() -> {
                spinner.bar.setVisibility(View.GONE);
                if (wrote && hookWrote && isListening) {
                    statusView.setText("Ready.\n\nYour computer can now use this phone as a "
                            + "webcam with no pairing. Press Start on the computer.");
                    detailView.setText("This phone: " + myIp()
                            + (realPort > 0 ? ":" + realPort
                               : "  (adb port unverified - the socket read failed, "
                               + "so no port is being reported)")
                            + "  ·  found via " + fHow
                            + "  ·  trust store " + countKeys() + " key(s)");
                } else {
                    statusView.setText("Not finished yet:\n"
                            + "  trusted key written: " + (wrote ? "yes" : "NO") + "\n"
                            + "  boot hook written:   " + (hookWrote ? "yes" : "NO") + "\n"
                            + "  adbd listening:      " + (isListening ? "yes" : "NO")
                            + " on port " + realPort
                            + "\n\nIf root was just granted, wait a few seconds and reopen.");
                }
            });
            Log2.i("provision wrote=" + wrote + " hook=" + hookWrote
                    + " listening=" + isListening + " on port " + realPort);
            // Last statement of the work that matters, not of its caller:
            // the caller is already ON the single-thread bg executor, so a
            // release there fires before any of this has run.
            keepAwake(false);   // provisioned; onDestroy is the backstop
        });
    }

    // ----------------------------------------------------------- widgets

    private TextView text(String s, int sizeSp) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sizeSp);
        return t;
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density);
    }

    private LinearLayout.LayoutParams lp(int margin) {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        p.topMargin = margin;
        return p;
    }

    private LinearLayout.LayoutParams lpTop(int margin) {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        p.topMargin = margin;
        return p;
    }
}
