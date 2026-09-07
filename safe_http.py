"""HTTP reachability checks restricted to public destinations.

Resolve each redirect separately and connect to the validated IP, while
preserving the original hostname for Host, TLS SNI and certificate checks.
Response bodies are never downloaded.
"""
import ipaddress
import socket
from types import SimpleNamespace
from urllib.parse import urlsplit, urljoin

import certifi
import requests
import urllib3
from fastapi import HTTPException


def parse_public_url(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError()
        if parts.username is not None or parts.password is not None:
            raise ValueError()
        host = parts.hostname.encode("idna").decode("ascii")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port not in (80, 443) or any(c.isspace() for c in url) or "\\" in url:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise HTTPException(422, "invalid public HTTP(S) URL; only ports 80 and 443 are allowed")
    return parts, host, port


def public_addresses(host, port):
    try:
        addresses = list(dict.fromkeys(
            item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ))
    except OSError as exc:
        raise requests.exceptions.ConnectionError("DNS resolution failed") from exc
    if not addresses:
        raise requests.exceptions.ConnectionError("DNS returned no addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        mapped = isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None
        if not ip.is_global or ip.is_multicast or mapped:
            raise HTTPException(422, "destination must resolve only to public unicast IP addresses")
    return addresses


def public_request(url, method="HEAD", timeout=5, headers=None):
    current = url
    for hop in range(6):
        parts, host, port = parse_public_url(current)
        addresses = public_addresses(host, port)
        # Connection pool host is a validated numeric IP: no second DNS lookup.
        options = dict(port=port, timeout=urllib3.Timeout(connect=timeout, read=timeout), retries=False)
        if parts.scheme == "https":
            pool = urllib3.HTTPSConnectionPool(
                addresses[0], server_hostname=host, assert_hostname=host,
                cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(), **options
            )
        else:
            pool = urllib3.HTTPConnectionPool(addresses[0], **options)
        authority = f"[{host}]" if ":" in host else host
        if port != (443 if parts.scheme == "https" else 80):
            authority += f":{port}"
        request_headers = dict(headers or {}, Host=authority)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        response = None
        try:
            response = pool.urlopen(method, path, headers=request_headers,
                                    redirect=False, preload_content=False)
            status, location = response.status, response.headers.get("Location")
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            raise requests.exceptions.ConnectionError("public HTTP request failed") from exc
        finally:
            if response is not None:
                response.close()
            pool.close()
        if status in (301, 302, 303, 307, 308) and location:
            if hop == 5:
                raise requests.exceptions.TooManyRedirects("redirect limit exceeded")
            current = urljoin(current, location)
            continue
        return SimpleNamespace(status_code=status, url=current)
