import getpass
import warnings
from collections.abc import Awaitable, Callable

import pytest

from mdm.application.bootstrap import BootstrapOutcome, BootstrapStateConflict
from mdm.domain.auth import AuthInvariantError
from mdm.infrastructure import cli


class FakeTerminal:
    def __init__(self) -> None:
        self.text_answers = iter(["admin@example.net", "최초 관리자"])
        self.password_answers = iter(
            ["correct horse battery staple", "correct horse battery staple"]
        )
        self.prompts: list[str] = []

    def read_text(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return next(self.text_answers)

    def read_password(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return next(self.password_answers)


def _runner(
    outcome: BootstrapOutcome | Exception,
) -> tuple[
    Callable[[str, str, str], Awaitable[BootstrapOutcome]],
    list[tuple[str, str, str]],
]:
    calls: list[tuple[str, str, str]] = []

    async def run(email: str, name: str, password: str) -> BootstrapOutcome:
        calls.append((email, name, password))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return run, calls


@pytest.mark.unit
@pytest.mark.parametrize("outcome", [BootstrapOutcome.CREATED, BootstrapOutcome.NO_OP])
def test_cli_reads_identity_interactively_without_echoing_secrets(
    outcome: BootstrapOutcome,
    capsys: pytest.CaptureFixture[str],
) -> None:
    terminal = FakeTerminal()
    runner, calls = _runner(outcome)

    exit_code = cli.main(
        ["auth", "bootstrap-super-admin"],
        input_reader=terminal.read_text,
        password_reader=terminal.read_password,
        runner=runner,
        terminal_check=lambda: True,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls == [("admin@example.net", "최초 관리자", "correct horse battery staple")]
    assert terminal.prompts == ["Email: ", "Name: ", "Password: ", "Confirm password: "]
    assert "admin@example.net" not in captured.out + captured.err
    assert "correct horse battery staple" not in captured.out + captured.err


@pytest.mark.unit
def test_cli_state_conflict_is_one_sanitized_nonzero_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    terminal = FakeTerminal()
    runner, _ = _runner(BootstrapStateConflict("BOOTSTRAP_STATE_CONFLICT"))

    exit_code = cli.main(
        ["auth", "bootstrap-super-admin"],
        input_reader=terminal.read_text,
        password_reader=terminal.read_password,
        runner=runner,
        terminal_check=lambda: True,
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    assert captured.out == ""
    assert captured.err.strip() == "BOOTSTRAP_STATE_CONFLICT"


@pytest.mark.unit
def test_cli_rejects_extra_arguments_without_reprinting_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_email = "must-not-leak@example.net"

    exit_code = cli.main(
        ["auth", "bootstrap-super-admin", "--email", raw_email],
        input_reader=lambda _: "unused",
        password_reader=lambda _: "unused",
        runner=_runner(BootstrapOutcome.CREATED)[0],
        terminal_check=lambda: True,
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    assert raw_email not in captured.out + captured.err
    assert "Usage: mdm auth bootstrap-super-admin" in captured.err


@pytest.mark.unit
def test_cli_hides_unexpected_exception_diagnostics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    terminal = FakeTerminal()
    runner, _ = _runner(RuntimeError("admin@example.net stored-hash"))

    exit_code = cli.main(
        ["auth", "bootstrap-super-admin"],
        input_reader=terminal.read_text,
        password_reader=terminal.read_password,
        runner=runner,
        terminal_check=lambda: True,
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    assert captured.out == ""
    assert captured.err.strip() == "BOOTSTRAP_FAILED"
    assert "admin@example.net" not in captured.err
    assert "stored-hash" not in captured.err


@pytest.mark.unit
def test_cli_refuses_noninteractive_password_input(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(
        ["auth", "bootstrap-super-admin"],
        input_reader=lambda _: pytest.fail("must not prompt"),
        password_reader=lambda _: pytest.fail("must not prompt"),
        runner=_runner(BootstrapOutcome.CREATED)[0],
        terminal_check=lambda: False,
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    assert captured.out == ""
    assert captured.err.strip() == "BOOTSTRAP_INTERACTIVE_TERMINAL_REQUIRED"


@pytest.mark.unit
def test_cli_rejects_password_confirmation_mismatch_before_runner(
    capsys: pytest.CaptureFixture[str],
) -> None:
    terminal = FakeTerminal()
    terminal.password_answers = iter(
        ["correct horse battery staple", "different horse battery staple"]
    )
    runner, calls = _runner(BootstrapOutcome.CREATED)

    exit_code = cli.main(
        ["auth", "bootstrap-super-admin"],
        input_reader=terminal.read_text,
        password_reader=terminal.read_password,
        runner=runner,
        terminal_check=lambda: True,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert calls == []
    assert captured.out == ""
    assert captured.err.strip() == "BOOTSTRAP_PASSWORD_CONFIRMATION_MISMATCH"
    assert "correct horse battery staple" not in captured.err
    assert "different horse battery staple" not in captured.err


@pytest.mark.unit
def test_cli_accepts_canonically_equivalent_password_confirmation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    terminal = FakeTerminal()
    terminal.password_answers = iter(
        ["correct horse cafe\u0301 password", "correct horse caf\u00e9 password"]
    )
    runner, calls = _runner(BootstrapOutcome.CREATED)

    exit_code = cli.main(
        ["auth", "bootstrap-super-admin"],
        input_reader=terminal.read_text,
        password_reader=terminal.read_password,
        runner=runner,
        terminal_check=lambda: True,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls == [("admin@example.net", "최초 관리자", "correct horse caf\u00e9 password")]
    assert "correct horse cafe\u0301 password" not in captured.out + captured.err
    assert "correct horse caf\u00e9 password" not in captured.out + captured.err


@pytest.mark.unit
@pytest.mark.parametrize("unsafe_prompt_number", [1, 2])
def test_cli_refuses_password_when_terminal_echo_cannot_be_disabled(
    unsafe_prompt_number: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    terminal = FakeTerminal()
    runner, calls = _runner(BootstrapOutcome.CREATED)
    prompt_number = 0

    def unsafe_password_reader(prompt: str) -> str:
        nonlocal prompt_number
        prompt_number += 1
        if prompt_number == unsafe_prompt_number:
            warnings.warn("Password input may be echoed.", getpass.GetPassWarning, stacklevel=2)
        return "must-not-be-used"

    exit_code = cli.main(
        ["auth", "bootstrap-super-admin"],
        input_reader=terminal.read_text,
        password_reader=unsafe_password_reader,
        runner=runner,
        terminal_check=lambda: True,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert calls == []
    assert captured.out == ""
    assert captured.err.strip() == "BOOTSTRAP_INTERACTIVE_TERMINAL_REQUIRED"


@pytest.mark.unit
def test_cli_invalid_identity_input_returns_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    terminal = FakeTerminal()
    runner, _ = _runner(AuthInvariantError("must-not-leak@example.net"))

    exit_code = cli.main(
        ["auth", "bootstrap-super-admin"],
        input_reader=terminal.read_text,
        password_reader=terminal.read_password,
        runner=runner,
        terminal_check=lambda: True,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.strip() == "BOOTSTRAP_INPUT_INVALID"
    assert "must-not-leak@example.net" not in captured.err


@pytest.mark.unit
def test_cli_end_of_input_returns_input_environment_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(
        ["auth", "bootstrap-super-admin"],
        input_reader=lambda _: (_ for _ in ()).throw(EOFError),
        password_reader=lambda _: pytest.fail("must not prompt"),
        runner=_runner(BootstrapOutcome.CREATED)[0],
        terminal_check=lambda: True,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.strip() == "BOOTSTRAP_INPUT_UNAVAILABLE"


@pytest.mark.unit
def test_cli_control_c_returns_shell_interrupt_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(
        ["auth", "bootstrap-super-admin"],
        input_reader=lambda _: (_ for _ in ()).throw(KeyboardInterrupt),
        password_reader=lambda _: pytest.fail("must not prompt"),
        runner=_runner(BootstrapOutcome.CREATED)[0],
        terminal_check=lambda: True,
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.out == ""
    assert captured.err.strip() == "BOOTSTRAP_CANCELLED"
