package dev.ishanp.androidwebcam;

import android.app.Activity;
import android.content.Context;
import android.graphics.Color;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.TypedValue;
import android.view.View;
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
            provision(k, "manual entry");
        });
        manualBox.addView(setupBtn, lpTop(dp(12)));
        col.addView(manualBox);

        scroll.addView(col);
        return scroll;
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

    /** This phone's IPv4 on the interface that actually has a prefix length. */
    private String myIp() {
        try {
            Enumeration<NetworkInterface> ifs = NetworkInterface.getNetworkInterfaces();
            while (ifs.hasMoreElements()) {
                NetworkInterface n = ifs.nextElement();
                if (!n.isUp() || n.isLoopback()) continue;
                Enumeration<InetAddress> addrs = n.getInetAddresses();
                while (addrs.hasMoreElements()) {
                    InetAddress a = addrs.nextElement();
                    if (a instanceof Inet4Address && !a.isLinkLocalAddress()) {
                        return a.getHostAddress();
                    }
                }
            }
        } catch (Exception ignored) {}
        return null;
    }

    /**
     * The real network prefix length, so a fallback scan covers the subnet the
     * phone is actually on instead of assuming a /24.
     */
    private int prefixLength() {
        try {
            Enumeration<NetworkInterface> ifs = NetworkInterface.getNetworkInterfaces();
            while (ifs.hasMoreElements()) {
                NetworkInterface n = ifs.nextElement();
                if (!n.isUp() || n.isLoopback()) continue;
                Enumeration<InetAddress> addrs = n.getInetAddresses();
                while (addrs.hasMoreElements()) {
                    InetAddress a = addrs.nextElement();
                    if (a instanceof Inet4Address && !a.isLinkLocalAddress()) {
                        // getNetworkPrefixLength() is API 24+, but it is hidden
                        // in the android.jar stub used to compile, so call it
                        // reflectively. Falls back to /24 if absent.
                        try {
                            Object v = NetworkInterface.class
                                    .getMethod("getNetworkPrefixLength").invoke(n);
                            if (v instanceof Integer) return (Integer) v;
                        } catch (Exception ignored) {}
                        return 24;
                    }
                }
            }
        } catch (Exception ignored) {}
        return 24;
    }

    /**
     * Ask whether something is LISTENing on the adb port. /proc/net/tcp is
     * world-readable, so this costs no root - unlike shelling out to su twenty
     * times, which re-prompts for the Magisk/KernelSU dialog each round.
     */
    private boolean adbdListening() {
        final String want = String.format("%04X", ADB_PORT);
        for (String f : new String[]{"/proc/net/tcp", "/proc/net/tcp6"}) {
            try (BufferedReader r = new BufferedReader(new InputStreamReader(
                    new java.io.FileInputStream(f)))) {
                String line;
                boolean first = true;
                while ((line = r.readLine()) != null) {
                    if (first) { first = false; continue; }   // header
                    String[] col = line.trim().split("\\s+");
                    if (col.length < 4) continue;
                    // sl is hex "local_address", st is hex "0A" == LISTEN
                    if (col[1].contains(":") && col[1].split(":")[1].equals(want)
                            && col[3].equals("0A")) {
                        return true;
                    }
                }
            } catch (Exception ignored) {}
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
            // NsdManager.PROTO_DNS_SD is 0; the constant is hidden in the
            // android.jar stub used to compile, so pass the literal.
            mgr.discoverServices(SERVICE_TYPE, 0, dl);
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
        try {
            HttpURLConnection c = (HttpURLConnection)
                    new URL("http://" + host + ":" + port + "/adbkey.pub").openConnection();
            c.setConnectTimeout(2500);
            c.setReadTimeout(4000);
            if (c.getResponseCode() != 200) return null;
            try (InputStream in = c.getInputStream()) {
                String s = new String(in.readAllBytes(), StandardCharsets.UTF_8).trim();
                return (s.startsWith("Q") && s.length() > 100) ? s : null;
            }
        } catch (Exception e) {
            return null;
        }
    }

    /** Candidate hosts on our own subnet, bounded and excluding ourselves. */
    private List<String> scanSubnet(String myAddr) {
        List<String> out = new ArrayList<>();
        int plen = Math.max(16, Math.min(prefixLength(), 24));
        int mask = plen == 16 ? 0xFFFF0000 : (plen == 24 ? 0xFFFFFF00 : 0xFFFFC000);
        int mine = inetToInt(myAddr);
        int net = mine & mask;
        for (int i = 1; i < 255 && out.size() < 300; i++) {
            int cand = net + i;
            if (cand == mine) continue;
            out.add(intToInet(cand));
        }
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

    // ------------------------------------------------------------- flow

    private void autoConnect() {
        final String addr = myIp();
        ui.post(() -> addrView.setText("This phone: "
                + (addr == null ? "no WiFi address" : addr + ":" + ADB_PORT)));

        bg.execute(() -> {
            // Fast path: already provisioned on a PREVIOUS run. It must check
            // that a trusted key is actually present, not merely that adbd
            // happens to be listening: 5555 is also where the phone's own
            // wireless-debugging daemon listens, so "adbd is up" alone would
            // report Ready with an empty trust store - the exact stuck state.
            if (hasRoot() && adbdListening() && countKeys() > 0) {
                int keys = countKeys();
                ui.post(() -> done("Ready — your computer can use this phone as a webcam. "
                        + keys + " trusted computer(s)."));
                return;
            }
            if (hasRoot() && adbdListening() && countKeys() == 0) {
                ui.post(() -> statusView.setText(
                        "This phone is listening on " + ADB_PORT
                        + " but has no trusted computer. Press “Trust this computer” "
                        + "on your computer, then reopen this app."));
                return;
            }

            if (addr == null) {
                ui.post(() -> statusView.setText(
                        "This phone is not on WiFi. Join the same WiFi as your computer, then reopen."));
                return;
            }

            say("Looking for your computer…");
            // The scan is the PRIMARY path: it is the one that is actually
            // proven to work. mDNS is tried first because when it works it is
            // instant and unambiguous, but a host may not publish a resolvable
            // mDNS record at all, so it must never be the only way in.
            String host = discoverViaNsd();
            String how = "automatic discovery (mDNS)";

            if (host == null) {
                say("Scanning this network for your computer…");
                // Bounded concurrency on the provisioning port only.
                java.util.concurrent.ExecutorService pool =
                        Executors.newFixedThreadPool(64);
                List<String> cands = scanSubnet(addr);
                List<Future<Boolean>> fs = new ArrayList<>();
                for (final String c : cands) {
                    fs.add(pool.submit(() -> {
                        try (Socket s = new Socket()) {
                            s.connect(new InetSocketAddress(c, 7911), 800);
                            return true;
                        } catch (Exception e) { return false; }
                    }));
                }
                List<String> live = new ArrayList<>();
                for (int i = 0; i < fs.size(); i++) {
                    try { if (fs.get(i).get()) live.add(cands.get(i)); } catch (Exception ignored) {}
                }
                pool.shutdownNow();
                for (String c : live) {
                    String k = fetchKeyFrom(c + ":7911");
                    if (k != null) { host = c + ":7911"; break; }
                }
                how = "network scan";
            }

            if (host == null) {
                ui.post(() -> {
                    statusView.setText("Couldn't find your computer.\n\n"
                            + "On the computer, open the Android Webcam app and press "
                            + "“Trust this computer”, then reopen this app.");
                    spinner.bar.setVisibility(View.GONE);
                    manualBox.setVisibility(View.VISIBLE);
                });
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
                return;
            }

            say("Found your computer at " + host + " (" + how + ")");
            provision(key, how);
        });
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

    // -------------------------------------------------------- provision

    private void provision(String key, String how) {
        bg.execute(() -> {
            if (!hasRoot()) {
                ui.post(() -> {
                    statusView.setText("Found your computer, but this app needs root access "
                            + "to register the trust key.\n\nOpen the app's settings and grant "
                            + "root, then tap “Set up connection” below.");
                    spinner.bar.setVisibility(View.GONE);
                    manualBox.setVisibility(View.VISIBLE);
                });
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
            cmd.append("mkdir -p ").append(hook).append("; ");
            cmd.append("printf '%s\\n' '#!/system/bin/sh' ")
               .append("'setprop service.adb.tcp.port ").append(ADB_PORT).append("' ")
               .append("'setprop persist.adb.tcp.port ").append(ADB_PORT).append("' ")
               .append("'stop adbd' 'start adbd' > ").append(hook).append("/adb_tcp.sh; ");
            cmd.append("chmod 0755 ").append(hook).append("/adb_tcp.sh");
            su(cmd.toString());

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
            final String fHow = how;
            ui.post(() -> {
                spinner.bar.setVisibility(View.GONE);
                if (wrote && hookWrote && isListening) {
                    statusView.setText("Ready.\n\nYour computer can now use this phone as a "
                            + "webcam with no pairing. Press Start on the computer.");
                    detailView.setText("This phone: " + myIp() + ":" + ADB_PORT
                            + "  ·  found via " + fHow
                            + "  ·  trust store " + countKeys() + " key(s)");
                } else {
                    statusView.setText("Not finished yet:\n"
                            + "  trusted key written: " + (wrote ? "yes" : "NO") + "\n"
                            + "  boot hook written:   " + (hookWrote ? "yes" : "NO") + "\n"
                            + "  listening on " + ADB_PORT + ":   " + (isListening ? "yes" : "NO")
                            + "\n\nIf root was just granted, wait a few seconds and reopen.");
                }
            });
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
