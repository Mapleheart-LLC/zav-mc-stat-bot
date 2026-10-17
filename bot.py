import asyncio
import discord
from discord.ext import tasks, commands
import aiohttp
import os
import logging
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from dashboard import build_dashboard, build_server_embed
from status import InvalidStatusResponse, fetch_status
from storage import Store

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger('mc-status-bot')

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN', '').strip()
MINECRAFT_IP = os.environ.get('MINECRAFT_IP', '').strip()
COMMAND_PREFIX = os.environ.get('COMMAND_PREFIX', '!').strip() or '!'

raw_toggle_role_id = os.environ.get('TOGGLE_ROLE_ID', '').strip()
try:
    TOGGLE_ROLE_ID = int(raw_toggle_role_id) if raw_toggle_role_id else 0
except ValueError:
    TOGGLE_ROLE_ID = 0

raw_channel_id = os.environ.get('CHANNEL_ID', '').strip()
try:
    CHANNEL_ID = int(raw_channel_id)
except ValueError:
    CHANNEL_ID = 0

MESSAGE_ID_FILE = Path('data/message_id.txt')
SETTINGS_FILE = Path('data/settings.json')
DATABASE_PATH = Path(os.environ.get('DATABASE_PATH', 'data/bot.sqlite3'))
store = Store(DATABASE_PATH)
http_session: Optional[aiohttp.ClientSession] = None
last_polled: dict[int, float] = {}
last_retention: float = 0

intents = getattr(discord, 'Intents').default()
intents.message_content = True


class StatusBot(commands.Bot):
    async def close(self) -> None:
        if http_session is not None and not http_session.closed:
            await http_session.close()
        await super().close()


bot = StatusBot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)


def load_message_id() -> Optional[int]:
    if not MESSAGE_ID_FILE.exists():
        return None
    content = MESSAGE_ID_FILE.read_text(encoding='utf-8').strip()
    return int(content) if content.isdigit() else None


def load_settings() -> dict[str, bool]:
    default_settings = {'show_ip': False}
    if not SETTINGS_FILE.exists():
        return default_settings
    try:
        content = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
        if isinstance(content, dict):
            show_ip = bool(content.get('show_ip', False))
            return {'show_ip': show_ip}
    except (json.JSONDecodeError, OSError):
        logger.warning('Invalid settings file found. Falling back to defaults.')
    return default_settings


async def get_text_channel(channel_id: int) -> Optional[discord.TextChannel]:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.exception('Could not resolve dashboard channel %s', channel_id)
            return None
    if not isinstance(channel, discord.TextChannel):
        logger.error('Dashboard destination %s is not a text channel', channel_id)
        return None
    return channel


async def publish_guild_dashboard(guild_id: int) -> bool:
    settings = await store.get_guild_settings(guild_id)
    if settings.dashboard_channel_id is None:
        return False
    channel = await get_text_channel(settings.dashboard_channel_id)
    if channel is None or channel.guild.id != guild_id:
        return False

    servers = await store.list_servers(guild_id)
    samples = {server.id: await store.get_latest_status(server.id) for server in servers}
    uptimes = {
        server.id: await store.get_uptime_summary(
            server.id,
            interval_minutes=settings.poll_interval_minutes,
        )
        for server in servers
    }
    embeds = build_dashboard(servers, samples, settings, uptimes)
    previous_messages = await store.list_messages(guild_id)
    for message_key, embed in embeds.items():
        previous = await store.get_message(guild_id, message_key)
        message = None
        if previous is not None and previous[0] == channel.id:
            try:
                message = await channel.fetch_message(previous[1])
                await message.edit(embed=embed)
            except discord.NotFound:
                message = None
            except (discord.Forbidden, discord.HTTPException):
                logger.exception('Could not update dashboard message %s', previous[1])
                return False
        if message is None:
            try:
                message = await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                logger.exception('Could not send dashboard message to %s', channel.id)
                return False
        await store.set_message(guild_id, message_key, channel.id, message.id)

    for message_key, old_channel_id, message_id in previous_messages:
        if message_key in embeds and old_channel_id == channel.id:
            continue
        old_channel = await get_text_channel(old_channel_id)
        if old_channel is not None and old_channel.guild.id == guild_id:
            try:
                old_message = await old_channel.fetch_message(message_id)
                await old_message.delete()
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                logger.warning('Could not remove stale dashboard message %s', message_id)
        await store.delete_message(guild_id, message_key)
    return True


async def delete_tracked_message(guild_id: int, message_key: str) -> None:
    tracked = await store.get_message(guild_id, message_key)
    if tracked is None:
        return
    channel = await get_text_channel(tracked[0])
    if channel is not None and channel.guild.id == guild_id:
        try:
            message = await channel.fetch_message(tracked[1])
            await message.delete()
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException):
            logger.warning('Could not delete tracked message %s', tracked[1])
    await store.delete_message(guild_id, message_key)


