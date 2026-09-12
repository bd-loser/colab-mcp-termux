#!/usr/bin/env python3
"""Launch the Colab MCP server with a DNS-over-HTTPS fallback.

This network's resolver drops colab.research.google.com (and may drop the
runtime proxy hosts), while the IPs themselves are reachable. We wrap
socket.getaddrinfo: on a local resolution failure, resolve via dns.google
over HTTPS (which does resolve) and return IPv4 results. TLS still uses the
original hostname for SNI/cert validation because only address lookup is
patched, never the hostname.
"""
import json
import socket
import threading
import urllib.parse
import urllib.request

_ORIG_GETADDRINFO = socket.getaddrinfo
_CACHE = {"dns.google": ["8.8.8.8", "8.8.4.4"]}  # pre-seeded: avoids recursion
_IN_DOH = threading.local()
# DoH endpoints tried in order; the IP forms work even when the resolver
# refuses dns.google itself (Google/Cloudflare certs cover the bare IPs).
_DOH_ENDPOINTS = (
    "https://dns.google/resolve",
    "https://8.8.8.8/resolve",
    "https://8.8.4.4/resolve",
    "https://1.1.1.1/dns-query",
)


def _doh_ipv4(host):
    if host in _CACHE:
        return _CACHE[host]
    ips = []
    busy = getattr(_IN_DOH, "busy", False)
    _IN_DOH.busy = True
    try:
        for endpoint in _DOH_ENDPOINTS:
            if busy and "dns.google" in endpoint:
                continue  # resolving dns.google inside DoH would recurse
            try:
                url = (endpoint + "?"
                       + urllib.parse.urlencode({"name": host, "type": "A"}))
                with urllib.request.urlopen(url, timeout=10) as resp:
                    data = json.load(resp)
                ips = [a["data"] for a in data.get("Answer", [])
                       if a.get("type") == 1 and a.get("data")]
                if ips:
                    break
            except Exception:
                continue
    finally:
        _IN_DOH.busy = False
    _CACHE[host] = ips
    return ips


def _patched(host, port, family=0, type=0, proto=0, flags=0):
    socktype = type or socket.SOCK_STREAM
    try:
        results = _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
    except socket.gaierror:
        results = []

    # IPv6 is broken on this handset (resolver returns AAAA, no route).
    # Prefer IPv4 whenever the caller did not explicitly ask for v6.
    if family in (0, socket.AF_UNSPEC, socket.AF_INET):
        ipv4 = [r for r in results if r[0] == socket.AF_INET]
        if ipv4:
            return ipv4
        if host and host not in ("localhost", "127.0.0.1"):
            ips = _doh_ipv4(host)
            if ips:
                return [(socket.AF_INET, socktype, proto or 6, "",
                         (ip, port or 0)) for ip in ips]
    if results:
        return results
    raise socket.gaierror(socket.EAI_NONAME,
                          "Name or service not known")


def install():
    socket.getaddrinfo = _patched


if __name__ == "__main__":
    install()
    from mcp_server_colab_exec.server import main
    main()
