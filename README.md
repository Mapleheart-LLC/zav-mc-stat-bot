# Minecraft Status Discord Bot

A Discord bot that maintains Minecraft server status dashboards per Discord server. Each Discord server can register multiple Minecraft servers, choose a combined or per-server dashboard, and inspect collected uptime and player history.

## Setup

1. Create a Discord application and bot, then invite it with View Channels, Send Messages, Embed Links, Read Message History, and Manage Messages permissions in the channels it will manage.
2. Enable the **Message Content Intent** in the Discord Developer Portal. Prefix commands require it.
3. Create a `.env` file alongside `compose.yaml` containing:

   ```env
   DISCORD_TOKEN=your_bot_token
   COMMAND_PREFIX=!
   LOG_LEVEL=INFO
   MOTHERS_LEDGER_SECRET=replace_with_openssl_rand_hex_32_output
   QUEST_API_PORT=8765
   ```

   `TOGGLE_ROLE_ID` is optional. Members with Manage Server or Administrator can manage the bot; the configured role is an additional operator role.
4. Start the bot with `docker compose up -d --build`.
5. In each Discord server, configure a dashboard channel and add servers:

   ```text
   !server channel #minecraft-status
   !server add cobble play.example.org
   !server add creative play.example.org:25566
   ```

The bot stores its SQLite database in the persistent `bot_data` Docker volume. Keep that volume when rebuilding/updating the image.

## Mothers Ledger integration

The optional API listens inside the bot container on port `8765`; Compose publishes it to host loopback only. For a remote Minecraft server, terminate HTTPS at a reverse proxy and forward `POST /api/v1/events` to `http://127.0.0.1:8765/api/v1/events`. Generate a secret with `openssl rand -hex 32`, set the same value as `MOTHERS_LEDGER_SECRET` in the bot and Minecraft server environments, and never commit it.

In Discord, an admin binds the server's stable source ID and configures the completion channel:

```text
!quest bind cobble-server #quest-completions
!quest announce cobble-server on
!quest sources
```

Set `MOTHERS_LEDGER_ENDPOINT` on the Minecraft server to the full HTTPS route (for example, `https://quests.example.org/api/v1/events`) and set `MOTHERS_LEDGER_SOURCE_ID` to the same source ID. The mod signs `POST`, the route, timestamp, nonce, and exact JSON body with HMAC-SHA256; the bot rejects stale timestamps, reused nonces, unbound sources, and duplicate event IDs. Announcements are names-only and never ping players.

## Commands

- `!servers` lists configured servers and dashboard settings.
- `!status [alias]` shows the current status for a server. The alias may be omitted when only one server is configured.
- `!stats <alias> [1|7|30]` shows uptime and average players for the selected window. The default window is seven days.
- `!refresh` requests an immediate status refresh.
- `!server add <alias> <address>` and `!server remove <alias>` manage server entries.
- `!server channel #channel` selects the dashboard destination.
- `!server layout combined|per_server` selects one paginated combined dashboard or one message per server.
- `!server history on|off` enables or removes the optional history message for this Discord server.
- `!server interval <minutes>` sets polling from one to 60 minutes; the default is five minutes.
- `!ip status|on|off` reports or changes whether addresses appear in public status output.
- `!help` lists commands.

Configuration changes, address visibility, and manual refresh require Manage Server, Administrator, or the optional `TOGGLE_ROLE_ID` role. Status and history are available to all members. Server settings and history are isolated by Discord server.

The history message is off by default. Turning it off removes only the published message; collected samples and the `!stats` command remain available. The main status message always shows the observed online streak and a rolling 24-hour uptime estimate. The streak starts at the last confirmed offline check; provider/network failures neither count as offline nor reset it. Uptime includes successful-check coverage and is labeled inconclusive below 80% coverage. The dashboard also shows how long monitoring has been collecting data, so a partial day is not mistaken for a full 24-hour history.

## Existing installations

The first startup can import the old single-server configuration from `MINECRAFT_IP`, `CHANNEL_ID`, `data/message_id.txt`, and `data/settings.json`. Keep the existing `bot_data` volume and set the old environment values for the first upgraded startup. The configured channel must still exist and be accessible to the bot so it can identify the owning Discord server. After migration, SQLite is authoritative; the old values are not re-imported.

Historical data begins when SQLite status sampling starts; old installations do not have historical samples to import. The bot retains 30 days of samples and prunes older records daily. Network/provider failures are recorded as unavailable checks and are excluded from uptime calculations.

## Backup and tests

Back up the `bot_data` volume while the bot is stopped, or use SQLite's online backup facilities while it is running. Restoring the database restores guild configuration, dashboard message references, and status history.

Run the local tests with:

```text
python -m unittest discover -s tests -v
```

Build the deployment image with `docker compose build`.