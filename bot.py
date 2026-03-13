import discord
from discord.ext import tasks, commands
import aiohttp
import os
import logging
import json
import sys
from pathlib import Path
from typing import Any, Optional, cast

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

if not DISCORD_TOKEN:
    raise RuntimeError('Missing required environment variable: DISCORD_TOKEN')
if not MINECRAFT_IP:
    raise RuntimeError('Missing required environment variable: MINECRAFT_IP')
if CHANNEL_ID <= 0:
    raise RuntimeError('Missing or invalid environment variable: CHANNEL_ID')

MESSAGE_ID_FILE = Path('data/message_id.txt')
SETTINGS_FILE = Path('data/settings.json')

intents = getattr(discord, 'Intents').default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)


async def resolve_text_channel() -> Optional[discord.TextChannel]:
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
        except discord.NotFound:
            logger.error('Channel with id %s was not found', CHANNEL_ID)
            return None
        except discord.Forbidden:
            logger.error('Missing permission to fetch channel %s', CHANNEL_ID)
            return None
        except discord.HTTPException:
            logger.exception('HTTP error while fetching channel %s', CHANNEL_ID)
            return None

    if not isinstance(channel, discord.TextChannel):
        logger.error('Channel %s is not a text channel', CHANNEL_ID)
        return None
    return channel


def load_message_id() -> Optional[int]:
    if not MESSAGE_ID_FILE.exists():
        return None
    content = MESSAGE_ID_FILE.read_text(encoding='utf-8').strip()
    return int(content) if content.isdigit() else None


def save_message_id(message_id: int) -> None:
    MESSAGE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    MESSAGE_ID_FILE.write_text(str(message_id), encoding='utf-8')


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


def save_settings(settings: dict[str, bool]) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding='utf-8')