async def refresh_guild(guild_id: int) -> bool:
    global last_retention
    last_polled[guild_id] = time.monotonic()
    servers = await store.list_servers(guild_id)
    if http_session is None:
        return False

    for server in servers:
        checked_at = datetime.now(timezone.utc)
        try:
            status = await fetch_status(http_session, server.address)
        except (aiohttp.ClientError, asyncio.TimeoutError, InvalidStatusResponse) as error:
            logger.warning('Status request failed for %s: %s', server.alias, error)
            await store.record_status(
                server.id, checked_at=checked_at, online=None, error=type(error).__name__
            )
        except Exception:
            logger.exception('Unexpected status failure for %s', server.alias)
            await store.record_status(
                server.id, checked_at=checked_at, online=None, error='Unexpected error'
            )
        else:
            await store.record_status(
                server.id,
                checked_at=checked_at,
                online=status.online,
                players_online=status.players_online,
                players_max=status.players_max,
                version=status.version,
                motd=status.motd,
            )

    if time.monotonic() - last_retention >= 24 * 60 * 60:
        await store.prune_status_samples(before=datetime.now(timezone.utc) - timedelta(days=30))
        last_retention = time.monotonic()
    return await publish_guild_dashboard(guild_id)

async def migrate_legacy_installation() -> None:
    address = MINECRAFT_IP
    if not address or CHANNEL_ID <= 0:
        return
    try:
        channel = await bot.fetch_channel(CHANNEL_ID)
    except discord.DiscordException:
        logger.exception('Could not resolve legacy status channel %s', CHANNEL_ID)
        return
    if not isinstance(channel, discord.TextChannel):
        logger.error('Legacy channel %s is not a text channel', CHANNEL_ID)
        return
    imported = await store.import_legacy(
        guild_id=channel.guild.id,
        channel_id=channel.id,
        address=address,
        message_id=load_message_id(),
        show_ip=load_settings().get('show_ip', False),
    )
    if imported:
        logger.info('Imported legacy single-server configuration for guild %s', channel.guild.id)


@bot.event
async def on_ready() -> None:
    global http_session
    logger.info('Logged in as %s | prefix=%s', bot.user, COMMAND_PREFIX)
    await store.initialize()
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    await migrate_legacy_installation()
    for guild in bot.guilds:
        try:
            await refresh_guild(guild.id)
        except Exception:
            logger.exception('Initial status update failed for guild %s', guild.id)
    if not update_dashboards.is_running():
        update_dashboards.start()


@tasks.loop(minutes=1)
async def update_dashboards() -> None:
    for guild in bot.guilds:
        try:
            settings = await store.get_guild_settings(guild.id)
            if time.monotonic() - last_polled.get(guild.id, 0) >= settings.poll_interval_minutes * 60:
                await refresh_guild(guild.id)
        except Exception:
            logger.exception('Scheduled status update failed for guild %s', guild.id)


@update_dashboards.before_loop
async def before_dashboard_updates() -> None:
    await bot.wait_until_ready()


def can_manage_guild(ctx: commands.Context[Any]) -> bool:
    if not isinstance(ctx.author, discord.Member):
        return False
    permissions = ctx.author.guild_permissions
    if permissions.administrator or permissions.manage_guild:
        return True
    return TOGGLE_ROLE_ID > 0 and any(role.id == TOGGLE_ROLE_ID for role in ctx.author.roles)


@bot.command(name='help')
async def help_command(ctx: commands.Context[Any]) -> None:
    prefix = COMMAND_PREFIX
    await ctx.reply(
        f'Commands: `{prefix}servers`, `{prefix}status [alias]`, '
        f'`{prefix}stats <alias> [1|7|30]`, `{prefix}refresh`, '
        f'`{prefix}server add|remove|channel|layout|interval|history`, '
        f'`{prefix}ip status|on|off`',
        mention_author=False,
    )


@bot.command(name='servers')
@commands.guild_only()
async def servers_command(ctx: commands.Context[Any]) -> None:
    guild_id = ctx.guild.id
    servers = await store.list_servers(guild_id)
    settings = await store.get_guild_settings(guild_id)
    if not servers:
        await ctx.reply(
            f'No servers configured. Add one with `{COMMAND_PREFIX}server add <alias> <address>`.',
            mention_author=False,
        )
        return
    lines = [
        f'**{server.alias}**: `{server.address}`' if settings.show_ip else f'**{server.alias}**'
        for server in servers
    ]
    channel = f'<#{settings.dashboard_channel_id}>' if settings.dashboard_channel_id else 'not set'
    await ctx.reply(
        '\n'.join(lines)
        + f'\nDashboard: {channel} | Layout: `{settings.layout}`'
        + f' | Poll: `{settings.poll_interval_minutes}m`',
        mention_author=False,
    )


