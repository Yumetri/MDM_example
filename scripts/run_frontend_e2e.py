"""Run built-frontend browser tests against a local mdm_test API with temporary TLS keys."""

import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from mdm.infrastructure.jwt_key_generation import generate_local_jwt_keys

ROOT = Path(__file__).resolve().parents[1]


class RunnerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MDM_", env_file=".env", extra="ignore")
    database_url: SecretStr


def validate_test_database(url: str) -> None:
    parsed = make_url(url)
    if (
        parsed.drivername != "postgresql+asyncpg"
        or parsed.database != "mdm_test"
        or parsed.host not in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("browser tests require a local mdm_test database")


def write_https_certificate(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path, cert_path = directory / "https-key.pem", directory / "https-cert.pem"
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for_server(url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 40
    with httpx.Client(verify=False, trust_env=False, timeout=1) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("browser test server exited before becoming ready")
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    raise RuntimeError("browser test server did not become ready")


def main() -> int:
    database = RunnerSettings().database_url.get_secret_value()
    validate_test_database(database)
    with tempfile.TemporaryDirectory(prefix="mdm-frontend-e2e-") as temporary:
        directory = Path(temporary)
        key, cert = write_https_certificate(directory)
        private_key, jwks = directory / "jwt.pem", directory / "jwks.json"
        generate_local_jwt_keys(kid="browser-test", private_key_path=private_key, jwks_path=jwks)
        api_port, frontend_port = available_port(), available_port()
        while frontend_port == api_port:
            frontend_port = available_port()
        origin = f"https://localhost:{frontend_port}"
        api_origin = f"http://127.0.0.1:{api_port}"
        control_key = secrets.token_urlsafe(32)
        env = {key: value for key, value in os.environ.items() if not key.startswith("MDM_")}
        env.update(
            {
                "MDM_DATABASE_URL": database,
                "MDM_AUTH_SESSIONS_ENABLED": "true",
                "MDM_AUTH_REGISTRATIONS_ENABLED": "true",
                "MDM_AUTH_PASSWORD_RESETS_ENABLED": "true",
                "MDM_AUTH_ALLOWED_ORIGINS": json.dumps([origin]),
                "MDM_AUTH_ALLOW_INSECURE_LOCAL_COOKIES": "false",
                "MDM_AUTH_IP_HMAC_SECRET": secrets.token_urlsafe(32),
                "MDM_AUTH_JWT_ACTIVE_KID": "browser-test",
                "MDM_AUTH_JWT_PRIVATE_KEY_PATH": str(private_key),
                "MDM_AUTH_JWT_JWKS_PATH": str(jwks),
                "MDM_AUTH_REGISTRATION_ALLOWED_DOMAINS": '["example.net"]',
                "MDM_EMAIL_PUBLIC_APP_BASE_URL": origin,
                "MDM_EMAIL_SMTP_HOST": "smtp.example.net",
                "MDM_EMAIL_SMTP_PORT": "587",
                "MDM_EMAIL_SMTP_USERNAME": "browser-test",
                "MDM_EMAIL_SMTP_PASSWORD": "unused-test-adapter",
                "MDM_EMAIL_SENDER_ADDRESS": "noreply@example.net",
                "MDM_E2E_API_PORT": str(api_port),
                "MDM_E2E_CONTROL_KEY": control_key,
                "MDM_E2E_ORIGIN": origin,
                "MDM_E2E_API_ORIGIN": api_origin,
                "MDM_FRONTEND_API_ORIGIN": api_origin,
                "MDM_FRONTEND_TLS_KEY": str(key),
                "MDM_FRONTEND_TLS_CERT": str(cert),
            }
        )
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True
        )
        processes: list[subprocess.Popen[bytes]] = []
        try:
            api = subprocess.Popen(
                [sys.executable, "scripts/frontend_e2e_server.py"],
                cwd=ROOT,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(api)
            wait_for_server(f"{api_origin}/api/v1/health/ready", api)
            frontend = subprocess.Popen(
                ["node", "node_modules/vite/bin/vite.js", "preview", "--port", str(frontend_port)],
                cwd=ROOT / "frontend",
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(frontend)
            wait_for_server(origin, frontend)
            return subprocess.run(
                ["npm", "run", "e2e"], cwd=ROOT / "frontend", env=env, check=False
            ).returncode
        finally:
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
