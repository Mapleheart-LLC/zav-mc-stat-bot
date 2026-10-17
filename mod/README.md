# Mothers Ledger Mod

The NeoForge mod-side connector for the Mothers Ledger Discord bot. It subscribes to FTB Quests quest-completion events and sends signed, asynchronous HTTPS events to the bot. Quest/team progress remains owned by FTB Quests.

## Target profile

- Minecraft 1.21.1 / Java 21
- NeoForge 21.1.251
- Architectury API 13.0.8
- FTB Quests 2101.1.36, FTB Library 2101.1.36, FTB Teams 2101.1.9
- Cobblemon 1.7.0 or newer
- Cobblemon Quests 1.2.0 or newer

This project does not reimplement Cobblemon quest tasks. It listens to the FTB Quests completion event, so quests using Cobblemon Quests tasks are forwarded automatically when their parent FTB quest completes. Cobblemon Quests remains a separate required mod; its implementation is not copied into this project.

## Build

Build the NeoForge jar with Docker (which supplies Java 21 and Gradle):

```powershell
docker build --output type=local,dest=dist .
```

The NeoForge jar is written to `dist/artifacts/mothers-ledger-neoforge.jar`. A local build can instead run `gradle build` with Java 21.

## Bot connection

Configure these environment variables on the Minecraft server process:

- `MOTHERS_LEDGER_ENDPOINT`: the full HTTPS URL for the bot's `POST /api/v1/events` endpoint.
- `MOTHERS_LEDGER_SECRET`: the shared HMAC-SHA256 secret.
- `MOTHERS_LEDGER_SOURCE_ID`: stable identifier assigned to this Minecraft server.

The sender signs `POST + newline + /api/v1/events + newline + timestamp + newline + nonce + newline + exact JSON body` and retries transient failures in the background. The bot receiver starts when `MOTHERS_LEDGER_SECRET` is configured. Bind the source in Discord with `!quest bind <source_id> #channel` before expecting events to be accepted.

This integration targets the existing [Cobblemon Quests](https://github.com/WinterWolfSV/Cobblemon_Quests) mod. It does not reimplement or copy its custom task code; completed parent FTB quests are sent through FTB Quests' completion event.