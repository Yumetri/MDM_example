"""Strict origin values shared by configuration and request protection."""

import re
from ipaddress import IPv6Address, ip_address
from urllib.parse import urlsplit

_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def canonical_origin(value: str) -> str:
    """Parse one serialized HTTP(S) origin without accepting URL paths or URL cleanup."""
    if (
        not value
        or not value.isascii()
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
        or any(character in value for character in "\\%?#,@")
    ):
        raise ValueError("expected one HTTP(S) origin")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("expected one HTTP(S) origin") from None
    if (
        parsed.scheme not in ("http", "https")
        or not host
        or parsed.path
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("expected one HTTP(S) origin")
    try:
        address = ip_address(host)
    except ValueError:
        # A trailing DNS dot is retained: browser origins do not equate these hosts.
        labels = host.removesuffix(".").split(".")
        if len(host) > 254 or any(_DNS_LABEL.fullmatch(label) is None for label in labels):
            raise ValueError("expected one HTTP(S) origin") from None
    else:
        host = f"[{address}]" if isinstance(address, IPv6Address) else str(address)
    return f"{parsed.scheme}://{host}:{port or (443 if parsed.scheme == 'https' else 80)}"
