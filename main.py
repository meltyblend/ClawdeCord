import base64
import io
import os
import re
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import anthropic
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables from the .env file into os.environ
load_dotenv()

# Secrets/config pulled from .env so they aren't hardcoded in source
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
CHANNEL_ID2 = int(os.getenv("CHANNEL_ID2"))

# Create the bot. `!` is the prefix for text commands (e.g. !ping),
# and Intents.all() subscribes to every gateway event Discord offers.
bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())

# Async Anthropic client — reads ANTHROPIC_API_KEY from the environment.
# Async so calls don't block discord.py's event loop.
claude = anthropic.AsyncAnthropic()

CLAUDE_MODEL = "claude-haiku-4-5"
CLAUDE_SYSTEM_PROMPT = (
    # --- Who the bot is ---
    "You're Clawde, a regular member of this Discord server, not a corporate assistant. "
    "You're laid-back, a little witty, and you talk like a real person in chat — casual, "
    "lowercase-friendly, the occasional emoji or bit of slang, but never forced or cringe. "
    "Match the energy of whoever you're talking to: playful when they're joking, genuine and "
    "helpful when they actually need something. "
    # --- How to write ---
    "Keep replies short and chatty — usually a sentence or two, under ~500 characters. Don't "
    "lecture, don't pad with disclaimers, and don't end every message asking if they need more help. "
    "Skip the assistant-y phrases like 'Certainly!' or 'I'd be happy to.' Just talk. "
    "It's fine to have opinions and to push back or tease a little. "
    # --- Formatting ---
    "You can use Discord Markdown when it helps: *italics*, **bold**, __underline__, `inline code`, "
    "> block quotes, and fenced code blocks. Don't over-format casual chatter, though. "
    "When asked for a file or script, put the code in a single fenced code block tagged with the "
    "correct file extension as the language identifier (e.g. ```py, ```js, ```sh) so it can be saved "
    "directly as a file. "
    # --- Context ---
    "Each user message includes today's prior conversation in the channel as context — use it to "
    "follow the vibe and reference what's been said when relevant, but don't rehash it unprompted."
)
CLAUDE_MAX_TOKENS = 1024

# Server-side web search. Claude decides when to search, runs the queries on
# Anthropic's infra, and grounds its reply in the results — no separate search
# API needed. Haiku 4.5 uses the basic variant (the _20260209 dynamic-filtering
# variant requires Opus/Sonnet 4.6+).
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}

# Image types Claude's vision API accepts.
SUPPORTED_IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
# Anthropic's per-image size cap.
MAX_IMAGE_BYTES = 5 * 1024 * 1024

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


async def fetch_today_transcript(channel, before_message=None) -> str:
    """Pull up to HISTORY_MESSAGE_CAP messages from `channel` posted today
    (LOCAL_TZ) and before `before_message`, formatted as a chat transcript.
    `before_message` defaults to None (up to now) so slash commands, which have
    no triggering message, can call this too."""
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


async def download_image_attachments(message: discord.Message) -> list[dict]:
    """Download any supported image attachments on `message` as Claude image content blocks."""
    blocks = []
    for attachment in message.attachments:
        if attachment.content_type not in SUPPORTED_IMAGE_TYPES:
            continue
        if attachment.size > MAX_IMAGE_BYTES:
            continue
        data = await attachment.read()
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": attachment.content_type,
                    "data": base64.b64encode(data).decode("utf-8"),
                },
            }
        )
    return blocks


async def ask_claude(question: str, asker: str, transcript: str, images: list[dict] | None = None) -> str:
    """Send the user's question (plus any attached images) + today's channel transcript to Claude."""
    if transcript:
        user_content = (
            "Today's conversation in this Discord channel so far:\n\n"
            f"{transcript}\n\n"
            "---\n"
            f"{asker} just asked: {question}"
        )
    else:
        user_content = f"{asker} asks: {question}"

    content = [*images, {"type": "text", "text": user_content}] if images else user_content

    messages = [{"role": "user", "content": content}]
    # Web search runs a server-side tool loop. Usually it finishes in one call,
    # but if it hits the loop cap it returns stop_reason "pause_turn" and we
    # re-send to let it continue. Cap our own retries so we can't spin forever.
    for _ in range(3):
        response = await claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            temperature=1.0,
            system=CLAUDE_SYSTEM_PROMPT,
            tools=[WEB_SEARCH_TOOL],
            messages=messages,
        )
        if response.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})

    # With web search the reply can hold several text blocks (Claude narrates,
    # searches, then answers), so join them all rather than taking the first.
    return "".join(b.text for b in response.content if b.type == "text").strip()


def chunk_for_discord(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split a long reply into <=limit-char chunks so Discord will accept it."""
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [""]


# Phrases that signal the user wants an actual file/script, not just an inline snippet.
FILE_REQUEST_KEYWORDS = ("file", "script", "code for", "write me", "program")

# Only allow short, plain-alphanumeric tags as extensions (no path traversal, no junk).
CODE_BLOCK_PATTERN = re.compile(r"```([a-zA-Z0-9]{1,10})?\n(.*?)```", re.DOTALL)


def wants_file(question: str) -> bool:
    """Heuristic: did the user explicitly ask for a file/script rather than a snippet?"""
    lowered = question.lower()
    return any(keyword in lowered for keyword in FILE_REQUEST_KEYWORDS)


def extract_code_block(answer: str) -> tuple[str, str] | None:
    """Pull the first fenced code block out of Claude's reply, if any.
    Trusts the block's own language tag (Claude is instructed via
    CLAUDE_SYSTEM_PROMPT to tag it with the correct file extension) so new
    languages don't require code changes here."""
    match = CODE_BLOCK_PATTERN.search(answer)
    if not match:
        return None
    language, code = match.group(1), match.group(2)
    extension = (language or "txt").lower()
    return f"snippet.{extension}", code.strip()


