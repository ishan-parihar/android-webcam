#!/usr/bin/env python3
"""Watch 7911 for connections AND measure whether packets arrive at all.

tcpdump is not installed here, and a raw AF_PACKET socket needs root, so this
cannot see a SYN that is later dropped - it only observes connections that
COMPLETE. On its own it therefore cannot separate "the SYN never arrived"
from "it arrived and was dropped", which is the whole ambiguity.

A raw AF_PACKET socket needs root and tcpdump is not installed here, so this
CANNOT see a SYN that is later dropped. It reports one thing only: whether a
TCP session was established. Silence here means no session, and the reason
must come from the phone's own route/neigh lines or a privileged capture.

The counters are printed for context but CANNOT attribute a segment to port
7911. TcpInSegs is system-wide, so this host's own SSH, DNS and background
scans move it during any window. A delta of zero or a large delta are both
uninformative about the phone. They are printed, clearly labelled, only so
nobody re-derives the same dead end.

Usage:  python3 watch_7911.py [seconds] [--nstat]
"""
import socket
import sys
import time

PORT = 7911


def tcp_counters() -> dict[str, int]:
    """Read the kernel TCP counters. /proc/net/snmp is world-readable.

    The field names are BARE within each section - "InSegs", not
    "TcpInSegs" - and the section name is the line prefix. /proc/net/netstat
    reads as 0 bytes here, so it cannot be relied on for the TcpExt values.
    """
    out: dict[str, int] = {}
    try:
        lines = open("/proc/net/snmp").read().strip().split("\n")
    except Exception:
        return out
    for i in range(0, len(lines) - 1, 2):
        hdr, val = lines[i].split(), lines[i + 1].split()
        if len(hdr) != len(val):
            continue
        section = hdr[0].rstrip(":")
        if section != "Tcp":
            continue
        for k, v in zip(hdr[1:], val[1:]):
            try:
                out[f"{section}{k}"] = int(v)
            except ValueError:
                pass
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    want_nstat = "--nstat" in sys.argv
    duration = float(args[0]) if args else 120.0

    before = tcp_counters()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))          # every interface, incl. 192.168.1.13
    srv.listen(16)
    srv.settimeout(1.0)
    print(f"listening on 0.0.0.0:{PORT} for {duration:.0f}s", flush=True)
    print("now open the phone app", flush=True)

    end = time.monotonic() + duration
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

    after = tcp_counters()
    print(f"done: {seen} connection(s) observed", flush=True)
    if want_nstat:
        segs = after.get("TcpInSegs", 0) - before.get("TcpInSegs", 0)
        outr = after.get("TcpOutRsts", 0) - before.get("TcpOutRsts", 0)
        print(f"  [context only, NOT attributable to port 7911]", flush=True)
        print(f"    TcpInSegs system-wide  +{segs}", flush=True)
        print(f"    TcpOutRsts system-wide +{outr}", flush=True)
        print("  TcpInSegs is system-wide: this host's own traffic moves it, so "
              "no reading here proves or disproves anything about the phone.",
              flush=True)
    if seen == 0:
        print("  RESULT: no session was established. That is the only thing "
              "this tool can conclude; the cause is not visible here.",
              flush=True)


if __name__ == "__main__":
    main()
