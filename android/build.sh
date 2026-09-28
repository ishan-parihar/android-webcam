#!/usr/bin/env bash
# Build the phone-side APK with the raw Android SDK toolchain.
#
# No Gradle, no Kotlin, no AndroidX, no network. Uses only what ships in
# $ANDROID_SDK: aapt2 -> javac -> d8 -> apksigner. Everything is plain Java
# against the platform APIs, so the APK is tiny and has no supply chain.
set -euo pipefail

SDK="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/Android/Sdk}}"
HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="$HERE/build"
OUT="${1:-$HERE/dist/android-webcam.apk}"

BT="$(ls -d "$SDK"/build-tools/*/ | sort -V | tail -1)"; BT="${BT%/}"
PLATFORM="$(ls -d "$SDK"/platforms/*/ | sort -V | tail -1)"; PLATFORM="${PLATFORM%/}"
ANDROID_JAR="$PLATFORM/android.jar"

[ -f "$ANDROID_JAR" ] || { echo "no android.jar in $PLATFORM" >&2; exit 1; }
for t in aapt2 d8 apksigner zipalign; do
  [ -x "$BT/$t" ] || { echo "missing $BT/$t" >&2; exit 1; }
done

echo "==> toolchain: $BT / $PLATFORM"
rm -rf "$BUILD"; mkdir -p "$BUILD/compiled" "$BUILD/dex" "$BUILD/gen" "$(dirname "$OUT")"

echo "==> aapt2: compile + link resources"
"$BT/aapt2" compile --dir "$HERE/res" -o "$BUILD/res.zip"
"$BT/aapt2" link \
  -I "$ANDROID_JAR" \
  --manifest "$HERE/AndroidManifest.xml" \
  --java "$BUILD/gen" \
  -o "$BUILD/base.apk" \
  --min-sdk-version 29 --target-sdk-version 34 \
  "$BUILD/res.zip"

echo "==> javac"
find "$HERE/java" "$BUILD/gen" -name '*.java' > "$BUILD/sources.txt"
# Clear stale output first: `set -e` plus the `| grep` pipe below cannot see
# javac's exit status, so a previous build's .class files would otherwise
# satisfy the check below and produce an APK built from OLD sources.
rm -rf "$BUILD/compiled"
# Capture output to a file rather than piping: a pipe discards javac's exit
# status, and `|| true` on top of that hid real compile errors entirely.
set +e
javac -nowarn -source 17 -target 17 \
  -cp "$ANDROID_JAR" -d "$BUILD/compiled" @"$BUILD/sources.txt" \
  > "$BUILD/javac.log" 2>&1
javac_rc=$?
set -e
grep -v 'bootstrap class path' "$BUILD/javac.log" | head -20 || true
# Fail on the error lines themselves, not just on a missing .class file.
if [ "$javac_rc" -ne 0 ] || grep -qE '(^|[^a-zA-Z])error:' "$BUILD/javac.log"; then
  echo "COMPILE FAILED (rc=$javac_rc)" >&2
  exit 1
fi
[ -f "$BUILD/compiled/dev/ishanp/androidwebcam/MainActivity.class" ] || {
  echo "compile failed: no MainActivity.class" >&2; exit 1; }

echo "==> d8: classes -> dex"
find "$BUILD/compiled" -name '*.class' > "$BUILD/classes.txt"
"$BT/d8" --lib "$ANDROID_JAR" --min-api 29 --output "$BUILD/dex" @"$BUILD/classes.txt"

echo "==> package"
# Do NOT unzip/rezip. aapt2 emits resources.arsc and AndroidManifest.xml as
# STORED entries, and `zip -r` would re-deflate them. A compressed
# resources.arsc makes PackageManager fail to parse the APK and the installer
# reports the generic "App not installed" with no reason. Instead copy the
# linked APK and add the dex with `zip`'s update mode, which leaves existing
# entries (and their compression + alignment) untouched.
cp "$BUILD/base.apk" "$BUILD/unsigned.apk"
( cd "$BUILD/dex" && zip -q "$BUILD/unsigned.apk" classes.dex )
"$BT/zipalign" -f -p 4 "$BUILD/unsigned.apk" "$BUILD/aligned.apk"

# Guard the invariant aapt2/zipalign are supposed to guarantee, so a future
# packaging change cannot silently regress it. `unzip -v` prints the method as
# "Stored" (capital S), which is why an earlier lowercase check produced a false
# alarm on a perfectly good APK.
arsc_method() { unzip -v "$1" 2>/dev/null | awk '$NF=="resources.arsc"{print $2; exit}'; }
case "$(arsc_method "$BUILD/aligned.apk")" in
  Stored|stored) echo "    resources.arsc: Stored (ok)" ;;
  *) echo "    FATAL: resources.arsc method = '$(arsc_method "$BUILD/aligned.apk")'" \
       "(must be Stored) - installer will reject this APK" >&2; exit 1 ;;
esac

echo "==> sign"
# min-sdk 29 means v1/v2 are redundant (they only gate API<24), so apksigner
# emits v3 alone and correctly reports v1/v2 as false. Do not "fix" that.
KS="$HERE/keystore.jks"
if [ ! -f "$KS" ]; then
  keytool -genkeypair -keystore "$KS" -storepass android -keypass android \
    -alias androidwebcam -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Webcam, OU=ishanp, O=Android Webcam, C=IN" >/dev/null 2>&1
  echo "    (generated $KS)"
fi
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:android --key-pass pass:android \
  --min-sdk-version 29 --out "$OUT" "$BUILD/aligned.apk"
"$BT/apksigner" verify --min-sdk-version 29 "$OUT" >/dev/null
"$BT/aapt2" dump badging "$OUT" | head -1

echo "==> built: $OUT"
ls -lh "$OUT" | awk '{print "    "$5, $9}'