async def summarize_transcript(transcript: str, requester: str) -> str:
    """Ask Claude for a quick TL;DR of a channel transcript (the /catchup command)."""
    user_content = (
        "Here's the recent conversation in this Discord channel:\n\n"
        f"{transcript}\n\n"
        "---\n"
        f"{requester} just asked you to catch them up on what they missed. Give a quick, "
        "casual TL;DR — the main topics, decisions, or drama as a few short bullet points. "
        "Skip the trivial back-and-forth. If barely anything happened, just say so."
    )
    response = await claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        temperature=1.0,
        system=CLAUDE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


async def send_interaction_reply(interaction: discord.Interaction, text: str, ephemeral: bool = False) -> None:
    """Send a (possibly long) reply to a slash command, splitting to Discord's char limit.
    Assumes the interaction has already been deferred. `ephemeral` should match the
    defer's visibility so the chunks land the same way (private vs. public)."""
    for chunk in chunk_for_discord(text or "(empty response)"):
        await interaction.followup.send(chunk, ephemeral=ephemeral)


@bot.tree.command(name="ask", description="Ask Clawde anything — it'll search the web if needed.")
@app_commands.describe(
    question="What do you want to ask?",
    public="Show the answer to everyone in the channel? (default: only you)",
)
async def ask_command(interaction: discord.Interaction, question: str, public: bool = False):
    # Private (ephemeral) by default so quick lookups don't clutter the channel;
    # pass public:true to post the answer for everyone. The defer's visibility
    # carries to every followup, so we thread `ephemeral` through them all.
    ephemeral = not public
    # Defer immediately: Claude (plus any web search) can take longer than the
    # 3s window Discord gives us to acknowledge an interaction.
    await interaction.response.defer(thinking=True, ephemeral=ephemeral)
    try:
        transcript = await fetch_today_transcript(interaction.channel)
        answer = await ask_claude(question, interaction.user.display_name, transcript)
    except anthropic.APIError as e:
        await interaction.followup.send(f"Sorry, I hit an API error: {e.message}", ephemeral=ephemeral)
        return
    await send_interaction_reply(interaction, answer, ephemeral=ephemeral)


@bot.tree.command(name="catchup", description="Get a TL;DR of what you missed in this channel today.")
async def catchup_command(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        transcript = await fetch_today_transcript(interaction.channel)
        if not transcript:
            await interaction.followup.send("nothing's really happened in here today 👀")
            return
        summary = await summarize_transcript(transcript, interaction.user.display_name)
    except anthropic.APIError as e:
        await interaction.followup.send(f"Sorry, I hit an API error: {e.message}")
        return
    await send_interaction_reply(interaction, summary)


@bot.tree.command(name="help", description="See what Clawde can do.")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="👋 hey, I'm Clawde",
        description="Just a regular member of the server who happens to know things. Here's how to reach me:",
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="@mention me",
        value="Ping me with a question or just to chat. Attach an image and I'll take a look "
        "(builds, screenshots, error screens, whatever). Ask for a file or script and I'll send it as an attachment.",
        inline=False,
    )
    embed.add_field(
        name="/ask <question>",
        value="Ask me privately from the slash menu — only you see the answer, so it won't clutter chat. "
        "I'll search the web if the question needs current info. Add `public: true` to share the answer with the channel.",
        inline=False,
    )
    embed.add_field(
        name="/catchup",
        value="A quick TL;DR of what you missed in this channel today.",
        inline=False,
    )
    embed.add_field(
        name="/help",
        value="This message.",
        inline=False,
    )
    # Ephemeral: only the person who ran /help sees it, so it doesn't clutter chat.
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.event
async def on_ready():
    # Fires once the bot has connected and authenticated with Discord.
    print(f"Logged in as {bot.user}")
    # Register slash commands with Discord. Global sync can take up to an hour
    # to propagate the first time; swap to bot.tree.sync(guild=...) for instant
    # updates while testing in a single server.
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Slash command sync failed: {e}")
    print("Clawde is ready!")
    # Look up the target channel by ID and post a startup greeting.
    channel = bot.get_channel(CHANNEL_ID2)
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
        images = await download_image_attachments(message)

        if not question and not images:
            await message.reply("Ask me or say something after the mention and I'll respond!")
        else:
            # Default prompt when someone mentions the bot with just an image and no text.
            question = question or "What do you see in this image?"
            # Show the typing indicator while Claude is generating a response.
            async with message.channel.typing():
                try:
                    transcript = await fetch_today_transcript(message.channel, message)
                    answer = await ask_claude(question, message.author.display_name, transcript, images)
                except anthropic.APIError as e:
                    await message.reply(f"Sorry, I hit an API error: {e.message}")
                    return

            extracted = extract_code_block(answer) if wants_file(question) else None
            if extracted:
                # User asked for a file/script: send the code as an attachment
                # (named per the code block's own language tag) instead of inline text.
                filename, code = extracted
                caption = CODE_BLOCK_PATTERN.sub("", answer).strip() or "Here's your file:"
                file_buffer = io.BytesIO(code.encode("utf-8"))
                for chunk in chunk_for_discord(caption):
                    await message.reply(chunk)
                await message.reply(file=discord.File(file_buffer, filename=filename))
            else:
                # Send the reply, splitting if it exceeds Discord's per-message char limit.
                for chunk in chunk_for_discord(answer or "(empty response)"):
                    await message.reply(chunk)

    # Hand the message off to the commands extension so `!`-prefixed
    # commands still get dispatched (overriding on_message disables this by default).
    await bot.process_commands(message)


# Connect to Discord and start the event loop. Blocks until the bot exits.
bot.run(BOT_TOKEN)
