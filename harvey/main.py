"""Harvey's main heartbeat loop. Always Be Closing."""

import asyncio
import logging
import signal
import sys
from datetime import datetime, time

import pytz

from harvey.brain import Brain
from harvey.config import load_config, load_env, HarveyConfig
from harvey.state import StateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("harvey")


def in_quiet_hours(config: HarveyConfig) -> bool:
    """Check if we're currently in quiet hours."""
    qh = config.usage.quiet_hours
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz).time()
    start = time.fromisoformat(qh.start)
    end = time.fromisoformat(qh.end)

    if start <= end:
        return start <= now <= end
    else:
        # Quiet hours cross midnight (e.g., 22:00 - 07:00)
        return now >= start or now <= end


def seconds_until_quiet_hours_end(config: HarveyConfig) -> int:
    """Calculate seconds until quiet hours end."""
    qh = config.usage.quiet_hours
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz)
    end = time.fromisoformat(qh.end)
    end_today = now.replace(hour=end.hour, minute=end.minute, second=0)

    if end_today <= now:
        # End time is tomorrow
        end_today = end_today.replace(day=end_today.day + 1)

    delta = end_today - now
    return max(int(delta.total_seconds()), 60)


async def decide_next_action(brain: Brain, state: StateManager, config: HarveyConfig) -> str:
    """Ask Claude what Harvey should do next based on current state."""
    summary = await state.get_state_summary()

    prompt = brain.load_prompt(
        "system",
        company_name=config.persona.company,
        product_description=config.product.description,
    )

    prompt += f"""

Current state:
- Prospects by status: {summary['prospects']}
- Draft campaigns waiting to send: {summary['draft_campaigns']}
- Active campaigns running: {summary['active_campaigns']}
- Open conversations needing replies: {summary['open_conversations']}
- Claude calls used today: {summary['usage_today']}

Based on this state, what should I do next? Pick exactly ONE action:
- "prospect" — if we need more leads (fewer than 20 prospects with status 'new')
- "write_campaign" — if we have new prospects but no draft campaigns ready
- "send_campaign" — if we have draft campaigns ready to deploy
- "handle_replies" — if there are open conversations needing responses
- "idle" — if everything is running and nothing needs attention

Respond with ONLY the action name, nothing else."""

    response = await brain.think(prompt, session_id="harvey-decision")
    action = response.strip().strip('"').lower()

    valid_actions = {"prospect", "write_campaign", "send_campaign", "handle_replies", "idle"}
    if action not in valid_actions:
        logger.warning(f"Unknown action from brain: {action}. Defaulting to idle.")
        action = "idle"

    return action


async def heartbeat():
    """Harvey's main loop. Wakes up, decides, acts, sleeps. Repeat."""
    logger.info("=" * 60)
    logger.info("Harvey is online. Always Be Closing.")
    logger.info("=" * 60)

    config = load_config()
    env = load_env()
    state = StateManager()
    brain = Brain(state)

    await state.init_db()
    logger.info("Database initialized.")

    # Import agents here to avoid circular imports
    from harvey.agents.scout import Scout
    from harvey.agents.writer import Writer
    from harvey.agents.sender import Sender
    from harvey.agents.handler import Handler

    scout = Scout(brain, state, config, env)
    writer = Writer(brain, state, config)
    sender = Sender(brain, state, config, env)
    handler = Handler(brain, state, config, env)

    interval = config.usage.heartbeat_interval_minutes * 60
    max_calls = int(200 * (config.usage.max_daily_claude_percent / 100))

    while True:
        try:
            # 1. Check quiet hours
            if in_quiet_hours(config):
                sleep_for = seconds_until_quiet_hours_end(config)
                logger.info(f"Quiet hours. Sleeping for {sleep_for // 60} minutes.")
                await asyncio.sleep(sleep_for)
                continue

            # 2. Check usage budget
            if not await brain.is_within_budget(max_calls):
                logger.info("Daily usage limit reached. Sleeping until tomorrow.")
                # Sleep for 1 hour and re-check (date will eventually roll over)
                await asyncio.sleep(3600)
                continue

            # 3. Decide what to do
            logger.info("Thinking about what to do next...")
            action = await decide_next_action(brain, state, config)
            logger.info(f"Decision: {action}")

            # 4. Execute
            if action == "prospect":
                await scout.run()
            elif action == "write_campaign":
                await writer.run()
            elif action == "send_campaign":
                await sender.run()
            elif action == "handle_replies":
                await handler.run()
            elif action == "idle":
                logger.info("Nothing to do. Waiting for next cycle.")

            # 5. Log the action
            await state.log_action(action_type=action, agent="main")

            # 6. Sleep until next heartbeat
            logger.info(
                f"Cycle complete. Sleeping for {config.usage.heartbeat_interval_minutes} minutes."
            )
            await asyncio.sleep(interval)

        except KeyboardInterrupt:
            logger.info("Harvey shutting down. Deals don't close themselves, but I need a break.")
            break
        except Exception as e:
            logger.error(f"Error in heartbeat: {e}", exc_info=True)
            logger.info("Recovering... sleeping 60s before retry.")
            await asyncio.sleep(60)


def main():
    """Entry point."""
    # Graceful shutdown on SIGTERM (for Docker)
    loop = asyncio.new_event_loop()

    def shutdown(sig, frame):
        logger.info("Received shutdown signal.")
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.stop()

    signal.signal(signal.SIGTERM, shutdown)

    try:
        loop.run_until_complete(heartbeat())
    except KeyboardInterrupt:
        logger.info("Goodbye.")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
