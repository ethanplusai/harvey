"""Brain — Harvey's thinking engine.

Runs on the operator's Claude subscription (Claude Code headless, the
default) or, when ``brain.provider: codex`` is configured, their ChatGPT
subscription through an isolated Codex runtime (see harvey/codex.py). The
provider is fixed per Brain; a failing provider never falls back to the
other one.
"""

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from harvey.state import StateManager

logger = logging.getLogger("harvey.brain")

from harvey.paths import PROJECT_ROOT
from harvey.textio import read_user_text

PROMPTS_DIR = PROJECT_ROOT / "prompts"
SKILLS_DIR = PROJECT_ROOT / "skills"

# Subprocess safety limits
DEFAULT_TIMEOUT_SECONDS = 300  # a single Claude call should never hang forever
DEFAULT_MAX_RETRIES = 2  # retries on transient failures (non-zero exit, timeout)
RETRY_BASE_DELAY = 5.0  # seconds; doubles per attempt
KILL_REAP_SECONDS = 10.0  # waiting for a killed child on shutdown

# stderr fragments that indicate retrying is pointless
_NON_RETRYABLE_PATTERNS = (
    "not logged in",
    "please run `claude login`",
    "invalid api key",
    "unauthorized",
)


PROVIDER_LABELS = {"claude": "Claude", "codex": "Codex"}

# Settings key holding the brain's last health transition, for `harvey
# status` and the dashboard: {"provider", "status": "ok"|"paused", "reason",
# "since", and for pauses "kind" (plus "until", epoch seconds, for usage limits)}.
BRAIN_STATUS_KEY = "brain_status"

# Codex failures that repeat on every call until the operator acts (login,
# CLI version, model choice, an unsafe Codex config layer). After one, the
# Brain stops starting runtimes until something changes.
_PERSISTENT_CODEX_KINDS = {"setup", "auth", "isolation"}
# Re-check a paused provider at least this often even if nothing changed
# on disk (e.g. a managed config layer was fixed).
PROVIDER_RECHECK_SECONDS = 1800
# After Codex reports the plan's usage limit, stop calling it (and keep the
# budget gate closed) for this long.
USAGE_LIMIT_PAUSE_SECONDS = 3600


def parse_brain_status(raw: str) -> dict:
    """The stored BRAIN_STATUS_KEY value as a dict ({} if absent or bad)."""
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def active_brain_status(raw: str, provider: str) -> dict:
    """The stored status if it belongs to ``provider`` (the one configured
    now), else {}: a pause recorded for Codex says nothing about Claude."""
    status = parse_brain_status(raw)
    return status if status.get("provider") == provider else {}


class BrainUnavailable(Exception):
    """The provider can't be used until the operator acts. Raised only where
    a caller must leave its input untouched for a later retry."""


