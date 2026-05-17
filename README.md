# Codex Telegram Bot

A small Telegram relay for running `codex exec` from your phone.

This project lets you send messages such as:

```text
/codex summarize the current repo
/sessions
/resume 019e...
/history
```

The relay runs on your server, polls Telegram Bot API, starts Codex subprocesses locally, and sends the final Codex reply back to your Telegram chat.

## What It Does

- Runs a Telegram bot with no third-party Python dependencies.
- Allows only explicitly configured Telegram `chat_id`s.
- Starts new Codex sessions with `/codex <task>`.
- Resumes the Telegram-bound Codex session on later `/codex <task>` calls.
- Lists known Codex sessions with `/sessions`.
- Manually binds to a session with `/resume <session_id>`.
- Shows recent conversation history with `/history`.
- Shows a Telegram reply keyboard with `/menu`, `/start`, or `/help`.
- Supports `/cancel` for terminating the current Codex subprocess.

## Important Limits

This relay does not inject messages into an already open VS Code Codex chat window. It uses the Codex CLI:

```bash
codex exec ...
codex exec resume ...
```

That means it can resume CLI-recorded Codex sessions, but it is still a separate subprocess from the VS Code UI.

## Security Model

Treat this bot as a remote control surface for your development machine.

Required safeguards:

- Keep `.env` private. It contains your Telegram bot token.
- Set `TELEGRAM_ALLOWED_CHAT_IDS`; never leave it empty after setup.
- Run the relay under your normal user account, not root.
- Start with `CODEX_SANDBOX=workspace-write`.
- Use `CODEX_SANDBOX=danger-full-access` only if you understand the risk.
- Do not commit `.env`.

`.gitignore` already excludes `.env`.

## Requirements

- Python 3.10+
- A working `codex` CLI on the server
- A Telegram account
- SSH/tmux access to the server

No Python packages are required.

## Repository Layout

```text
.
├── .env.example
├── .gitignore
├── LICENSE
├── README.md
├── scripts
│   ├── start_telegram_codex_relay.sh
│   └── telegram_codex_relay_status.sh
└── src
    └── telegram_codex_relay.py
```

## Step 1: Create A Telegram Bot

On your phone or desktop Telegram:

1. Search for `@BotFather`.
2. Send:

```text
/newbot
```

3. Follow the prompts.
4. BotFather returns a token like:

```text
123456789:ABCDEF...
```

Keep this token secret.

Optional: configure Telegram's command menu through BotFather:

```text
/setcommands
```

Then paste:

```text
id - show your chat_id
codex - run Codex with a task
new - start a fresh Codex session
session - show bound Codex session id
sessions - list recent Codex sessions
resume - bind to a Codex session id
history - show recent messages in the bound session
menu - show quick command keyboard
status - show relay status
cancel - terminate current Codex subprocess
help - show help
```

This BotFather command menu is optional. The relay also provides a reply keyboard through `/menu`.

## Step 2: Clone And Configure

On your server:

```bash
git clone git@github.com:luojq19/codex-telegram-bot.git
cd codex-telegram-bot
cp .env.example .env
chmod 600 .env
```

Edit `.env`:

```bash
nano .env
```

Minimum configuration:

```bash
TELEGRAM_BOT_TOKEN=123456789:replace_me
TELEGRAM_ALLOWED_CHAT_IDS=
CODEX_BIN=codex
CODEX_WORKDIR=/absolute/path/to/your/project
CODEX_SANDBOX=workspace-write
CODEX_RELAY_TIMEOUT_SEC=3600
```

If `codex` is installed through `nvm`, cron, tmux, or non-login shells may not see it. In that case either set `CODEX_BIN` to an absolute path:

```bash
CODEX_BIN=/home/you/.nvm/versions/node/v24.15.0/bin/codex
```

or set:

```bash
CODEX_EXTRA_PATH=/home/you/.nvm/versions/node/v24.15.0/bin
```

## Step 3: Get Your Telegram `chat_id`

Start the relay temporarily:

```bash
./scripts/start_telegram_codex_relay.sh
```

Open your bot in Telegram and send:

```text
/id
```

