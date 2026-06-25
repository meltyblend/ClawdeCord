# Clawde — a Claude-powered Discord bot

A small Discord bot that answers when you `@mention` it, powered by Anthropic's
Claude. It reads the day's prior messages in the channel so it can follow up,
summarize, and stay on topic. Run it on your own server with your own keys.

- Replies to any message that `@mentions` the bot, in any channel it can see.
- Feeds Claude today's conversation (resets at local midnight PST Los Angeles) as context.
- Splits long answers across messages without breaking code blocks.
- Optional one-time startup greeting in a channel of your choice.

---

## Requirements

- Python 3.11 or newer (it uses `zoneinfo` and `int | None` syntax).
- A Discord account and a server where you can add a bot.
- An Anthropic API key.

---

## 1. Install

```bash
git clone <your-repo-url>
cd <your-repo>
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Create the Discord application and bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   and click **New Application**. Give it a name.
2. Open the **Bot** tab. The bot user is created automatically.
3. Click **Reset Token**, copy the token — this is your `BOT_TOKEN`.
   (You only see it once; reset again if you lose it.)
4. On the same **Bot** tab, scroll to **Privileged Gateway Intents** and turn on
   **Message Content Intent**. The bot needs this to read what people type.
   *(Members and Presence intents are not required.)*

## 3. Invite the bot to your server

1. Open the **OAuth2 → URL Generator** tab.
2. Under **Scopes**, check **`bot`**.
3. Under **Bot Permissions**>>**Text Permissions/General Permissions**, check:
   - **Send Messages**
   - **Create Public Threads**
   - **Send Messages in Threads**
   - **Embed Links**
   - **Attach Files**
   - **Read Message History** — required; without it the bot errors on every
     mention while trying to read the day's context.
   - **Mention Everyone** - I allowed this just cause (personal preference)
   - **Add Reactions**
   - **Create Polls**
   - **Bypass Slowmode** (Optional)
   - **View Channels**
   - **Change Nickname**
5. Copy the generated URL at the bottom, open it in your browser, and add the
   bot to your server.

## 4. Get your Anthropic API key

Create one at the [Anthropic Console](https://console.anthropic.com/) under
**Settings → API Keys**. This is your `ANTHROPIC_API_KEY`.

## 5. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable             | Required | What it is                                                        |
| -------------------- | -------- | ----------------------------------------------------------------- |
| `BOT_TOKEN`          | yes      | The bot token from step 2.                                        |
| `ANTHROPIC_API_KEY`  | yes      | Your Anthropic key from step 4.                                   |
| `GREETING_CHANNEL_ID`| no       | Channel ID for the startup greeting. Leave blank to skip it.      |

To get a channel ID: enable **User Settings → Advanced → Developer Mode**, then
right-click a channel and choose **Copy Channel ID**.

If a required variable is missing, the bot exits at startup and tells you
exactly which one.

## 6. Run

```bash
python bot.py
```

You should see a log line like `Logged in as Clawde#1234`. Mention the bot in
your server to talk to it:

```
@Clawde what did we decide about the meeting time?
```

---

## Customizing

All knobs live near the top of `bot.py`:

- **`CLAUDE_MODEL`** — which Claude model to use.
- **`CLAUDE_SYSTEM_PROMPT`** — the bot's personality and instructions.
- **`CLAUDE_MAX_TOKENS`** — max length of each reply.
- **`LOCAL_TZ`** — timezone whose midnight resets the daily context window.
- **`HISTORY_MESSAGE_CAP`** — how many of today's messages to feed Claude.
- **`PER_USER_COOLDOWN_SECONDS`** — minimum seconds between one user's mentions,
  to guard against API-bill spikes from spam. Set to `0` to disable.

**Restrict the bot to one channel.** By default it replies to mentions
everywhere it can see. To limit it to a single channel, set
`GREETING_CHANNEL_ID` and change the condition in `on_message` from
`if bot.user in message.mentions:` to
`if bot.user in message.mentions and message.channel.id == GREETING_CHANNEL_ID:`.

---

## Troubleshooting

- **Exits immediately with "Missing required environment variable".** Your
  `.env` isn't filled in, or you're running from a different directory than the
  `.env` file. Run from the project root.
- **Bot logs in but never responds to mentions.** Message Content Intent is
  probably off — re-check step 2.4.
- **Replies fail / warning about Read Message History.** The invite didn't grant
  that permission. Re-invite with the permission checked, or adjust the bot's
  role in **Server Settings → Roles**.
- **401 / authentication error from Anthropic.** The `ANTHROPIC_API_KEY` is
  wrong or unset.

---

## Cost note

Every mention sends today's channel transcript plus the question to the
Anthropic API, which costs money against the host's account. The per-user
cooldown limits casual spam, but anyone in your server can run up usage. Keep
the bot in trusted servers, or tighten `PER_USER_COOLDOWN_SECONDS`,
`HISTORY_MESSAGE_CAP`, and `CLAUDE_MAX_TOKENS` to cap spend.
