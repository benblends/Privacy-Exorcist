"""
Browser Operator — vision-driven data broker automation.

SPEC-002 §5 Phase 5: Wires browser_factory, task_builder, capsolver_action,
and result_mapper into a single async execute() method.

Usage:
    operator = BrowserOperator(openai_key=..., capsolver_key=..., headless=True)
    result = await operator.execute(task_context)
    # result is a BrokerRunResult ready for Orchestrator.finish_broker()
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from browser_use import Agent
from browser_use.llm import ChatOpenAI

from privacy_exorcist.browser_operator.browser_factory import create_browser_profile
from privacy_exorcist.browser_operator.capsolver_action import create_capsolver_controller
from privacy_exorcist.browser_operator.result_mapper import map_result
from privacy_exorcist.browser_operator.task_builder import build_task
from privacy_exorcist.engine import BrokerRunResult, TaskContext


# ── Timeout ────────────────────────────────────────────────────────────────

DEFAULT_AGENT_TIMEOUT = 300  # 5 minutes per broker


# ── BrowserOperator ────────────────────────────────────────────────────────

class BrowserOperator:
    """Executes data broker opt-out flows using browser-use + CapSolver.

    One instance per run. Create fresh for each orchestrator session.
    """

    def __init__(
        self,
        openai_key: str,
        capsolver_key: Optional[str] = None,
        headless: bool = True,
        agent_timeout: int = DEFAULT_AGENT_TIMEOUT,
    ):
        self._openai_key = openai_key
        self._capsolver_key = capsolver_key
        self._headless = headless
        self._agent_timeout = agent_timeout

    # ── Public API ───────────────────────────────────────────────────────

    async def execute(self, ctx: TaskContext) -> BrokerRunResult:
        """Run the full browser automation lifecycle for one broker.

        Args:
            ctx: TaskContext from Orchestrator.start_broker().

        Returns:
            BrokerRunResult with outcome, duration, final_state, etc.
        """
        start = time.monotonic()
        broker_id = ctx.broker_id
        captcha_solved = False

        try:
            # 1. Build task string
            task = build_task(ctx)

            # 2. Create stealth browser profile
            browser_profile = create_browser_profile(headless=ctx.headless)

            # 3. Create CapSolver controller
            controller = create_capsolver_controller(
                self._capsolver_key, ctx.playbook_entry, headless=ctx.headless
            )

            # 4. Build LLM
            llm = ChatOpenAI(
                model="gpt-4o",
                api_key=self._openai_key,
                temperature=0.1,
            )

            # 5. Build and run agent
            agent = Agent(
                task=task,
                llm=llm,
                browser_profile=browser_profile,
                controller=controller,
            )

            # Run with timeout
            history = await asyncio.wait_for(
                agent.run(), timeout=self._agent_timeout
            )

            # 6. Extract final result
            final_text = ""
            if history is not None:
                if hasattr(history, "final_result"):
                    final_text = str(history.final_result() or "")[:3000]
                else:
                    final_text = str(history)[:3000]

            # 7. Map to BrokerResult
            outcome = map_result(final_text, ctx.playbook_entry.success_anchor)

            # 8. Detect if CAPTCHA was solved
            if "turnstile captcha solved" in final_text.lower():
                captcha_solved = True
            if "capsolver" in final_text.lower():
                captcha_solved = True

            duration = time.monotonic() - start

            return BrokerRunResult(
                broker_id=broker_id,
                outcome=outcome,
                duration_seconds=round(duration, 1),
                final_state=final_text,
                captcha_solved=captcha_solved,
            )

        except asyncio.TimeoutError:
            duration = time.monotonic() - start
            return BrokerRunResult(
                broker_id=broker_id,
                outcome="BROKER_UNREACHABLE",
                duration_seconds=round(duration, 1),
                final_state="Agent timed out",
                error=f"Agent execution exceeded {self._agent_timeout}s timeout",
            )

        except Exception as e:
            duration = time.monotonic() - start
            return BrokerRunResult(
                broker_id=broker_id,
                outcome="BROKER_UNREACHABLE",
                duration_seconds=round(duration, 1),
                final_state=str(e)[:3000],
                error=str(e),
            )
