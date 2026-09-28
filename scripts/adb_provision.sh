#!/system/bin/sh
# adb_provision.sh - one-time root setup so this host NEVER needs adb pairing again.
#
# WHY ROOT MATTERS
#   adb pairing exists to prove trust over a channel anyone can join. The RSA
#   consent dialog lives in System UI, so no APK and no adb flag can accept it.
#   But with root we can write this host's public key directly into the phone's
#   trust store:
#       /data/misc/adb/adb_keys
#   adbd then treats this host as already-authorised: no pairing screen, no
#   6-digit code, no port to copy, forever. That is the only way to make
#   pairing genuinely automatic.
#
# WHAT IT DOES
#   1. pushes the host adb public key into /data/misc/adb/adb_keys (backed up)
#   2. pins adbd to a FIXED port so wireless debugging can auto-off
#   3. re-applies both at every boot via the root framework's boot-hook dir
#      (Magisk: /data/adb/service.d, KernelSU/APatch: /data/adb/post-fs-data.d
#      - detected at runtime, not assumed)
#
# INSTALL (from the HOST). Two console-free-ish routes; prefer the first once
# you have ANY working adb path, otherwise the second:
#   (a) adb push  (works today only if already paired)
#       adb push adb_provision.sh /data/local/tmp/
#       adb shell su -c "sh /data/local/tmp/adb_provision.sh '$(cat ~/.android/adbkey.pub)'"
#   (b) MTP / Download folder + Termux (no adb needed at all)
#       copy the file to the phone's Download/, then in Termux:
#         su -c "sh \$PREFIX/files/home/../shared/Download/adb_provision.sh '...'"
#       NOTE: Termux cannot read /data/local/tmp (SELinux), so for (a) move it
#       into $PREFIX first:
#         adb shell su -c "cp /data/local/tmp/adb_provision.sh \$PREFIX/bin/"
#         adb shell su -c "sh \$PREFIX/bin/adb_provision.sh '...'"
#
# VERIFY (host):
#   adb kill-server && adb connect <phone-ip>:5555 && adb devices -l
#   -> the device must appear WITHOUT ever opening the pairing screen.
set -u

PORT="${ADB_TCP_PORT:-5555}"
KEYS=/data/misc/adb/adb_keys

