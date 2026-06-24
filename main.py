import os
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import anthropic
import discord
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables from the .env file into os.environ
load_dotenv()

# Secrets/config pulled from .env so they aren't hardcoded in source
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

# Create the bot. `!` is the prefix for text commands (e.g. !ping),
# and Intents.all() subscribes to every gateway event Discord offers.
bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())

# Async Anthropic client — reads ANTHROPIC_API_KEY from the environment.
# Async so calls don't block discord.py's event loop.
claude = anthropic.AsyncAnthropic()

CLAUDE_MODEL = "claude-haiku-4-5"
CLAUDE_SYSTEM_PROMPT = (
    "You are Clawde, a helpful assistant living in a Discord channel. "
    "Keep replies concise and chat-appropriate (under ~500 characters when possible). "
    "You can use both plain text and Discord appropriate Markdown. "
    "Italics, Bold, Underline, Headers, Code Blocks, Multi-line Code Blocks, and Block Quotes. "
    "Each user message includes today's prior conversation in the channel as context — "
    "use it when relevant (e.g. summarizing, following up), but don't rehash it unprompted."
)
CLAUDE_MAX_TOKENS = 512

# Discord caps individual messages at 2000 characters.
DISCORD_MESSAGE_LIMIT = 2000

# Daily-context settings: the conversation log handed to Claude resets at
# local midnight in this timezone, and is capped at this many messages.
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
HISTORY_MESSAGE_CAP = 500


def start_of_today_utc() -> datetime:
    """Return the UTC datetime corresponding to today's midnight in LOCAL_TZ."""
    now_local = datetime.now(LOCAL_TZ)
    start_local = datetime.combine(now_local.date(), time.min, tzinfo=LOCAL_TZ)
    return start_local.astimezone(timezone.utc)


async def fetch_today_transcript(channel, before_message) -> str:
    """Pull up to HISTORY_MESSAGE_CAP messages from `channel` posted today
    (LOCAL_TZ) and before `before_message`, formatted as a chat transcript."""
    start = start_of_today_utc()
    # Newest-first + cap means we keep the *recent* tail if the day is huge.
    recent = [
        msg
        async for msg in channel.history(
            after=start, before=before_message, limit=HISTORY_MESSAGE_CAP, oldest_first=False
        )
    ]
    recent.reverse()  # transcript reads top-down chronologically

    lines = []
    for msg in recent:
        if not msg.content:
            continue  # skip embed-only / attachment-only messages
        local_time = msg.created_at.astimezone(LOCAL_TZ).strftime("%-I:%M %p")
        lines.append(f"[{local_time}] {msg.author.display_name}: {msg.content}")
    return "\n".join(lines)


async def ask_claude(question: str, asker: str, transcript: str) -> str:
    """Send the user's question + today's channel transcript to Claude."""
    if transcript:
        user_content = (
            "Today's conversation in this Discord channel so far:\n\n"
            f"{transcript}\n\n"
            "---\n"
            f"{asker} just asked: {question}"
        )
    else:
        user_content = f"{asker} asks: {question}"

    response = await claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        system=CLAUDE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return next((b.text for b in response.content if b.type == "text"), "")


def chunk_for_discord(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split a long reply into <=limit-char chunks so Discord will accept it."""
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [""]


@bot.event
async def on_ready():
    # Fires once the bot has connected and authenticated with Discord.
    print(f"Logged in as {bot.user}")
    print("Clawde is ready!")
    # Look up the target channel by ID and post a startup greeting.
    channel = bot.get_channel(CHANNEL_ID)
    await channel.send("Hello, I am ClawdeCord")


@bot.event
async def on_message(message):
    # Fires for every message the bot can see in any channel it's in.

    # Ignore the bot's own messages to avoid replying to itself in a loop.
    if message.author == bot.user:
        return

    # If the bot was @-mentioned, treat the rest of the message as a question for Claude.
    if bot.user in message.mentions:
        # Strip the <@bot_id> mention token(s) out to get the actual question text.
        question = message.content
        for mention in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
            question = question.replace(mention, "")
        question = question.strip()

        if not question:
            await message.reply("Ask me something after the mention and I'll answer.")
        else:
            # Show the typing indicator while Claude is generating a response.
            async with message.channel.typing():
                try:
                    transcript = await fetch_today_transcript(message.channel, message)
                    answer = await ask_claude(question, message.author.display_name, transcript)
                except anthropic.APIError as e:
                    await message.reply(f"Sorry, I hit an API error: {e.message}")
                    return

            # Send the reply, splitting if it exceeds Discord's per-message char limit.
            for chunk in chunk_for_discord(answer or "(empty response)"):
                await message.reply(chunk)

    # Hand the message off to the commands extension so `!`-prefixed
    # commands still get dispatched (overriding on_message disables this by default).
    await bot.process_commands(message)


# Connect to Discord and start the event loop. Blocks until the bot exits.
bot.run(BOT_TOKEN)
