#!/usr/bin/env python3
"""Run a stdio MCP server with a DNS-over-HTTPS fallback.

Why this exists
---------------
Many Android/Termux handsets have a resolver that returns IPv6-only (AAAA)
answers while the device has no working IPv6 route, and some networks
filter specific hostnames (e.g. colab.research.google.com). The result is
``[Errno 113] No route to host`` or ``No address associated with hostname``
even though the service is reachable over IPv4.

This wrapper patches ``socket.getaddrinfo``:

* prefer IPv4 whenever the caller did not explicitly request IPv6;
* if no IPv4 answer exists, resolve through DNS-over-HTTPS (dns.google).

Only address lookup is patched - the hostname is left untouched, so TLS
SNI and certificate validation still use the real name.

Usage
-----
    python3 colab_mcp_dns.py            # runs `mcp-server-colab-exec`

Set ``COLAB_MCP_TARGET`` to a ``module:function`` pair to launch a
different stdio server, e.g. ``COLAB_MCP_TARGET=mypkg.server:main``.
"""
import importlib
import json
import os
import socket
import sys
import urllib.parse
import urllib.request

_ORIG_GETADDRINFO = socket.getaddrinfo
_CACHE: dict[str, list[str]] = {}


def _doh_ipv4(host: str) -> list[str]:
    if host in _CACHE:
        return _CACHE[host]
    ips: list[str] = []
    try:
        url = "https://dns.google/resolve?" + urllib.parse.urlencode(
            {"name": host, "type": "A"}
        )
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.load(resp)
        ips = [
            a["data"]
            for a in data.get("Answer", [])
            if a.get("type") == 1 and a.get("data")
        ]
    except Exception:
        ips = []
    _CACHE[host] = ips
    return ips


def _patched(host, port, family=0, type=0, proto=0, flags=0):
    socktype = type or socket.SOCK_STREAM
    try:
        results = _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
    except socket.gaierror:
        results = []

    if family in (0, socket.AF_UNSPEC, socket.AF_INET):
        ipv4 = [r for r in results if r[0] == socket.AF_INET]
        if ipv4:
            return ipv4
        if host and host not in ("localhost", "127.0.0.1"):
            ips = _doh_ipv4(host)
            if ips:
                return [
                    (socket.AF_INET, socktype, proto or 6, "", (ip, port or 0))
                    for ip in ips
                ]

    if results:
        return results
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


def install() -> None:
    """Install the getaddrinfo patch (idempotent)."""
    socket.getaddrinfo = _patched


def main() -> None:
    install()
    target = os.environ.get("COLAB_MCP_TARGET", "mcp_server_colab_exec.server:main")
    module_name, _, func_name = target.partition(":")
    module = importlib.import_module(module_name)
    getattr(module, func_name)()


if __name__ == "__main__":
    main()
