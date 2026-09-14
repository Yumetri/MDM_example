"""Validated provider-network configuration values."""

from ipaddress import IPv6Address, ip_address


def normalize_network_host(value: str) -> str:
    """Return one normalized DNS hostname or IP literal."""
    if not value or value != value.strip() or "%" in value:
        raise ValueError("network host must be a hostname or IP literal")
    try:
        return str(ip_address(value))
    except ValueError:
        pass
    try:
        hostname = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("network host must be a hostname or IP literal") from None
    if hostname.endswith("."):
        hostname = hostname[:-1]
    labels = hostname.split(".")
    if (
        not hostname
        or len(hostname) > 253
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(
                character.isascii() and (character.isalnum() or character == "-")
                for character in label
            )
            for label in labels
        )
    ):
        raise ValueError("network host must be a hostname or IP literal")
    return hostname


def format_url_host(value: str) -> str:
    """Bracket a normalized IPv6 literal for URL authority rendering."""
    normalized = normalize_network_host(value)
    try:
        address = ip_address(normalized)
    except ValueError:
        return normalized
    return f"[{normalized}]" if isinstance(address, IPv6Address) else normalized