def can_toggle_ip(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    if TOGGLE_ROLE_ID <= 0:
        return False
    return any(role.id == TOGGLE_ROLE_ID for role in member.roles)


def build_status_embed(response: dict[str, Any], show_ip: bool) -> discord.Embed:
    online = response.get('online', False)
    description = f"**IP:** `{MINECRAFT_IP}`" if show_ip else '**IP:** Hidden'

    embed = discord.Embed(
        title='🌿 Cobblemon Server Status',
        description=description,
        color=discord.Color.brand_green() if online else discord.Color.brand_red()
    )

    if online:
        players = response.get('players', {}).get('online', 0)
        max_players = response.get('players', {}).get('max', 0)
        version = response.get('version', 'Unknown')
        motd_list = response.get('motd', {}).get('clean', [])
        motd = motd_list[0] if motd_list else ''

        embed.add_field(name='Status', value='🟢 Online', inline=True)
        embed.add_field(name='Players', value=f'{players}/{max_players}', inline=True)
        embed.add_field(name='Version', value=version, inline=True)

        if motd:
            embed.add_field(name='MOTD', value=f'*{motd}*', inline=False)

        embed.set_thumbnail(url=f'https://api.mcsrvstat.us/icon/{MINECRAFT_IP}')
    else:
        embed.add_field(name='Status', value='🔴 Offline', inline=True)
        embed.add_field(name='Players', value='0/0', inline=True)

    embed.set_footer(text='Updated automatically')
    embed.timestamp = discord.utils.utcnow()
    return embed


async def delete_old_status_embeds(channel: discord.TextChannel, keep_message_id: Optional[int] = None) -> None:
    bot_user = bot.user
    if bot_user is None:
        return
    try:
        async for old_message in channel.history(limit=50):
            if keep_message_id and old_message.id == keep_message_id:
                continue
            if old_message.author.id != bot_user.id:
                continue
            if not old_message.embeds:
                continue

            first_embed = old_message.embeds[0]
            if first_embed.title == '🌿 Cobblemon Server Status':
                try:
                    await old_message.delete()
                    logger.info('Deleted old status embed message %s', old_message.id)
                except discord.Forbidden:
                    logger.warning('Missing permissions to delete old status message %s', old_message.id)
                except discord.HTTPException:
                    logger.warning('Failed deleting old status message %s due to HTTP error', old_message.id)
    except discord.Forbidden:
        logger.warning('Missing permissions to read channel history for cleanup')
    except discord.HTTPException:
        logger.warning('Failed to read channel history for cleanup due to HTTP error')


async def publish_status_embed() -> bool:
    logger.info('Publishing status embed (ip_visible=%s)', load_settings().get('show_ip', False))
    url = f'https://api.mcsrvstat.us/3/{MINECRAFT_IP}'
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.error('Server status request failed: HTTP %s', resp.status)
                return False
            response = await resp.json(content_type=None)

    channel = await resolve_text_channel()
    if not channel:
        return False

    settings = load_settings()
    embed = build_status_embed(response, show_ip=settings.get('show_ip', False))
    message_id = load_message_id()

    if message_id:
        try:
            msg = await channel.fetch_message(message_id)
            await cast(Any, msg).edit(embed=embed)
            logger.info('Updated status embed message %s', message_id)
            return True
        except discord.NotFound:
            logger.warning('Tracked message %s was not found. Sending a new embed.', message_id)
        except discord.Forbidden:
            logger.exception('Missing permissions to edit message %s', message_id)
            return False
        except discord.HTTPException:
            logger.exception('Failed to edit message %s due to Discord HTTP error', message_id)
            return False

    await delete_old_status_embeds(channel)
    msg = await cast(Any, channel).send(embed=embed)
    save_message_id(msg.id)
    logger.info('Sent new status embed message %s', msg.id)
    return True

@bot.event
async def on_ready():
    logger.info('Logged in as %s | prefix=%s | channel_id=%s | role_id=%s', bot.user, COMMAND_PREFIX, CHANNEL_ID, TOGGLE_ROLE_ID)
    try:
        await publish_status_embed()
    except Exception:
        logger.exception('Initial status publish failed during on_ready')
    if not update_embed.is_running():
        update_embed.start()

@tasks.loop(minutes=5)
async def update_embed():
    try:
        logger.info('Running scheduled status update')
        await publish_status_embed()

    except aiohttp.ClientError:
        logger.exception('Network error while requesting server status')
    except Exception:
        logger.exception('Unexpected error in update loop')


@update_embed.before_loop
async def before_update_embed() -> None:
    await bot.wait_until_ready()


@bot.command(name='help')
async def help_command(ctx: commands.Context[Any]) -> None:
    await ctx.reply(
        f'Commands: `{COMMAND_PREFIX}help`, `{COMMAND_PREFIX}refresh`, `{COMMAND_PREFIX}ip status`, `{COMMAND_PREFIX}ip on`, `{COMMAND_PREFIX}ip off`',
        mention_author=False
    )


@bot.command(name='refresh')
async def refresh_command(ctx: commands.Context[Any]) -> None:
    if not can_toggle_ip(ctx.author):
        await ctx.reply('You do not have permission to refresh the status message.', mention_author=False)
        return
    updated = await publish_status_embed()
    if updated:
        await ctx.reply('Status embed refreshed.', mention_author=False)
    else:
        await ctx.reply('Could not refresh the status embed. Check logs.', mention_author=False)


@bot.command(name='ip')
async def ip_command(ctx: commands.Context[Any], mode: Optional[str] = None) -> None:
    settings = load_settings()

    if mode is None or mode.lower() == 'status':
        current = 'visible' if settings.get('show_ip', False) else 'hidden'
        await ctx.reply(f'IP visibility is currently **{current}**.', mention_author=False)
        return

    normalized = mode.lower()
    if normalized not in {'on', 'off'}:
        await ctx.reply(f'Usage: `{COMMAND_PREFIX}ip on|off|status`', mention_author=False)
        return

    if not can_toggle_ip(ctx.author):
        await ctx.reply('You do not have permission to change IP visibility.', mention_author=False)
        return

    show_ip = normalized == 'on'
    settings['show_ip'] = show_ip
    save_settings(settings)
    await publish_status_embed()
    await ctx.reply(f'IP visibility set to **{"visible" if show_ip else "hidden"}**.', mention_author=False)


@bot.event
async def on_disconnect():
    logger.warning('Discord client disconnected')


@bot.event
async def on_resumed():
    logger.info('Discord client resumed connection')


@bot.event
async def on_error(event_method: str, *args, **kwargs):
    logger.exception('Unhandled discord.py error in event: %s', event_method)

bot.run(DISCORD_TOKEN)