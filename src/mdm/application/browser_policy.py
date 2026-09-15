"""Validated browser origin and transport policy shared by settings and cookie helpers."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from mdm.application.origins import canonical_origin


@dataclass(frozen=True, slots=True)
class BrowserProtectionPolicy:
    allowed_origins: tuple[str, ...] = ()
    allow_insecure_local_cookies: bool = False

    def __post_init__(self) -> None:
        origins = tuple(canonical_origin(value) for value in self.allowed_origins)
        if self.allow_insecure_local_cookies:
            if not origins or any(
                urlsplit(value).scheme != "http"
                or urlsplit(value).hostname not in ("localhost", "127.0.0.1", "::1")
                for value in origins
            ):
                raise ValueError("insecure cookies require only explicit loopback HTTP origins")
        elif any(urlsplit(value).scheme != "https" for value in origins):
            raise ValueError("authentication origins must use HTTPS")
        object.__setattr__(self, "allowed_origins", origins)

    @property
    def secure(self) -> bool:
        return not self.allow_insecure_local_cookies
