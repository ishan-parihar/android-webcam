#!/bin/sh
# Print the host public key for pasting into the phone app (Android Webcam).
# The phone app needs ~/.android/adbkey.pub to trust THIS computer permanently.
echo "Copy everything below into the phone app's 'Host public key' field:"
echo
cat "$HOME/.android/adbkey.pub"
echo
echo "Install the app on the phone from: scripts/../android/dist/android-webcam.apk"