The bot replies with something like:

```text
chat_id: 123456789
```

Stop the relay with `Ctrl-C`, then edit `.env`:

```bash
TELEGRAM_ALLOWED_CHAT_IDS=123456789
```

Multiple allowed chats are comma-separated:

```bash
TELEGRAM_ALLOWED_CHAT_IDS=123456789,987654321
```

## Step 4: Run In tmux

Recommended:

```bash
tmux new -s codex-telegram
cd /path/to/codex-telegram-bot
./scripts/start_telegram_codex_relay.sh
```

Detach while keeping it running:

```text
Ctrl-b d
```

Reattach:

```bash
tmux attach -t codex-telegram
```

Stop:

```text
Ctrl-C
```

Check from another shell:

```bash
./scripts/telegram_codex_relay_status.sh
```

## Step 5: Use The Bot

Open Telegram and send:

```text
/menu
```

This shows a reply keyboard with common no-argument commands.

Run a new task:

```text
/codex summarize this project in 5 bullet points
```

Continue the same Telegram-bound Codex session:

```text
/codex now inspect the README and suggest improvements
```

Start fresh:

```text
/new remember the number 42 and only reply "remembered"
```

Then test memory in that new session:

```text
/codex what number did I ask you to remember?
```

List sessions:

```text
/sessions
/sessions 20
/sessions all
```

Bind to an existing session:

```text
/resume 019e31d3-85b0-7281-baef-c3ce7383960c
```

View recent history:

```text
/history
/history 20
/history all
```

Cancel the current Codex subprocess:

```text
/cancel
```

## Bot Commands

```text
/id
```

Shows the current Telegram `chat_id`.

```text
/codex <task>
```

Runs Codex. If this Telegram chat has a saved Codex session, the relay calls `codex exec resume`; otherwise it calls `codex exec` and saves the new session id.

```text
/new [task]
```

Clears the Telegram-bound session. If a task is provided, starts a fresh Codex session immediately.

```text
/session
```

Shows the Codex session id bound to this Telegram chat.

```text
/sessions [all|N]
```

Lists recent Codex sessions from `~/.codex/session_index.jsonl`.

```text
/resume <session_id>
```

Binds this Telegram chat to a known Codex session id.

```text
/history [all|N]
```

Reads the local Codex session JSONL file and returns recent user/Codex messages.

```text
/menu
```

Shows a Telegram reply keyboard containing common no-argument commands.

```text
/status
```

Shows whether a Codex subprocess is running.

```text
/cancel
```

Terminates the current Codex subprocess.

```text
/help
```

Shows command help and displays the reply keyboard.

## How Session State Works

The relay stores two kinds of local state:

- Telegram update offset
- Telegram `chat_id` to Codex `session_id` binding

Default location:

```text
~/.codex_telegram_bot
```

Override it:

```bash
CODEX_RELAY_STATE_DIR=/absolute/path/to/state
```

Codex's own session data remains in your normal Codex home, usually:

```text
~/.codex/session_index.jsonl
~/.codex/sessions/
```

## Troubleshooting

If the bot does not reply:

1. Check that the relay is running in tmux.
2. Check that `.env` has the correct `TELEGRAM_BOT_TOKEN`.
3. Send `/id` and verify the returned id is in `TELEGRAM_ALLOWED_CHAT_IDS`.
4. Make sure only one relay process is polling the same bot token.
5. If `codex` is not found, set `CODEX_BIN` to an absolute path or set `CODEX_EXTRA_PATH`.

If Codex fails with trust or git directory checks:

- Ensure `CODEX_WORKDIR` points to the project you want.
- The relay passes `--skip-git-repo-check` for convenience.
- Configure Codex itself as needed in `~/.codex/config.toml`.

If commands run but produce no answer:

- Check the tmux output.
- Increase `CODEX_RELAY_TIMEOUT_SEC`.
- Run the equivalent `codex exec` command manually on the server.

## Development

Syntax check:

```bash
python3 -m py_compile src/telegram_codex_relay.py
```

Run locally:

```bash
./scripts/start_telegram_codex_relay.sh
```

The script intentionally uses Python standard library only.
