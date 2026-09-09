"""Generate ignored local-development JWT key files."""

from pathlib import Path

from mdm.infrastructure.jwt_key_generation import generate_local_jwt_keys


def main() -> None:
    private_key_path = Path("secrets/local/jwt-private.pem")
    jwks_path = Path("secrets/local/jwt-public-keys.json")
    generate_local_jwt_keys(
        kid="local-dev",
        private_key_path=private_key_path,
        jwks_path=jwks_path,
    )
    print(f"Created {private_key_path} and {jwks_path}")


if __name__ == "__main__":
    main()
