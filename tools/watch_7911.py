#!/usr/bin/env python3
"""Watch for SYNs to 7911 on this host, without tcpdump and without sudo.

tcpdump is not installed here, and the question is narrow enough to answer
without it: does a packet from the phone's source port range arrive at all?

A raw AF_PACKET socket needs CAP_NET_RAW (root). This binds a normal TCP
socket to 7911 instead: the kernel completes or refuses the handshake, and
either way we learn something. Comparing that against the phone's own
reported result is the whole test.

Usage:  sudo ./watch_7911.py         (or run as a normal user to see RSTs)
"""
import socket
import sys
import time

PORT = 7911
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0


def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))          # every interface, incl. 192.168.1.13
    srv.listen(16)
    srv.settimeout(1.0)
    print(f"listening on 0.0.0.0:{PORT} for {DURATION:.0f}s", flush=True)
    print("now open the phone app; this prints the peer for every connection",
          flush=True)

    end = time.monotonic() + DURATION
    seen = 0
    while time.monotonic() < end:
        try:
            conn, peer = srv.accept()
        except socket.timeout:
            continue
        except OSError as e:
            print(f"  accept failed: {e}", flush=True)
            continue
        seen += 1
        print(f"  CONNECTION from {peer[0]}:{peer[1]}", flush=True)
        conn.close()
    print(f"done: {seen} connection(s) observed", flush=True)
    if seen == 0:
        print("no TCP connection completed in this window - the SYN did not "
              "become an established session", flush=True)


if __name__ == "__main__":
    main()
