#!/system/bin/sh
# adb_tcp.sh - pin adbd to a FIXED TCP port so the host never has to discover it.
#
# Why: Android wireless debugging assigns a RANDOM high port per session
# (yours is 43671, published via _adb-tls-connect._tcp over mDNS). Measured on
# this LAN, that advertisement is intermittent - 2 hits, then 0/4 - so any
# host-side flow that depends on mDNS works only sometimes. Pinning the port
# removes discovery from the critical path entirely.
#
# Install (Termux, with root via `tsu`):
#   su -c 'mkdir -p /data/adb/service.d'
#   # copy this file to /data/adb/service.d/adb_tcp.sh, then:
#   su -c 'chmod 755 /data/adb/service.d/adb_tcp.sh'
# Reboot. adbd then listens on 5555 forever.
#
# Verify from the host:  adb connect <phone-ip>:5555
# 5555 is the adb bind-all-interfaces port, so this also works over WiFi
# without wireless debugging being enabled at all.
set -u

PORT="${ADB_TCP_PORT:-5555}"

# Persist across reboots. persist.* is written to /data/property and survives;
# a plain setprop would be lost on restart.
setprop persist.adb.tcp.port "$PORT"

# adbd only opens the TCP listener on a fresh start, so bounce it.
stop adbd
start adbd

# Wait (briefly) for the listener to come up so a caller that immediately
# connects does not lose the race. /proc/net/tcp stores ports as UPPERCASE HEX
# with no "tcp:" prefix, e.g. 0100007F:15B3 - match on that or the poll can
# never succeed and would report a working adbd as broken.
HEX_PORT=$(printf '%04X' "$PORT")
i=0
while [ "$i" -lt 20 ]; do
    if awk -v p=":$HEX_PORT" '$4=="0A" && $2 ~ p"$" {found=1} END{exit !found}' /proc/net/tcp 2>/dev/null; then
        echo "adb_tcp: adbd listening on $PORT"
        exit 0
    fi
    i=$((i + 1))
    sleep 0.25
done

echo "adb_tcp: WARNING - adbd did not report port $PORT within 5s" >&2
echo "adb_tcp: check with: adb connect <phone-ip>:$PORT" >&2
exit 1