log() { echo "adb_provision: $*"; }
die() { echo "adb_provision: FATAL: $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "must run as root (su -c ... in Termux)"

mkdir -p "$(dirname "$KEYS")"
[ -f "$KEYS" ] || : > "$KEYS"
[ -f "$KEYS.bak" ] || cp "$KEYS" "$KEYS.bak" 2>/dev/null || true

# --- 1. trust this host -------------------------------------------------
# Pass the host's public key in $1 (adbkey.pub contents) or on stdin.
PUBKEY="${1:-}"
[ -z "$PUBKEY" ] && PUBKEY="$(cat 2>/dev/null || true)"
[ -n "$PUBKEY" ] || die "no host public key given (arg 1 or stdin)"

# adb_keys stores the base64 key; a trailing "@user@host" comment is tolerated
# but stripped here so the comparison below is exact.
KEY=$(printf '%s' "$PUBKEY" | awk '{print $1}' | sed 's/@.*$//')
[ -n "$KEY" ] || die "could not parse host public key"

# APPEND ONLY, never truncate. Clobbering /data/misc/adb/adb_keys locks you out
# of adb entirely - and on a phone reachable only over the network that is
# unrecoverable without a reflash. grep -qxF guards against duplicates.
#
# `su` can be granted but SCOPED (e.g. KernelSU scoped to the app's own files),
# so trust the write itself, not the return of `su`. If this fails, say so
# plainly and let the caller fall back to the pairing screen.
if printf '%s\n' "$KEY" >> "$KEYS" 2>/dev/null; then
    if grep -qxF "$KEY" "$KEYS" 2>/dev/null; then
        log "host key present in $KEYS"
        chown root:shell "$KEYS" 2>/dev/null || chown system:system "$KEYS" 2>/dev/null || true
        chmod 0640 "$KEYS" 2>/dev/null || true
        restorecon "$KEYS" 2>/dev/null || true
    else
        log "trust store write did not persist - root may be scoped"
    fi
else
    log "cannot write $KEYS (root not granted for this path)"
fi

# --- 2. pin the port ----------------------------------------------------
# BOTH property names are required. AOSP adbd reads service.adb.tcp.port
# (main.cpp) and init's restart trigger is keyed on service.adb.tcp.port=*.
# Setting only persist.adb.tcp.port can leave adbd NOT LISTENING at all, and
# the failure is silent - the host just sees a closed port.
setprop service.adb.tcp.port "$PORT"
setprop persist.adb.tcp.port "$PORT"

# adbd reads its key set at startup, so the trust-store write above is only
# live AFTER a restart. Order matters: key first, then bounce.
stop adbd
start adbd

# --- 3. verify ----------------------------------------------------------
# /proc/net/tcp stores ports as UPPERCASE HEX, no "tcp:" prefix (e.g. 15B3).
HEX_PORT=$(printf '%04X' "$PORT")
i=0
while [ "$i" -lt 20 ]; do
    if awk -v p=":$HEX_PORT" '$4=="0A" && $2 ~ p"$" {f=1} END{exit !f}' /proc/net/tcp 2>/dev/null; then
        log "adbd listening on $PORT"
        break
    fi
    i=$((i + 1))
    sleep 0.25
done
[ "$i" -lt 20 ] || echo "adb_provision: WARNING - port $PORT not listening after 5s" >&2

# --- 4. persist at boot -------------------------------------------------
# /data/adb/service.d is a MAGISK-only convention. KernelSU and APatch use
# /data/adb/post-fs-data.d. Writing to the wrong one creates a directory
# nothing ever executes - the port silently stops being re-pinned after the
# next reboot, which looks exactly like "provisioned but broken". Detect which
# hook dir this ROM actually uses and install there, then VERIFY the file
# landed (an unexecutable directory is indistinguishable from success if you
# only check "wrote it").
HOOKDIR=""
for d in /data/adb/post-fs-data.d /data/adb/service.d; do
    if [ -d "$d" ]; then HOOKDIR="$d"; break; fi
done
# Neither exists: a ROM that is rooted always has one. Fall back to the
# post-fs-data convention (KernelSU/APatch/Magisk all honour it in practice)
# and say so, rather than pretending.
[ -n "$HOOKDIR" ] || HOOKDIR=/data/adb/post-fs-data.d

mkdir -p "$HOOKDIR" 2>/dev/null || true
if mkdir -p "$HOOKDIR" 2>/dev/null && [ -d "$HOOKDIR" ]; then
    cat > "$HOOKDIR/adb_tcp.sh" <<EOF
#!/system/bin/sh
# Generated by adb_provision.sh - re-assert the fixed adb port every boot.
# Both property names are required: AOSP adbd reads service.adb.tcp.port and
# init's restart trigger keys on it; persist.* alone can leave it not listening.
setprop service.adb.tcp.port $PORT
setprop persist.adb.tcp.port $PORT
stop adbd
start adbd
EOF
    chmod 0755 "$HOOKDIR/adb_tcp.sh" 2>/dev/null || true
    chown root:root "$HOOKDIR/adb_tcp.sh" 2>/dev/null || true
    if [ -s "$HOOKDIR/adb_tcp.sh" ]; then
        log "boot hook installed: $HOOKDIR/adb_tcp.sh"
    else
        log "WARNING - could not write $HOOKDIR/adb_tcp.sh (port will not persist)"
    fi
else
    log "WARNING - no boot-hook directory available; port pin is until-reboot only"
fi

log "done. From the host:  adb connect <phone-ip>:$PORT"
