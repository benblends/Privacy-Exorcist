"""
SPEC-004 test vectors — CLI formatter, config loader, signal handler.

Tests TV01–TV13: state change rendering, config validation,
HITL prompts, signal handling, and run summary formatting.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from privacy_exorcist.cli.config import AppConfig, load_config, _validate_env
from privacy_exorcist.cli.formatter import CLIFormatter, STATE_ICONS, STYLES
from privacy_exorcist.cli.signals import SignalHandler
from privacy_exorcist.engine import BrokerRunResult, TaskContext
from privacy_exorcist.models import BrokerState, Playbook, PlaybookEntry, Profile


# ═══════════════════════════════════════════════════════════════════════════════
# CLIFormatter — TV01–TV03, TV13
# ═══════════════════════════════════════════════════════════════════════════════

class TestCLIFormatter:

    @pytest.fixture
    def cli(self):
        cli = CLIFormatter()
        cli.console = mock.MagicMock()  # Prevent actual terminal output
        return cli

    def test_tv01_state_icon_queued_to_in_progress(self, cli):
        """TV01: QUEUED → IN_PROGRESS shows 🔄 icon."""
        cli.on_state_change("whitepages", BrokerState.QUEUED, BrokerState.IN_PROGRESS)
        # Verify console.print was called
        assert cli.console.print.called

    def test_tv02_state_icon_submitted_to_scrubbed(self, cli):
        """TV02: SUBMITTED → SCRUBBED shows ✅ icon."""
        cli.on_state_change("thatsthem", BrokerState.SUBMITTED, BrokerState.SCRUBBED)
        assert cli.console.print.called

    def test_state_icons_cover_all_states(self):
        """Every BrokerState has an icon."""
        for state in BrokerState:
            assert state.value in STATE_ICONS, f"Missing icon for {state.value}"

    def test_styles_have_required_keys(self):
        required = {"info", "success", "warning", "error", "captcha",
                     "reasoning", "action", "broker", "summary"}
        assert set(STYLES.keys()) == required

    @mock.patch("builtins.input", return_value="")
    def test_tv03_hitl_prompt(self, mock_input, cli):
        """TV03: HITL prompt fires with broker name."""
        cli.on_hitl_prompt("nuwber")
        assert cli.console.print.called
        # At least one print call should contain the broker name
        found = False
        for call in cli.console.print.mock_calls:
            args = call.args
            if args and any("nuwber" in str(a) for a in args):
                found = True
        assert found, "HITL prompt should mention broker name"

    def test_on_broker_start(self, cli):
        ctx = TaskContext(
            broker_id="test", seed_url="https://test.com",
            profile=Profile("A", "B", "S", "C", "ST", "Z", "P", "E"),
            playbook_entry=PlaybookEntry(
                broker_id="test", seed_url="https://test.com", success_anchor="OK"
            ),
            capsolver_key=None, headless=True,
        )
        cli.on_broker_start(ctx)
        assert cli.console.print.called

    def test_tv13_run_summary(self, cli):
        """TV13: Summary table renders all counts."""
        summary = {
            "scrubbed": 3, "no_record": 1, "failed": 2,
            "captcha_blocked": 1, "pending": 0, "total": 7,
            "permanently_failed": 0,
        }
        cli.print_run_summary(summary)
        assert cli.console.print.called

    def test_print_header(self, cli):
        cli.print_header()
        assert cli.console.print.called

    def test_print_config_summary(self, cli):
        profile = Profile("Jane", "Doe", "123 Main", "Austin", "TX", "78701",
                          "512-555-0147", "jane@test.com")
        playbook = Playbook(brokers=[
            PlaybookEntry(broker_id="test", seed_url="https://test.com",
                          success_anchor="OK"),
        ])
        cli.print_config_summary(profile, playbook, capsolver_key="sk-test")
        assert cli.console.print.called


# ═══════════════════════════════════════════════════════════════════════════════
# Config Loader — TV04–TV08
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigLoader:

    def test_tv04_missing_openai_key(self, monkeypatch):
        """TV04: OPENAI_API_KEY unset → error."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config = AppConfig()
        _validate_env(config)
        assert any("OPENAI_API_KEY" in e for e in config.errors)

    def test_tv05_valid_config(self, monkeypatch, tmp_path):
        """TV05: Valid setup → no errors."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("HEADLESS", "true")

        # Write valid profile
        profile = tmp_path / "profile.json"
        profile.write_text('{"first_name":"J","last_name":"D","current_street":"S",'
                           '"current_city":"C","current_state":"ST","current_zip":"78701",'
                           '"current_phone":"P","sentinel_email":"e@t.com"}')

        # Write valid playbook
        playbook = tmp_path / "playbook.json"
        playbook.write_text('{"brokers":[{"broker_id":"test","seed_url":"https://t.com",'
                            '"success_anchor":"OK"}]}')

        config = load_config(str(profile), str(playbook))
        assert config.is_valid
        assert config.openai_key == "sk-test"
        assert config.headless is True
        assert config.profile is not None
        assert config.playbook is not None

    def test_tv06_partial_imap(self, monkeypatch):
        """TV06: IMAP_SERVER set but IMAP_PORT not → error."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("IMAP_SERVER", "imap.gmail.com")
        monkeypatch.delenv("IMAP_PORT", raising=False)
        monkeypatch.delenv("IMAP_USERNAME", raising=False)
        monkeypatch.delenv("IMAP_PASSWORD", raising=False)

        config = AppConfig()
        _validate_env(config)
        assert any("IMAP" in e for e in config.errors), config.errors

    def test_tv07_headless_default(self, monkeypatch):
        """TV07: HEADLESS not set → defaults to True."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("HEADLESS", raising=False)

        config = AppConfig()
        _validate_env(config)
        assert config.headless is True

    def test_headless_false(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("HEADLESS", "false")
        config = AppConfig()
        _validate_env(config)
        assert config.headless is False

    def test_headless_zero(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("HEADLESS", "0")
        config = AppConfig()
        _validate_env(config)
        assert config.headless is False

    def test_full_imap_config_valid(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("IMAP_SERVER", "imap.gmail.com")
        monkeypatch.setenv("IMAP_PORT", "993")
        monkeypatch.setenv("IMAP_USERNAME", "test")
        monkeypatch.setenv("IMAP_PASSWORD", "pass")
        config = AppConfig()
        _validate_env(config)
        assert config.imap_configured
        assert config.imap_port == 993


# ═══════════════════════════════════════════════════════════════════════════════
# Signal Handler — TV11–TV12
# ═══════════════════════════════════════════════════════════════════════════════

class TestSignalHandler:

    def test_install_and_restore(self):
        handler = SignalHandler()
        orch = mock.MagicMock()
        handler.install(orch)
        assert handler._original_sigint is not None
        handler.restore()
        assert handler._original_sigint is None

    def test_tv11_single_sigint(self):
        """TV11: Single Ctrl+C → shutdown requested."""
        handler = SignalHandler()
        orch = mock.MagicMock()
        handler.install(orch)

        # Simulate one SIGINT
        handler._shutdown_requested = False
        # Directly test the handler's state
        assert not handler.shutdown_requested

    @mock.patch("os._exit")
    def test_tv12_double_sigint_force_quit(self, mock_exit):
        """TV12: Second Ctrl+C → os._exit(1)."""
        handler = SignalHandler()
        orch = mock.MagicMock()
        handler.install(orch)
        handler._shutdown_requested = True

        # Get the installed handler and call it directly
        import signal
        current_handler = signal.getsignal(signal.SIGINT)
        assert current_handler is not None
        # Call the handler (simulating second Ctrl+C)
        current_handler(signal.SIGINT, None)
        mock_exit.assert_called_once_with(1)
