import pytest

from mdm.application.origins import canonical_origin


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://app.example.net", "https://app.example.net:443"),
        ("https://APP.example.net:443", "https://app.example.net:443"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://127.0.0.1", "http://127.0.0.1:80"),
        ("https://[2001:db8::1]", "https://[2001:db8::1]:443"),
    ],
)
def test_canonical_origin_compares_scheme_host_and_effective_port(raw: str, expected: str) -> None:
    assert canonical_origin(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "null",
        "https://app.example.net/",
        "https://app.example.net/path",
        "https://app.example.net?",
        "https://app.example.net#",
        "https://user@app.example.net",
        "https://app.example.net https://evil.example",
        "https://app.example.net,https://evil.example",
        " https://app.example.net",
        "https://app.example.net\n",
        "https://app.exa\tmple.net",
        "ftp://app.example.net",
        "https://app.example.net:0",
        "https://app.example.net:65536",
        "https://app.example.net:",
        "https://*.example.net",
        "https://app.example.net\\evil",
        "https://[fe80::1%eth0]",
        "https://例.example",
        "https://app..example.net",
        "https://[::1]evil",
        "https://[::1]extra:443",
    ],
)
def test_origin_rejects_non_origin_or_ambiguous_values(raw: str) -> None:
    with pytest.raises(ValueError):
        canonical_origin(raw)


@pytest.mark.unit
@pytest.mark.parametrize(
    "other",
    [
        "http://app.example.net",
        "https://app.example.net:444",
        "https://sub.app.example.net",
        "https://app.example.net.evil",
        "https://app.example.net.",
    ],
)
def test_distinct_origin_is_not_collapsed_to_allowlisted_origin(other: str) -> None:
    assert canonical_origin(other) != canonical_origin("https://app.example.net")
