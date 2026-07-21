"""
SPEC-002 test vectors — 10 result mapper + 5 integration tests.

Tests result_mapper, task_builder, browser_factory, capsolver_action,
and BrowserOperator (with mocked agent).
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from privacy_exorcist.browser_operator.browser_factory import (
    STEALTH_ARGS,
    STEALTH_USER_AGENT,
    create_browser_profile,
)
from privacy_exorcist.browser_operator.capsolver_action import (
    CapSolverError,
    TURNSTILE_INJECT_JS,
    create_capsolver_controller,
    solve_turnstile_via_api,
)
from privacy_exorcist.browser_operator.operator import BrowserOperator
from privacy_exorcist.browser_operator.result_mapper import map_result
from privacy_exorcist.browser_operator.task_builder import build_task
from privacy_exorcist.engine import TaskContext
from privacy_exorcist.models import (
    BrokerResult,
    PlaybookEntry,
    Profile,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Result Mapper — TV01–TV10
# ═══════════════════════════════════════════════════════════════════════════════

class TestResultMapper:
    """TV01–TV10 from SPEC-002 §6."""

    ANCHOR = "Request Submitted"

    def test_tv01_success_exact_anchor(self):
        r = map_result("I see 'Request Submitted' on the page. SUCCESS.", self.ANCHOR)
        assert r == BrokerResult.SUCCESS.value

    def test_tv02_verification_required(self):
        r = map_result(
            "Form submitted. A verification email was sent to the address.",
            "submitted",
        )
        assert r == BrokerResult.VERIFICATION_REQUIRED.value

    def test_tv03_captcha_blocked(self):
        r = map_result("CAPTCHA_BLOCKED: challenge could not be solved.", self.ANCHOR)
        assert r == BrokerResult.CAPTCHA_BLOCKED.value

    def test_tv04_captcha_detected(self):
        r = map_result("I see a CAPTCHA challenge. solve_captcha failed.", self.ANCHOR)
        assert r == BrokerResult.CAPTCHA_DETECTED.value

    def test_tv05_blocked_403(self):
        r = map_result("403 Forbidden error. Cannot access page.", self.ANCHOR)
        assert r == BrokerResult.BLOCKED_403.value

    def test_tv06_no_match_found(self):
        r = map_result("Search returned no results for John Smith.", self.ANCHOR)
        assert r == BrokerResult.NO_MATCH_FOUND.value

    def test_tv07_broker_unreachable(self):
        r = map_result("Connection timeout. Site unreachable.", self.ANCHOR)
        assert r == BrokerResult.BROKER_UNREACHABLE.value

    def test_tv08_form_submit_failed(self):
        r = map_result("Form submitted but got validation error.", self.ANCHOR)
        assert r == BrokerResult.FORM_SUBMIT_FAILED.value

    def test_tv09_multiple_match(self):
        r = map_result("Too many matching records found. Cannot pick one.", self.ANCHOR)
        assert r == BrokerResult.MULTIPLE_MATCH.value

    def test_tv10_success_via_different_anchor(self):
        r = map_result("Task completed. done. SUCCESS.", "done")
        assert r == BrokerResult.SUCCESS.value

    def test_empty_string_defaults_to_failed(self):
        r = map_result("", self.ANCHOR)
        assert r == BrokerResult.FORM_SUBMIT_FAILED.value


# ═══════════════════════════════════════════════════════════════════════════════
# Task Builder — TV11–TV12
# ═══════════════════════════════════════════════════════════════════════════════

class TestTaskBuilder:
    """TV11 (DIRECT_FORM) and TV12 (SEARCH_AND_CLAIM)."""

    @pytest.fixture
    def profile(self):
        return Profile(
            first_name="Jane", last_name="Doe",
            current_street="123 Main", current_city="Austin",
            current_state="TX", current_zip="78701",
            current_phone="512-555-0147",
            sentinel_email="jane@test.com",
        )

    @pytest.fixture
    def playbook_entry(self):
        return PlaybookEntry(
            broker_id="thatsthem",
            seed_url="https://thatsthem.com/optout",
            success_anchor="Request Submitted",
            flow_type="DIRECT_FORM",
            captcha_type="cloudflare_turnstile",
            captcha_sitekey="0x4AAAAAACiKzu913X3aFRkP",
        )

    def test_tv11_direct_form_includes_all_fields(self, profile, playbook_entry):
        ctx = TaskContext(
            broker_id="thatsthem",
            seed_url=playbook_entry.seed_url,
            profile=profile,
            playbook_entry=playbook_entry,
            capsolver_key="sk-test",
            headless=True,
        )
        task = build_task(ctx)
        assert "Jane" in task
        assert "Doe" in task
        assert "123 Main" in task
        assert "Austin" in task
        assert "TX" in task
        assert "78701" in task
        assert "512-555-0147" in task
        assert "jane@test.com" in task
        assert "Request Submitted" in task
        assert "Solve CAPTCHA" in task
        # CRITICAL block must come BEFORE numbered steps
        critical_pos = task.index("CRITICAL:")
        step1_pos = task.index("1.")
        assert critical_pos < step1_pos, "CRITICAL must precede numbered steps"

    def test_tv12_search_and_claim(self, profile):
        entry = PlaybookEntry(
            broker_id="whitepages",
            seed_url="https://whitepages.com/suppress",
            success_anchor="successfully submitted",
            flow_type="SEARCH_AND_CLAIM",
        )
        ctx = TaskContext(
            broker_id="whitepages",
            seed_url=entry.seed_url,
            profile=profile,
            playbook_entry=entry,
            capsolver_key=None,
            headless=True,
        )
        task = build_task(ctx)
        assert "Search for:" in task
        assert "Jane Doe" in task
        assert "Austin" in task
        assert "successfully submitted" in task


# ═══════════════════════════════════════════════════════════════════════════════
# Browser Factory — TV14
# ═══════════════════════════════════════════════════════════════════════════════

class TestBrowserFactory:
    """TV14: Stealth browser configuration."""

    def test_headless_profile(self):
        bp = create_browser_profile(headless=True)
        assert bp.headless is True
        assert bp.chromium_sandbox is False
        assert bp.disable_security is True
        assert bp.user_agent == STEALTH_USER_AGENT
        for arg in STEALTH_ARGS:
            assert arg in bp.args, f"Missing stealth arg: {arg}"

    def test_headed_profile(self):
        bp = create_browser_profile(headless=False)
        assert bp.headless is False
        assert bp.chromium_sandbox is False  # Still mandatory


# ═══════════════════════════════════════════════════════════════════════════════
# CapSolver Action — TV13, TV15
# ═══════════════════════════════════════════════════════════════════════════════

class TestCapSolverAction:

    def test_turnstile_inject_js_contains_token(self):
        assert "{token}" in TURNSTILE_INJECT_JS
        assert "cf-turnstile-response" in TURNSTILE_INJECT_JS

    def test_controller_creation(self):
        entry = PlaybookEntry(
            broker_id="thatsthem",
            seed_url="https://thatsthem.com/optout",
            success_anchor="Done",
            captcha_type="cloudflare_turnstile",
            captcha_sitekey="0x4AAAAAACiKzu913X3aFRkP",
        )
        ctrl = create_capsolver_controller("sk-test", entry)
        assert ctrl is not None
        # Verify action is registered
        assert hasattr(ctrl, "registry")

    def test_controller_no_key_still_creates(self):
        entry = PlaybookEntry(
            broker_id="test", seed_url="https://x.com", success_anchor="OK"
        )
        ctrl = create_capsolver_controller(None, entry)
        assert ctrl is not None

    @mock.patch("privacy_exorcist.browser_operator.capsolver_action.requests.post")
    def test_tv13_solve_turnstile_mock_http(self, mock_post):
        """Mock CapSolver create + poll → returns token."""
        mock_post.side_effect = [
            mock.Mock(json=lambda: {"errorId": 0, "taskId": "task-123"}),
            mock.Mock(json=lambda: {
                "status": "ready",
                "solution": {"token": "mock-token-abc123"},
            }),
        ]
        token = solve_turnstile_via_api(
            sitekey="0x4AAAAAACiKzu913X3aFRkP",
            page_url="https://thatsthem.com/optout",
            api_key="sk-test",
        )
        assert token == "mock-token-abc123"

    @mock.patch("privacy_exorcist.browser_operator.capsolver_action.requests.post")
    def test_solve_turnstile_create_fails(self, mock_post):
        mock_post.return_value = mock.Mock(
            json=lambda: {"errorId": 1, "errorDescription": "Invalid key"}
        )
        with pytest.raises(CapSolverError, match="Invalid key"):
            solve_turnstile_via_api("sk", "https://test.com", "bad-key")


# ═══════════════════════════════════════════════════════════════════════════════
# BrowserOperator — integration with mock agent
# ═══════════════════════════════════════════════════════════════════════════════

class TestBrowserOperator:

    @pytest.fixture
    def ctx(self):
        return TaskContext(
            broker_id="thatsthem",
            seed_url="https://thatsthem.com/optout",
            profile=Profile(
                first_name="Jane", last_name="Doe",
                current_street="123 Main", current_city="Austin",
                current_state="TX", current_zip="78701",
                current_phone="512-555-0147",
                sentinel_email="jane@test.com",
            ),
            playbook_entry=PlaybookEntry(
                broker_id="thatsthem",
                seed_url="https://thatsthem.com/optout",
                success_anchor="Request Submitted",
            ),
            capsolver_key=None,
            headless=True,
        )

    @pytest.mark.asyncio
    @mock.patch("privacy_exorcist.browser_operator.operator.Agent")
    async def test_execute_success(self, mock_agent, ctx):
        """Mock agent returns success anchor → SUCCESS."""
        mock_history = mock.MagicMock()
        mock_history.final_result.return_value = "I see 'Request Submitted'. SUCCESS."
        mock_agent.return_value.run = mock.AsyncMock(return_value=mock_history)

        op = BrowserOperator(openai_key="sk-test")
        result = await op.execute(ctx)

        assert result.broker_id == "thatsthem"
        assert result.outcome == BrokerResult.SUCCESS.value
        assert result.duration_seconds >= 0

    @pytest.mark.asyncio
    @mock.patch("privacy_exorcist.browser_operator.operator.Agent")
    async def test_execute_captcha_detected(self, mock_agent, ctx):
        """Mock agent reports CAPTCHA → CAPTCHA_DETECTED."""
        mock_history = mock.MagicMock()
        mock_history.final_result.return_value = "I see a CAPTCHA. solve_captcha failed."
        mock_agent.return_value.run = mock.AsyncMock(return_value=mock_history)

        op = BrowserOperator(openai_key="sk-test")
        result = await op.execute(ctx)

        assert result.outcome == BrokerResult.CAPTCHA_DETECTED.value

    @pytest.mark.asyncio
    @mock.patch("privacy_exorcist.browser_operator.operator.Agent")
    async def test_execute_agent_crash(self, mock_agent, ctx):
        """Agent.run() raises → BROKER_UNREACHABLE."""
        mock_agent.return_value.run.side_effect = RuntimeError("Browser crash")

        op = BrowserOperator(openai_key="sk-test")
        result = await op.execute(ctx)

        assert result.outcome == BrokerResult.BROKER_UNREACHABLE.value
        assert result.error is not None
        assert "Browser crash" in result.error

    @pytest.mark.asyncio
    @mock.patch("privacy_exorcist.browser_operator.operator.Agent")
    async def test_execute_timeout(self, mock_agent, ctx):
        """Agent.run() times out → BROKER_UNREACHABLE."""
        async def slow_run():
            await asyncio.sleep(999)
        mock_agent.return_value.run.side_effect = slow_run

        op = BrowserOperator(openai_key="sk-test", agent_timeout=1)
        result = await op.execute(ctx)

        assert result.outcome == BrokerResult.BROKER_UNREACHABLE.value
        assert "timeout" in (result.error or "").lower()
