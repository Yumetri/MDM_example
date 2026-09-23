import runpy
from pathlib import Path

import pytest

runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/run_frontend_e2e.py"))
validate_test_database = runner["validate_test_database"]
write_https_certificate = runner["write_https_certificate"]

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://mdm:password@127.0.0.1:55432/mdm",
        "postgresql+asyncpg://mdm:password@production.example.net/mdm_test",
    ],
)
def test_browser_runner_refuses_nonlocal_or_non_test_database(url: str):
    with pytest.raises(ValueError, match="local mdm_test"):
        validate_test_database(url)


def test_browser_runner_accepts_only_the_local_test_database():
    validate_test_database("postgresql+asyncpg://mdm:password@127.0.0.1:55432/mdm_test")


def test_browser_runner_uses_a_private_temporary_https_key(tmp_path: Path):
    from cryptography import x509

    key, certificate = write_https_certificate(tmp_path)
    assert key.stat().st_mode & 0o777 == 0o600
    parsed = x509.load_pem_x509_certificate(certificate.read_bytes())
    names = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == ["localhost"]