class Brain:
    def __init__(self, state: StateManager, brain_config=None):
        from harvey.config import BrainConfig

        self.state = state
        self.config = brain_config or BrainConfig()
        self.provider = self.config.provider
        self.model = self.config.model
        self.codex = None
        # Whether the last think_json got no reply at all (an outage, logout,
        # rate limit or timeout), as opposed to a reply that wasn't valid
        # JSON. Callers that count model failures must not count outages.
        self.last_reply_empty = False
        # Codex health (unused for Claude, whose behaviour is unchanged).
        self.unavailable = ""
        self._unavailable_at = 0.0
        self._unavailable_fingerprint = None
        self._usage_limited_until = 0.0
        self._usage_pause_recorded = False
        self._status_synced = False
        if self.provider == "codex":
            from harvey.codex import CodexClient, CodexQuotaClient
            self.codex = CodexClient(
                model=self.model, reasoning_effort=self.config.reasoning_effort
            )
            self.quota = CodexQuotaClient(self.codex)
        else:
            # Lazy import avoids a cycle (quota -> usage -> state).
            from harvey.integrations.quota import QuotaClient
            self.quota = QuotaClient()

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS[self.provider]

    @property
    def paused_reason(self) -> str:
        """Why calls are currently being skipped ("" when they aren't)."""
        if self.unavailable:
            return self.unavailable
        if time.monotonic() < self._usage_limited_until:
            return f"{self.provider_label} usage limit reached for this plan"
        return ""

    async def preflight(self) -> str:
        """Can this Brain be used right now? Returns "" or the reason not.

        Codex only (Claude keeps its existing behaviour): runs the adapter's
        full pre-prompt check (CodexClient.preflight): CLI version, route and
        lockdown attested with no login in the runtime, the native login read
        only (expiry, renewal), then with the access-only copy the configured
        model against the catalog, the ChatGPT account, and a started
        tool-less thread. No turn, so no inference; nothing is written to the
        native login. While paused, the check is repeated only when the login
        file or the Codex executable changes, or every
        PROVIDER_RECHECK_SECONDS, so a persistent problem doesn't start
        runtimes every heartbeat.
        """
        if self.provider != "codex":
            return await self._claude_preflight()
        from harvey.codex import CodexError

        await self._expire_usage_pause()
        if self.unavailable:
            unchanged = self.codex.fingerprint() == self._unavailable_fingerprint
            recent = time.monotonic() - self._unavailable_at < PROVIDER_RECHECK_SECONDS
            if unchanged and recent:
                return self.unavailable
        try:
            await self.codex.preflight()
        except CodexError as e:
            if e.kind in _PERSISTENT_CODEX_KINDS:
                await self._mark_unavailable(e)
                return self.unavailable
            return str(e)  # transient: skip this cycle, check again next time
        except Exception as e:
            return f"Codex preflight failed unexpectedly ({type(e).__name__})."
        if self.unavailable:
            await self._mark_available()
        elif not self._status_synced:
            logger.info(
                f"Codex ready: supported CLI, isolation verified, login and account "
                f"valid, model {self.model or 'auto'} usable with no tools (no prompt sent).")
            # A pause recorded before a restart must not outlive the problem.
            self._status_synced = True
            try:
                stored = parse_brain_status(await self.state.get_setting(BRAIN_STATUS_KEY))
            except Exception:
                stored = {}
            until = stored.get("until")
            if (stored.get("status") == "paused" and stored.get("kind") == "usage_limit"
                    and isinstance(until, (int, float)) and until > time.time()):
                # Still inside a usage-limit pause recorded before a restart.
                self._usage_limited_until = time.monotonic() + (until - time.time())
                self._usage_pause_recorded = True
            elif stored.get("status") == "paused":
                await self._mark_available()
        return ""

    async def _claude_preflight(self) -> str:
        """Claude: the installed CLI must support every isolation flag
        (claude_runtime.check_cli). Runs ``claude --help`` only: no login
        check, no model call. Returns "" or why calls are paused."""
        from harvey.claude_runtime import ClaudeRuntimeError, check_cli

        try:
            await asyncio.to_thread(check_cli)
        except ClaudeRuntimeError as e:
            await self._mark_unavailable(e)
            return self.unavailable
        if self.unavailable:
            await self._mark_available()
        return ""

    def _fingerprint(self):
        if self.provider == "codex":
            return self.codex.fingerprint()
        from harvey.claude_runtime import fingerprint
        return fingerprint()

    async def _start_usage_pause(self):
        self._usage_limited_until = time.monotonic() + USAGE_LIMIT_PAUSE_SECONDS
        until = time.time() + USAGE_LIMIT_PAUSE_SECONDS
        resume = time.strftime("%H:%M UTC", time.gmtime(until))
        await self._record_status(
            "paused",
            f"{self.provider_label} usage limit reached for this plan; calls resume "
            f"after {resume}.",
            kind="usage_limit", until=until)
        self._usage_pause_recorded = True

    async def _expire_usage_pause(self):
        """Record recovery once a usage-limit pause has run out."""
        if self._usage_pause_recorded and time.monotonic() >= self._usage_limited_until:
            self._usage_pause_recorded = False
            if not self.unavailable:
                logger.info(f"{self.provider_label} usage-limit pause is over; resuming.")
                await self._record_status("ok", "")

    async def _mark_unavailable(self, error):
        first = not self.unavailable or self.unavailable != str(error)
        self.unavailable = str(error)
        self._unavailable_at = time.monotonic()
        self._unavailable_fingerprint = self._fingerprint()
        if first:
            await self._record_status("paused", self.unavailable,
                                      kind=getattr(error, "kind", "setup"))

    async def _mark_available(self):
        logger.info(f"{self.provider_label} is available again; resuming.")
        self.unavailable = ""
        self._unavailable_fingerprint = None
        await self._record_status("ok", "")

    async def _record_status(self, status: str, reason: str, **extra):
        try:
            await self.state.set_setting(BRAIN_STATUS_KEY, json.dumps({
                "provider": self.provider, "status": status, "reason": reason[:500],
                "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                **extra,
            }))
        except Exception as e:
            logger.warning(f"Failed to record brain status: {e}")

    async def think(
        self,
        prompt: str,
        session_id: str | None = None,
        expect_json: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        agent: str = "",
        task: str = "",
    ) -> str:
        """Send a prompt to the configured provider and return the response.

        Retries transient failures with exponential backoff and enforces a
        hard timeout so a hung CLI call can never stall the heartbeat.
        Returns "" on unrecoverable failure (callers already handle empty).

        ``agent``/``task`` label the call in the usage_events ledger so the
        dashboard can attribute tokens and cost to specific work.
        """
        if self.provider == "codex":
            return await self._think_codex(
                prompt, session_id=session_id, timeout=timeout,
                max_retries=max_retries, agent=agent, task=task,
            )

        from harvey import claude_runtime

        # Never start an unisolated call: a CLI missing any isolation flag
        # pauses Claude (like a Codex setup problem) until it's updated.
        try:
            executable = await asyncio.to_thread(claude_runtime.check_cli)
        except claude_runtime.ClaudeRuntimeError as e:
            logger.error(f"Claude call not sent: {e}")
            await self._mark_unavailable(e)
            return ""
        if self.unavailable:
            await self._mark_available()

        # The prompt always travels on stdin: argv is visible to other local
        # users, Windows caps it at ~32k chars, and .cmd shims re-parse it.
        cmd = claude_runtime.build_command(executable, self.model, session_id)

        logger.debug(f"Brain call (session={session_id}): {prompt[:100]}...")

        last_error = ""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.info(
                    f"Retrying brain call in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{max_retries + 1})..."
                )
                await asyncio.sleep(delay)

            process = None
            try:
                # The directory outlives the child: it's removed only after
                # the process has exited (Windows can't delete a live cwd).
                with claude_runtime.isolated_cwd() as cwd:
                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        # A fresh empty directory: never the checkout (whose
                        # CLAUDE.md is the setup wizard) or its ancestors.
                        cwd=cwd,
                        env=claude_runtime.runtime_env(cwd),
                        # A pipe, never the inherited terminal.
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        stdout, stderr = await asyncio.wait_for(
                            process.communicate(prompt.encode("utf-8")),
                            timeout=timeout,
                        )
                    except asyncio.TimeoutError:
                        logger.error(
                            f"Claude call timed out after {timeout:.0f}s. Killing process.")
                        try:
                            process.kill()
                            await process.wait()
                        except ProcessLookupError:
                            pass
                        stdout = None
                    except BaseException:
                        # Shutting down: stop and reap the child before its
                        # directory is removed, then let the cancellation
                        # through. The reap is bounded so a stuck child can't
                        # hold shutdown; a second cancel ends it at once.
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        try:
                            await asyncio.wait_for(process.wait(),
                                                   timeout=KILL_REAP_SECONDS)
                        except Exception as e:
                            logger.warning(f"Claude child not reaped on shutdown: {e!r}")
                        raise
                if stdout is None:
                    last_error = "timeout"
                    continue  # retry

                if process.returncode != 0:
                    error = stderr.decode(errors="replace").strip()
                    logger.error(
                        f"Claude exited with code {process.returncode}: {error[:300]}"
                    )
                    last_error = error
                    if any(p in error.lower() for p in _NON_RETRYABLE_PATTERNS):
                        logger.error(
                            "Non-retryable Claude error (auth). "
                            "Run 'claude login' and restart Harvey."
                        )
                        return ""
                    continue  # retry transient failures

                raw = stdout.decode(errors="replace").strip()
                response, payload = self._parse_result_payload(raw)

                # Usage accounting must never break the response path.
                try:
                    await self.state.increment_usage()
                except Exception as e:
                    logger.warning(f"Failed to record usage counter: {e}")
                try:
                    await self._record_usage(payload, agent=agent, task=task,
                                             label=session_id or "")
                except Exception as e:
                    logger.warning(f"Failed to record usage event: {e}")

                if payload and payload.get("is_error"):
                    # The CLI reported an API error (overloaded, rate limited)
                    # inside a successful exit. That text is not the model's
                    # answer, so no caller may use it as one: retry like any
                    # other transient failure.
                    logger.error(f"Claude reported an error: {response[:300]}")
                    last_error = response
                    continue

                logger.debug(f"Brain response: {response[:200]}...")
                return response

            except FileNotFoundError:
                logger.error(
                    "Claude CLI not found. Install it: https://code.claude.com/docs/en/setup"
                )
                return ""  # not retryable
            except asyncio.CancelledError:
                # Shutting down — kill the child so it doesn't orphan
                if process is not None and process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                raise
            except Exception as e:
                logger.error(f"Brain error: {e}")
                last_error = str(e)
                continue

        logger.error(
            f"Brain call failed after {max_retries + 1} attempts. "
            f"Last error: {str(last_error)[:200]}"
        )
        return ""

    async def _think_codex(
        self,
        prompt: str,
        session_id: str | None,
        timeout: float,
        max_retries: int,
        agent: str,
        task: str,
    ) -> str:
        """One isolated Codex turn per attempt (see harvey/codex.py)."""
        from harvey.codex import CodexError

        paused = self.paused_reason
        if paused:
            # Known not to work until the operator acts (or the limit
            # resets): don't start a runtime per call.
            logger.debug(f"Codex call skipped: {paused}")
            return ""
        logger.debug(f"Brain call via Codex (label={session_id})")
        last_error = ""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.info(
                    f"Retrying Codex call in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{max_retries + 1})..."
                )
                await asyncio.sleep(delay)
            try:
                result = await self.codex.run(
                    prompt, timeout=timeout, skip_if=lambda: self.paused_reason)
            except CodexError as e:
                if e.kind == "paused":
                    logger.debug(f"Codex call skipped: {e}")
                    return ""
                last_error = str(e)
                if e.retryable:
                    logger.warning(f"Codex call failed: {e}")
                    continue
                logger.error(f"Codex call failed: {e}")
                if e.kind in _PERSISTENT_CODEX_KINDS:
                    await self._mark_unavailable(e)
                elif e.kind == "usage_limit":
                    await self._start_usage_pause()
                    invalidate = getattr(self.quota, "invalidate", None)
                    if invalidate:
                        invalidate()
                return ""
            except Exception as e:
                # Not expected (the adapter reports failures as CodexError);
                # log only the type, since messages could carry call details.
                # CancelledError is not an Exception, so shutdown propagates.
                logger.error(f"Codex call failed unexpectedly ({type(e).__name__}).")
                return ""

            try:
                await self.state.increment_usage(provider="codex")
            except Exception as e:
                logger.warning(f"Failed to record usage counter: {e}")
            try:
                await self._record_codex_usage(result, agent=agent, task=task,
                                               label=session_id or "")
            except Exception as e:
                logger.warning(f"Failed to record usage event: {e}")
            return result.text

        logger.error(
            f"Codex call failed after {max_retries + 1} attempts. "
            f"Last error: {last_error[:200]}"
        )
        return ""

    async def _record_codex_usage(self, result, agent: str, task: str, label: str):
        """One usage row per Codex call. ChatGPT plans aren't billed per
        token, so cost is recorded as 0 rather than estimated."""
        if not agent and label.startswith("harvey-"):
            agent = label[len("harvey-"):].split("-")[0]
        await self.state.record_usage_event(
            agent=agent,
            task=task or label,
            session_id=result.thread_id,
            model=result.model,
            duration_ms=result.duration_ms,
            num_turns=1,
            source="codex_app_server",
            provider="codex",
            **result.usage,
        )

    @staticmethod
    def _parse_result_payload(raw: str) -> tuple[str, dict | None]:
        """Split CLI JSON output into (response_text, result_payload).

        With ``--output-format json`` stdout is one JSON object whose
        ``result`` field is the model's text. If parsing fails (older CLI,
        truncated output), fall back to treating stdout as plain text so a
        formatting change can never break Harvey's pipeline.
        """
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw, None
        if not isinstance(payload, dict) or payload.get("type") != "result":
            return raw, None
        return str(payload.get("result") or ""), payload

    async def _record_usage(
        self, payload: dict | None, agent: str, task: str, label: str
    ):
        """Write per-model usage rows from a CLI result payload."""
        if not payload:
            return

        if not agent and label.startswith("harvey-"):
            # Callers historically pass labels like "harvey-scout-score".
            agent = label[len("harvey-"):].split("-")[0]

        session = str(payload.get("session_id") or "")
        duration_ms = int(payload.get("duration_ms") or 0)
        num_turns = int(payload.get("num_turns") or 0)
        is_error = bool(payload.get("is_error"))

        # modelUsage includes subagent spend; the flat `usage` field does not.
        model_usage = payload.get("modelUsage")
        rows = []
        if isinstance(model_usage, dict) and model_usage:
            for model, mu in model_usage.items():
                if not isinstance(mu, dict):
                    continue
                rows.append({
                    "model": str(model),
                    "input_tokens": int(mu.get("inputTokens") or 0),
                    "output_tokens": int(mu.get("outputTokens") or 0),
                    "cache_read_tokens": int(mu.get("cacheReadInputTokens") or 0),
                    "cache_creation_tokens": int(mu.get("cacheCreationInputTokens") or 0),
                    "cost_usd": float(mu.get("costUSD") or 0.0),
                })
        else:
            usage = payload.get("usage")
            if isinstance(usage, dict):
                rows.append({
                    "model": "",
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
                    "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
                    "cost_usd": float(payload.get("total_cost_usd") or 0.0),
                })

        for row in rows:
            await self.state.record_usage_event(
                agent=agent,
                task=task or label,
                session_id=session,
                duration_ms=duration_ms,
                num_turns=num_turns,
                is_error=is_error,
                source="result_json",
                provider="claude",
                **row,
            )

    async def think_json(
        self,
        prompt: str,
        session_id: str | None = None,
        agent: str = "",
        task: str = "",
    ) -> dict | list | None:
        """Send a prompt and parse the response as JSON."""
        full_prompt = (
            prompt
            + "\n\nRespond ONLY with valid JSON. No markdown, no explanation."
        )
        response = await self.think(
            full_prompt, session_id=session_id, agent=agent, task=task
        )
        self.last_reply_empty = not response
        if not response:
            return None
        parsed = self._extract_json(response)
        if parsed is None:
            logger.error(f"Failed to parse JSON from brain: {response[:200]}")
        return parsed

    @staticmethod
    def _extract_json(text: str) -> dict | list | None:
        """Best-effort JSON extraction: strips code fences, then falls back to
        locating the outermost JSON object/array in surrounding prose."""
        cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        # Fall back: model wrapped the JSON in explanation text
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            start = cleaned.find(open_ch)
            end = cleaned.rfind(close_ch)
            if start != -1 and end > start:
                try:
                    return json.loads(cleaned[start : end + 1])
                except json.JSONDecodeError:
                    continue
        return None

    async def check_usage(self) -> float:
        """Check today's usage on this Brain's provider (own call count).

        Returns the number of calls made today. Errors are treated as
        "over budget is unknown" and return a safe 0.0 so a broken DB read
        doesn't crash the heartbeat.
        """
        try:
            return await self.state.get_usage_today(provider=self.provider)
        except Exception as e:
            logger.warning(f"Could not read usage from state: {e}")
            return 0.0

    async def is_within_budget(
        self, max_daily_calls: int = 200, max_percent: float | None = None
    ) -> bool:
        """Check whether Harvey may spend more of the active subscription.

        Preferred signal: the account's REAL utilization windows (Claude's
        `/usage` numbers, or Codex's ChatGPT-plan rate limits), compared
        against max_percent so Harvey always leaves the remainder for the
        user's own interactive work. Fallback when those are unavailable:
        Harvey's own call counter for this provider against max_daily_calls.
        """
        await self._expire_usage_pause()
        if time.monotonic() < self._usage_limited_until:
            # Codex said the plan is out of usage: an unreadable quota must
            # not be mistaken for room to spend.
            logger.info(f"{self.paused_reason}; waiting for it to reset.")
            return False
        if max_percent is not None:
            try:
                windows = await self.quota.get_utilization()
            except Exception as e:
                logger.debug(f"Quota check errored: {e}")
                windows = None
            if windows:
                worst = max(
                    (w["utilization"] for w in windows.values()), default=0.0
                )
                within = worst < max_percent
                if not within:
                    resets = ", ".join(
                        f"{name} resets {w['resets_at']}"
                        for name, w in windows.items()
                        if w.get("resets_at")
                    )
                    logger.info(
                        f"{self.provider_label} quota gate: utilization {worst:.0f}% >= "
                        f"{max_percent:.0f}% limit. {resets}"
                    )
                return within

        calls = await self.check_usage()
        return calls < max_daily_calls

    def load_prompt(self, prompt_name: str, **kwargs) -> str:
        """Load a prompt template from the prompts/ directory and fill in variables."""
        prompt_file = PROMPTS_DIR / f"{prompt_name}.md"
        try:
            template = read_user_text(prompt_file)
        except FileNotFoundError:
            logger.warning(f"Prompt file not found: {prompt_file}")
            return ""
        except OSError as e:
            logger.error(f"Could not read prompt file {prompt_file}: {e}")
            return ""
        for key, value in kwargs.items():
            template = template.replace(f"{{{{{key}}}}}", str(value))
        # Surface templating mistakes early instead of sending {{foo}} to Claude
        leftover = re.findall(r"\{\{(\w+)\}\}", template)
        if leftover:
            logger.warning(
                f"Prompt '{prompt_name}' has unfilled variables: {sorted(set(leftover))}"
            )
        return template

    def load_skill(self, skill_name: str) -> str:
        """Load a skill knowledge file from the skills/ directory."""
        skill_file = SKILLS_DIR / f"{skill_name}.md"
        try:
            return read_user_text(skill_file)
        except FileNotFoundError:
            logger.warning(f"Skill file not found: {skill_file}")
            return ""
        except OSError as e:
            logger.error(f"Could not read skill file {skill_file}: {e}")
            return ""

    def load_skills_for_agent(self, agent_name: str) -> str:
        """Load all relevant skills for a specific sub-agent.

        Returns concatenated skill content that should be injected
        into the agent's prompts as foundational knowledge.
        """
        # product_knowledge and competitive_intel are auto-generated by the trainer
        # and loaded by every agent that needs product context
        skill_map = {
            "scout": ["prospecting_tactics", "lead_qualification", "account_navigation", "signal_playbook", "product_knowledge"],
            "writer": ["email_frameworks", "signal_playbook", "sales_methodology", "offer_strategy", "product_knowledge", "competitive_intel"],
            "handler": ["objection_handling", "sales_methodology", "offer_strategy", "product_knowledge", "competitive_intel"],
            "sender": ["email_frameworks", "product_knowledge"],
            "linkedin": ["linkedin_outreach", "prospecting_tactics", "product_knowledge"],
        }

        skill_names = skill_map.get(agent_name, [])
        if not skill_names:
            return ""

        sections = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                sections.append(content)

        if not sections:
            return ""

        return (
            "\n\n---\n## FOUNDATIONAL KNOWLEDGE\n"
            "Use the following frameworks and best practices to guide your work:\n\n"
            + "\n\n---\n\n".join(sections)
        )
