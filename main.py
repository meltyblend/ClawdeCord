import os

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
    # If the bot was @-mentioned in the message, send a reply.
    if bot.user in message.mentions:
        await message.channel.send("Hi Aren")
    # Hand the message off to the commands extension so `!`-prefixed
    # commands still get dispatched (overriding on_message disables this by default).
    await bot.process_commands(message)


# Connect to Discord and start the event loop. Blocks until the bot exits.
bot.run(BOT_TOKEN)
