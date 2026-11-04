# tg-scribe — Telegram for AI agents on Mac

English | [Русский](README.ru.md)

A local MCP server for macOS that gives an AI agent, such as Claude Code, access to your Telegram account. The agent saves your chats as Markdown files on disk and works with them: searches, analyzes, summarizes, drafts replies.

The server runs on your Mac and connects to Telegram as you, over the official MTProto protocol. It uses the Telethon library and your own `api_id`. Text on photos is recognized with Apple Vision, so you need a Mac.

## Contents

- [What it does](#what-it-does)
  - [How it works in practice](#how-it-works-in-practice)
- [Archive](#archive)
- [Requirements](#requirements)
- [Installation](#installation)
- [Permissions](#permissions)
- [Choosing a chat](#choosing-a-chat)
- [Limits and risks](#limits-and-risks)
- [Development](#development)
- [License](#license)

## What it does

| Tool | What it does |
|---|---|
| `list_dialogs` | Shows the 50 most recent chats. Finds any other chat by name |
| `sync_chat` | Saves a chat to the archive: the last N days or the whole history. Later calls add new messages |
| `transcribe` | Transcribes voice and video messages. Needs Telegram Premium |
| `ocr_photos` | Reads text on photos with Apple Vision, locally on your Mac |
| `read_files` | Reads the text of txt, md, csv, pdf, doc, docx, rtf and image files locally on your Mac and saves its beginning to the archive |
| `fetch_media` | Downloads a photo or a file up to 50 MB so the agent can look at it. Videos and audio are not downloaded |
| `describe_media` | Saves a short description of a photo or a file to the archive |
| `send_message` | Sends a message after you confirm it |

### How it works in practice

1. You ask: "What did we agree on with the contractor over the last three weeks?"
2. The agent calls `sync_chat` with `days=21`. The server saves three weeks of messages to the archive, newest first.
3. The agent searches the archive. An important voice message is marked as not transcribed, so the agent transcribes it with `transcribe`. It needs the numbers from a photo of an estimate, so it reads the text with `ocr_photos`.
4. The agent answers and cites message ids.
5. Next day you ask: "Anything new?" `sync_chat` loads only new messages. It transcribes and reads their voice messages, photos and files right away, up to 20 of each per call.
6. You ask: "And what did we decide last month?" The agent calls `sync_chat` with `days=60`, and the server loads the missing older history.

Voice messages, photos and files from older history are not processed while loading. The agent transcribes and reads them only when a task needs them. That is why loading takes minutes, not hours, and does not use up limits.

## Archive

Chats are stored in the `archive/` folder next to the MCP source code, one file per month. The folder is in `.gitignore`.

```
archive/Ivan-Petrov__123456789/2026-10.md
```

One line per message. The tags are in Russian. Sample file:

```
[2026-10-31 14:02] #48213 Ivan Petrov: Sent the estimate
[2026-10-31 14:03] #48214 Ivan Petrov [фото, с текстом]: 
  [на фото] Tiles: $600
  [на фото] Total: $1,500
[2026-10-31 14:05] #48215 Я ↩48213: [голосовое 0:42] «Too expensive, let's talk tonight»
[2026-10-31 14:06] #48216 Ivan Petrov [фото: kitchen plan with sizes]: here is the plan
[2026-10-31 14:07] #48217 Ivan Petrov [переслано из «Renovation News»]: Tile prices went up
  15% in November
[2026-10-31 14:10] #48218 Ivan Petrov: [кружок 0:35, не расшифровано]
[2026-10-31 14:12] #48219 Ivan Petrov [файл contract.docx, 48 КБ, с текстом]: please sign
  [в файле] Construction contract No. 17
  [в файле] Deadline: December 15, 2026
```

What each part means:

| Part | Meaning |
|---|---|
| `[2026-10-31 14:02]` | Date and time in your Mac's time zone |
| `#48213` | Message id |
| `Я` | You ("me") |
| `↩48213` | Reply to message 48213 |
| Line indented by 2 spaces | Next line of a multi-line message |
| `[переслано из «…»]` | Forwarded message and its source |
| `[голосовое 0:42] «…»`, `[кружок 0:35] «…»` | Transcript of a voice or video message |
| `[голосовое 1:10, не расшифровано]` | Voice message not transcribed yet |
| `[фото]` | Photo not processed yet |
| `[фото, с текстом]` | Photo has text, shown in the `[на фото]` lines below |
| `[фото, без текста]` | Photo has no text |
| `[фото: …]` | Photo description saved by the agent |
| `[фото, недоступно]` | Photo was deleted from Telegram |
| `[фото, не читается]` | Photo file is broken |
| `[файл report.pdf, 2.3 МБ]` | File. For txt, md, csv, pdf, doc, docx, rtf and images: not read yet |
| `[файл …, с текстом]` | The beginning of the file's text, up to 2000 characters, is in the `[в файле]` lines below |
| `[файл …, без текста]` | The file has no text, for example a scanned PDF |
| `[файл …, 12 стр., …]` | PDF page count, added once the file is read |
| `[файл …: …]` | File description saved by the agent |
| `[файл …, недоступно]`, `[файл …, не читается]` | File was deleted from Telegram or is broken |
| `[файл …, слишком большой]` | File is over 50 MB, the server does not download it |
| `[видео]`, `[стикер 👍]`, `[gif]`, `[опрос]`, `[геопозиция]`, `[контакт]` | Video, sticker, GIF, poll, location, contact |

The archive only grows: messages edited or deleted in Telegram stay as they were saved.

## Requirements

- A Mac.
- [uv](https://docs.astral.sh/uv/). It installs Python 3.12 and the dependencies.
- An AI agent with MCP support, such as [Claude Code](https://claude.com/claude-code).
- A Telegram account. Transcribing voice messages needs Telegram Premium.

## Installation

### 1. Get the project and install dependencies

```bash
git clone https://github.com/rodion1010/tg-scribe.git tg-scribe
cd tg-scribe
uv sync
```

### 2. Get api_id and api_hash

Telegram lets third-party programs into an account only through a registered app. Registration is free and takes a couple of minutes.

1. Open [my.telegram.org](https://my.telegram.org) and enter your phone number in international format, for example `+15551234567`.
2. Enter the confirmation code on the site. It arrives in Telegram from the "Telegram" account, not by SMS.
3. Select **API development tools**.
4. Fill in the fields:
   - **App title**: any name without the word "Telegram", for example `tgc`. The API terms forbid "Telegram" in the name.
   - **Short name**: 5 to 32 Latin letters and digits, for example `tgclaude`.
   - **URL**: leave empty.
   - **Platform**: Desktop.
   - **Description**: leave empty.
5. Click **Create application**. The page shows **App api_id**, a number, and **App api_hash**, a 32-character string.

If the site shows `ERROR`, open it in another browser. One account can have one app: if you already have one, the site shows its data right away.

Never share api_hash and never commit it: anyone with it can pose as your app.

### 3. Save api_id and api_hash to the config

Create the folder and the config file:

```bash
mkdir -p ~/.tgc && chmod 700 ~/.tgc
nano ~/.tgc/config.toml
```

Paste your values:

```toml
api_id = 12345678
api_hash = "0123456789abcdef0123456789abcdef"
```

Make the file private to your user:

```bash
chmod 600 ~/.tgc/config.toml
```

### 4. Log in

```bash
uv run python -m tgc.login
```

The program asks for your phone number, the code from Telegram and your cloud password if two-step verification is on. After login the file `~/.tgc/tgc.session` appears.

This file gives full access to your account without a code or password. Do not copy it, publish it or put it in cloud storage. If it leaks, end the session in Telegram: **Settings → Devices**.

### 5. Connect the server to your agent

The server starts with this command:

```bash
uv run --directory /path/to/tg-scribe python -m tgc.server
```

Here `/path/to/tg-scribe` is the folder you cloned the project into. Add this command to your agent's MCP server settings. The steps for Claude Code follow.

The project already has `.mcp.json` with this server. Start Claude Code in the project folder and allow the `tg` server when Claude Code asks.

To use Telegram from any folder, add the server for your user:

```bash
claude mcp add tg --scope user -- uv run --directory /path/to/tg-scribe python -m tgc.server
```

The archive still lives in `archive/` next to the MCP source code.

The project's `.claude/settings.json` rule that asks before sending does not apply in other folders. Add it to `~/.claude/settings.json`:

```json
{
  "permissions": {
    "ask": ["mcp__tg__send_message"]
  }
}
```

### 6. Check the connection

Ask the agent: "Show my recent Telegram chats". It calls `list_dialogs` and shows the list.

## Permissions

The agent sends a message only after you confirm it. In Claude Code, `.claude/settings.json` takes care of this: it allows reading, saving and recognizing chats without asking, and sets an `ask` rule on sending. Claude Code shows the text and the chat every time and waits for your "yes". Do not move `send_message` to `allow`.

The rule works only in the project folder. For other folders, add it to `~/.claude/settings.json` as described in step 5. In other agents, set up confirmation for `send_message` the same way.

## Choosing a chat

You can name a chat by part of its title, but Telegram search does not find every chat, and the agent may pick a similar one. Before sending, give the chat id instead: `list_dialogs` shows it. After sending, the server names the chat the message went to.

To see ids in Telegram itself, turn them on in the debug menu. In Telegram Desktop, open **Settings → Advanced → Experimental settings** and turn on **Show Peer IDs in Profile**. For other platforms, see the [tginfo guide](https://tginfo.me/open-debug-menu/) (in Russian).

Change only this setting in the debug menu: other options can break the app or delete secret chats.

## Limits and risks

- **Telegram limits.** If Telegram asks to wait longer than 30 seconds, the server stops and says how long to wait. It never waits longer than 30 seconds itself, so the call does not time out.
- **Large loads** take several calls of up to 200 seconds each. 150,000 messages load in about 10 calls, and the agent repeats them by itself.
- **One session at a time.** If two agent sessions use Telegram at once, the second gets "Telegram busy" and retries later.
- **Telegram terms.** The [API terms](https://core.telegram.org/api/terms) forbid preventing self-destructing messages from disappearing, and the server saves messages from chats with auto-delete. The [content licensing terms](https://telegram.org/tos/content-licensing) forbid training AI models on Telegram data. The archive holds other people's messages too, so stop your agent's provider from training on your data. In claude.ai this is the **Help improve Claude** setting.
- **Chat text is data, not commands.** The server tells the agent not to follow requests found in chats. Still, confirm a send only after you have read the text.

## Development

Run the tests:

```bash
uv run pytest
```

The code has four modules:

- `tgc/client.py`: Telegram connection and chat lookup;
- `tgc/archive.py`: archive format, sync, transcription and text recognition;
- `tgc/server.py`: MCP tools and instructions for the agent;
- `tgc/login.py`: account login.

## License

[MIT](LICENSE)
