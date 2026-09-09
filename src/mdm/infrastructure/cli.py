"""Minimal command-line boundary for explicit MDM operations."""

import asyncio
import getpass
import sys
import warnings
from collections.abc import Awaitable, Callable, Sequence

from mdm.application.bootstrap import (
    BootstrapOutcome,
    BootstrapStateConflict,
    BootstrapSuperAdmin,
)
from mdm.domain.auth import AuthInvariantError
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.passwords import build_password_hasher
from mdm.infrastructure.repositories.bootstrap import SqlAlchemyBootstrapSuperAdminRepository
from mdm.infrastructure.settings import Settings

_BOOTSTRAP_COMMAND = ("auth", "bootstrap-super-admin")
_USAGE = "Usage: mdm auth bootstrap-super-admin"

InputReader = Callable[[str], str]
BootstrapRunner = Callable[[str, str, str], Awaitable[BootstrapOutcome]]
TerminalCheck = Callable[[], bool]


async def _execute_bootstrap(email: str, name: str, password: str) -> BootstrapOutcome:
    settings = Settings()
    engine = create_engine(settings.reveal_database_url())
    try:
        repository = SqlAlchemyBootstrapSuperAdminRepository(create_session_factory(engine))
        use_case = BootstrapSuperAdmin(repository, build_password_hasher(settings))
        return await use_case.execute(email=email, name=name, password=password)
    finally:
        await engine.dispose()


def main(
    argv: Sequence[str] | None = None,
    *,
    input_reader: InputReader = input,
    password_reader: InputReader = getpass.getpass,
    runner: BootstrapRunner = _execute_bootstrap,
    terminal_check: TerminalCheck = sys.stdin.isatty,
) -> int:
    """Run an exact command without reflecting rejected arguments or secrets."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments != _BOOTSTRAP_COMMAND:
        print(_USAGE, file=sys.stderr)
        return 2
    if not terminal_check():
        print("BOOTSTRAP_INTERACTIVE_TERMINAL_REQUIRED", file=sys.stderr)
        return 2

    async def invoke_runner(email: str, name: str, password: str) -> BootstrapOutcome:
        return await runner(email, name, password)

    try:
        email = input_reader("Email: ")
        name = input_reader("Name: ")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = password_reader("Password: ")
        outcome = asyncio.run(invoke_runner(email, name, password))
    except BootstrapStateConflict:
        print("BOOTSTRAP_STATE_CONFLICT", file=sys.stderr)
        return 1
    except AuthInvariantError:
        print("BOOTSTRAP_INPUT_INVALID", file=sys.stderr)
        return 2
    except getpass.GetPassWarning:
        print("BOOTSTRAP_INTERACTIVE_TERMINAL_REQUIRED", file=sys.stderr)
        return 2
    except EOFError:
        print("BOOTSTRAP_INPUT_UNAVAILABLE", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("BOOTSTRAP_CANCELLED", file=sys.stderr)
        return 130
    except Exception:
        print("BOOTSTRAP_FAILED", file=sys.stderr)
        return 1

    if outcome is BootstrapOutcome.CREATED:
        print("SUPER_ADMIN bootstrap 생성 완료")
    else:
        print("SUPER_ADMIN bootstrap 상태 확인 완료")
    return 0