@bot.command(name='status')
@commands.guild_only()
async def status_command(ctx: commands.Context[Any], alias: Optional[str] = None) -> None:
    guild_id = ctx.guild.id
    servers = await store.list_servers(guild_id)
    if alias is None:
        if len(servers) != 1:
            await ctx.reply(f'Specify a server alias with `{COMMAND_PREFIX}status <alias>`.', mention_author=False)
            return
        server = servers[0]
    else:
        server = await store.get_server(guild_id, alias)
    if server is None:
        await ctx.reply('That server is not configured. Use the servers command to list entries.', mention_author=False)
        return
    settings = await store.get_guild_settings(guild_id)
    sample = await store.get_latest_status(server.id)
    uptime = await store.get_uptime_summary(
        server.id,
        interval_minutes=settings.poll_interval_minutes,
    )
    await ctx.reply(
        embed=build_server_embed(
            server,
            sample,
            show_ip=settings.show_ip,
            uptime=uptime,
        ),
        mention_author=False,
    )


@bot.command(name='stats')
@commands.guild_only()
async def stats_command(ctx: commands.Context[Any], alias: str, days: int = 7) -> None:
    if days not in {1, 7, 30}:
        await ctx.reply('Choose a history window of `1`, `7`, or `30` days.', mention_author=False)
        return
    server = await store.get_server(ctx.guild.id, alias)
    if server is None:
        await ctx.reply('That server is not configured.', mention_author=False)
        return
    settings = await store.get_guild_settings(ctx.guild.id)
    summary = await store.get_status_summary(server.id, days=days)
    uptime = await store.get_uptime_summary(
        server.id,
        interval_minutes=settings.poll_interval_minutes,
        hours=days * 24,
    )
    if summary.uptime_percent is None or uptime.coverage_percent is None:
        await ctx.reply(f'No successful history is available for **{server.alias}** yet.', mention_author=False)
        return
    average = 'unknown' if summary.average_players is None else f'{summary.average_players:.1f}'
    tracked_for = 'unknown'
    if uptime.monitoring_since is not None:
        elapsed = max(0, int((datetime.now(timezone.utc) - uptime.monitoring_since).total_seconds() // 3600))
        tracked_for = f'{elapsed}h'
    uptime_text = (
        f'Inconclusive ({uptime.coverage_percent:.0f}% coverage)'
        if uptime.coverage_percent < 80
        else f'{uptime.uptime_percent:.2f}% ({uptime.coverage_percent:.0f}% coverage)'
    )
    await ctx.reply(
        f'**{server.alias} · last {days} days**\n'
        f'Uptime: **{uptime_text}** · tracking {tracked_for}\n'
        f'Confirmed checks: {uptime.online_count}/{uptime.successful_count} online\n'
        f'Average players: **{average}**',
        mention_author=False,
    )


@bot.command(name='refresh')
@commands.guild_only()
@commands.check(can_manage_guild)
async def refresh_command(ctx: commands.Context[Any]) -> None:
    if await refresh_guild(ctx.guild.id):
        await ctx.reply('Status dashboard refreshed.', mention_author=False)
    else:
        await ctx.reply(
            f'Set a dashboard channel with `{COMMAND_PREFIX}server channel #channel` first.',
            mention_author=False,
        )


@bot.group(name='server', invoke_without_command=True)
@commands.guild_only()
async def server_group(ctx: commands.Context[Any]) -> None:
    await ctx.reply(
        f'Use `{COMMAND_PREFIX}server add|remove|channel|layout|interval|history`. ',
        mention_author=False,
    )


@server_group.command(name='add')
@commands.check(can_manage_guild)
async def server_add(ctx: commands.Context[Any], alias: str, *, address: str) -> None:
    alias = alias.lower()
    address = address.strip()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', alias):
        await ctx.reply('Aliases must be 1-32 letters, numbers, `_` or `-`.', mention_author=False)
        return
    if (
        not address or len(address) > 255 or any(char.isspace() for char in address)
        or '://' in address or '/' in address
    ):
        await ctx.reply('Provide a hostname or IP address, optionally with a port.', mention_author=False)
        return
    if await store.get_server(ctx.guild.id, alias) is not None:
        await ctx.reply('That alias is already in use in this Discord server.', mention_author=False)
        return
    await store.add_server(ctx.guild.id, alias, address)
    await refresh_guild(ctx.guild.id)
    await ctx.reply(f'Added Minecraft server **{alias}**.', mention_author=False)


@server_group.command(name='remove')
@commands.check(can_manage_guild)
async def server_remove(ctx: commands.Context[Any], alias: str) -> None:
    if not await store.remove_server(ctx.guild.id, alias):
        await ctx.reply('That server is not configured.', mention_author=False)
        return
    await publish_guild_dashboard(ctx.guild.id)
    await ctx.reply(f'Removed Minecraft server **{alias}**.', mention_author=False)


@server_group.command(name='channel')
@commands.check(can_manage_guild)
async def server_channel(ctx: commands.Context[Any], channel: discord.TextChannel) -> None:
    await store.configure_guild(ctx.guild.id, dashboard_channel_id=channel.id)
    await publish_guild_dashboard(ctx.guild.id)
    await ctx.reply(f'Dashboard channel set to {channel.mention}.', mention_author=False)


@server_group.command(name='layout')
@commands.check(can_manage_guild)
async def server_layout(ctx: commands.Context[Any], layout: str) -> None:
    layout = layout.lower()
    if layout not in {'combined', 'per_server'}:
        await ctx.reply('Choose `combined` or `per_server`.', mention_author=False)
        return
    await store.configure_guild(ctx.guild.id, layout=layout)
    await publish_guild_dashboard(ctx.guild.id)
    await ctx.reply(f'Dashboard layout set to `{layout}`.', mention_author=False)


@server_group.command(name='interval')
@commands.check(can_manage_guild)
async def server_interval(ctx: commands.Context[Any], minutes: int) -> None:
    if not 1 <= minutes <= 60:
        await ctx.reply('Polling interval must be between 1 and 60 minutes.', mention_author=False)
        return
    await store.configure_guild(ctx.guild.id, poll_interval_minutes=minutes)
    await ctx.reply(f'Polling interval set to **{minutes} minutes**.', mention_author=False)


@server_group.command(name='history')
@commands.check(can_manage_guild)
async def server_history(ctx: commands.Context[Any], mode: str) -> None:
    mode = mode.lower()
    if mode not in {'on', 'off'}:
        await ctx.reply('Choose `on` or `off`.', mention_author=False)
        return

    guild_id = ctx.guild.id
    if mode == 'on':
        settings = await store.get_guild_settings(guild_id)
        if settings.dashboard_channel_id is None:
            await ctx.reply(
                f'Set a dashboard channel first with `{COMMAND_PREFIX}server channel #channel`.',
                mention_author=False,
            )
            return
        await store.configure_guild(guild_id, history_enabled=True)
        if not await publish_guild_dashboard(guild_id):
            await ctx.reply(
                'History is enabled, but I could not publish the dashboard. Check my channel permissions.',
                mention_author=False,
            )
            return
        await ctx.reply('The history message is now enabled.', mention_author=False)
        return

    await store.configure_guild(guild_id, history_enabled=False)
    await delete_tracked_message(guild_id, 'history')
    await publish_guild_dashboard(guild_id)
    await ctx.reply('The history message is now disabled; collected data is kept.', mention_author=False)


@bot.command(name='ip')
@commands.guild_only()
async def ip_command(ctx: commands.Context[Any], mode: Optional[str] = None) -> None:
    settings = await store.get_guild_settings(ctx.guild.id)
    if mode is None or mode.lower() == 'status':
        visibility = 'visible' if settings.show_ip else 'hidden'
        await ctx.reply(f'Server addresses are currently **{visibility}**.', mention_author=False)
        return
    mode = mode.lower()
    if mode not in {'on', 'off'}:
        await ctx.reply(f'Usage: `{COMMAND_PREFIX}ip on|off|status`', mention_author=False)
        return
    if not can_manage_guild(ctx):
        await ctx.reply('You do not have permission to change address visibility.', mention_author=False)
        return
    show_ip = mode == 'on'
    await store.configure_guild(ctx.guild.id, show_ip=show_ip)
    await publish_guild_dashboard(ctx.guild.id)
    visibility = 'visible' if show_ip else 'hidden'
    await ctx.reply(f'Server addresses are now **{visibility}**.', mention_author=False)


@bot.event
async def on_command_error(ctx: commands.Context[Any], error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.reply('This command is unavailable here or you do not have permission.', mention_author=False)
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f'Missing argument: `{error.param.name}`.', mention_author=False)
        return
    if isinstance(error, commands.BadArgument):
        await ctx.reply(f'Check the command syntax with `{COMMAND_PREFIX}help`.', mention_author=False)
        return
    logger.error(
        'Unhandled command error',
        exc_info=(type(error), error, error.__traceback__),
    )
    await ctx.reply('The command failed. Check the bot logs for details.', mention_author=False)


@bot.event
async def on_disconnect():
    logger.warning('Discord client disconnected')


@bot.event
async def on_resumed():
    logger.info('Discord client resumed connection')


@bot.event
async def on_error(event_method: str, *args, **kwargs):
    logger.exception('Unhandled discord.py error in event: %s', event_method)

if __name__ == '__main__':
    if not DISCORD_TOKEN:
        raise RuntimeError('Missing required environment variable: DISCORD_TOKEN')
    bot.run(DISCORD_TOKEN)