package dev.ishanp.androidwebcam;

import android.content.Context;
import android.util.Log;

/**
 * Diagnostic log, written where BOTH logcat and adb can read it:
 *   logcat -s AWCam                       (always available)
 *   adb shell cat /sdcard/Android/data/dev.ishanp.androidwebcam/files/aw.log
 *
 * Exists because "Couldn't find your computer" is a symptom with a dozen
 * possible causes, and guessing between them costs the user a reinstall each
 * time. Every step of discovery records what it actually observed.
 */
final class Log2 {
    private static final String TAG = "AWCam";
    private static final int MAX_BYTES = 256 * 1024;

    /** Set from onCreate; null before that. */
    private static Context appCtx;

    private Log2() {}

    static void init(Context c) {
        appCtx = c.getApplicationContext();
        // Each launch starts a FRESH log. Previously the file only rotated at
        // 256KB, so a new run was appended straight after the previous one with
        // no separator, and a pasted log mixed runs from different times into
        // something that read as one coherent session. Two wrong conclusions
        // in this session came from exactly that. Truncate here, once per
        // launch, and mark the boundary so an old tail is never mistaken for
        // the current run.
        resetForNewRun();
    }

    /** Wipe the previous run and stamp a header for this one. */
    private static synchronized void resetForNewRun() {
        java.io.File f = logFile();
        if (f == null) return;
        try {
            String head = "===== new run "
                    + new java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss")
                            .format(new java.util.Date()) + " =====\n";
            java.nio.file.Files.write(f.toPath(),
                    head.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        } catch (Exception e) {
            Log.w(TAG, "could not reset log: " + e);
        }
    }

    /**
     * getExternalFilesDir(null) is app-private and needs NO storage permission
     * on any API level, and stays adb-readable. A bare /sdcard path does NOT:
     * on API 29+ scoped storage rejects it, and a swallowed exception would
     * look exactly like "no logs were produced".
     */
    private static java.io.File logFile() {
        if (appCtx == null) return null;
        java.io.File d = appCtx.getExternalFilesDir(null);
        if (d == null) d = appCtx.getFilesDir();     // internal fallback
        if (d == null) return null;
        return new java.io.File(d, "aw.log");
    }

    static void i(String msg) {
        Log.i(TAG, msg);
        append(msg);
    }

    private static synchronized void append(String msg) {
        java.io.File f = logFile();
        if (f == null) { Log.w(TAG, "no writable log dir"); return; }
        try {
            if (f.exists() && f.length() > MAX_BYTES) {
                // Keep the tail; it is the part that matters.
                String all = new String(java.nio.file.Files.readAllBytes(f.toPath()),
                        java.nio.charset.StandardCharsets.UTF_8);
                all = all.substring(Math.max(0, all.length() - MAX_BYTES / 2));
                java.nio.file.Files.write(f.toPath(), all.getBytes(
                        java.nio.charset.StandardCharsets.UTF_8));
            }
            String line = new java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.US)
                    .format(new java.util.Date()) + "  " + msg + "\n";
            java.nio.file.Files.write(f.toPath(), line.getBytes(
                    java.nio.charset.StandardCharsets.UTF_8),
                    java.nio.file.StandardOpenOption.CREATE,
                    java.nio.file.StandardOpenOption.APPEND);
        } catch (Throwable t) {
            // Report the reason instead of swallowing it - a silent failure here
            // is indistinguishable from "logging never worked".
            Log.w(TAG, "log write failed: " + t);
        }
    }

    /** Where the log lives, so the UI can show it. */
    static String logPath() {
        java.io.File f = logFile();
        return f == null ? "(no writable dir)" : f.getAbsolutePath();
    }

    /** Every network interface with its addresses - the first thing to check. */
    static void dumpInterfaces() {
        try {
            java.util.Enumeration<java.net.NetworkInterface> ifs =
                    java.net.NetworkInterface.getNetworkInterfaces();
            StringBuilder sb = new StringBuilder("interfaces:");
            while (ifs.hasMoreElements()) {
                java.net.NetworkInterface n = ifs.nextElement();
                sb.append("\n  ").append(n.getName())
                  .append(" up=").append(n.isUp())
                  .append(" loop=").append(n.isLoopback())
                  .append(" addrs=");
                try {
                    for (java.net.InterfaceAddress ia : n.getInterfaceAddresses()) {
                        sb.append(ia.getAddress().getHostAddress())
                          .append('/').append(ia.getNetworkPrefixLength()).append(' ');
                    }
                } catch (Exception ignored) { sb.append("?"); }
            }
            i(sb.toString());
        } catch (Exception e) {
            i("dumpInterfaces failed: " + e);
        }
    }
}
