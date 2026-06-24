import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    print("Clawde is ready!")
    channel = bot.get_channel(CHANNEL_ID)
    await channel.send("Hello, I am ClawdeCord")


bot.run(BOT_TOKEN)
