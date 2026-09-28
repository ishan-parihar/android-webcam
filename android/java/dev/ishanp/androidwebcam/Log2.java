package dev.ishanp.androidwebcam;

import android.util.Log;

/**
 * Append-only diagnostic log, world-readable so the desktop can pull it over
 * adb without root:  adb shell cat /sdcard/android-webcam.log
 *
 * Exists because "Couldn't find your computer" is a symptom with a dozen
 * possible causes, and guessing between them costs the user a reinstall each
 * time. Every step of discovery writes here what it actually observed.
 */
final class Log2 {
    private static final String TAG = "AWCam";
    private static final String FILE = "/sdcard/android-webcam.log";
    private static final int MAX_BYTES = 256 * 1024;

    private Log2() {}

    static void i(String msg) {
        Log.i(TAG, msg);
        append(msg);
    }

    private static synchronized void append(String msg) {
        try {
            java.io.File f = new java.io.File(FILE);
            if (f.exists() && f.length() > MAX_BYTES) {
                // Keep it bounded; keep the tail, which is the useful part.
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
        } catch (Throwable ignored) {
            // Never let logging break the app; Logcat still has it.
        }
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
