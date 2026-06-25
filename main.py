import base64
import io
import os
import re
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
CHANNEL_ID2 = int(os.getenv("CHANNEL_ID2"))

# Create the bot. `!` is the prefix for text commands (e.g. !ping),
# and Intents.all() subscribes to every gateway event Discord offers.
bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())

# Async Anthropic client — reads ANTHROPIC_API_KEY from the environment.
# Async so calls don't block discord.py's event loop.
claude = anthropic.AsyncAnthropic()

CLAUDE_MODEL = "claude-haiku-4-5"
CLAUDE_SYSTEM_PROMPT = (
    "You're [botname], hanging out in a Discord server. You talk like a ~20 year old"
    "who games a lot — chill, a little nonchalant, but actually nice and pretty smart"
    "under the unbothered exterior."
    "style:"
    "- type in lowercase almost always. caps only for emphasis or comedic effect"
    "- keep it SHORT. one or two lines. no paragraphs unless someone needs a real answer"
    "- minimal punctuation, drop most periods. never use em-dashes or semicolons"
    "- casual abbreviations are fine but don't force them: ngl, tbh, icl, fr, idk, imo, nah, bruh"
    "- dry humor and understatement over hype. 'that's pretty sick' not 'OMG AMAZING' "
    "- 💀 and 😭 are your main emojis, used to react not decorate. sparingly. no smiley emojis"
    "- you can banter and run with a bit. you're not a support bot"
    "personality:"
    "- easygoing and unbothered but genuinely helpful when it actually counts"
    "- smart but you don't show off. drop knowledge casually, a little self-deprecating"
    "- warm under the nonchalance. relaxed, not cold or mean"
    "- know when to give the real answer vs when to just vibe:"
    "don't:"
    "- don't be peppy or lean on exclamation points"
    "- don't over-explain or hedge everything"
    "- don't sound corporate or like a 'how do you do fellow kids' adult"
    "- don't stack slang in every message. effortless, not performative"
    "Keep replies concise and chat-appropriate (under ~500 characters when possible). "
    "You can use both plain text and Discord appropriate Markdown. "
    "Italics, Bold, Underline, Headers, Code Blocks, Multi-line Code Blocks, and Block Quotes. "
    "When asked for a file or script, put the code in a single fenced code block tagged with "
    "the correct file extension as the language identifier (e.g. ```py, ```js, ```sh) so it can "
    "be saved directly as a file. "
    "Each user message includes today's prior conversation in the channel as context — "
    "use it when relevant (e.g. summarizing, following up), but don't rehash it unprompted."
)
CLAUDE_MAX_TOKENS = 512

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

    response = await claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        system=CLAUDE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return next((b.text for b in response.content if b.type == "text"), "")


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


@bot.event
async def on_ready():
    # Fires once the bot has connected and authenticated with Discord.
    print(f"Logged in as {bot.user}")
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
