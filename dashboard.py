from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import discord

from storage import GuildSettings, Server, StatusSample, UptimeSummary


def _state_label(sample: Optional[StatusSample]) -> str:
    if sample is None or sample.online is None:
        return '🟠 Data unavailable'
    return '🟢 Online' if sample.online else '🔴 Offline'


def _players(sample: Optional[StatusSample]) -> str:
    if sample is None or sample.online is None:
        return 'Unknown'
    if sample.players_online is None:
        return 'Unknown'
    maximum = '?' if sample.players_max is None else str(sample.players_max)
    return f'{sample.players_online}/{maximum}'


def _sample_line(sample: Optional[StatusSample]) -> str:
    if sample is None:
        return 'No status checks recorded yet.'
    checked = discord.utils.format_dt(sample.checked_at, style='R')
    if sample.online is None:
        return f'Last check {checked}; status could not be retrieved.'
    return f'Last checked {checked}.'


def _format_duration(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    days, remainder = divmod(minutes, 24 * 60)
    hours, remainder = divmod(remainder, 60)
    if days:
        return f'{days}d {hours}h'
    if hours:
        return f'{hours}h {remainder}m'
    return f'{remainder}m'


def _streak_value(summary: Optional[UptimeSummary], now: datetime) -> str:
    if summary is None or summary.online_since is None:
        return 'No confirmed online streak'
    duration = _format_duration((now - summary.online_since).total_seconds())
    prefix = 'At least ' if summary.streak_is_lower_bound else ''
    return f'{prefix}{duration} · since {discord.utils.format_dt(summary.online_since, style="R")}'


def _uptime_value(summary: Optional[UptimeSummary], now: datetime) -> str:
    if summary is None or summary.coverage_percent is None:
        return 'Collecting status history'
    tracking = ''
    if summary.monitoring_since is not None:
        age = _format_duration((now - summary.monitoring_since).total_seconds())
        tracking = f' · tracking {age}'
    if summary.coverage_percent < 80:
        return f'Inconclusive · {summary.coverage_percent:.0f}% coverage{tracking}'
    if summary.uptime_percent is None:
        return f'No confirmed checks · {summary.coverage_percent:.0f}% coverage{tracking}'
    return (
        f'{summary.uptime_percent:.1f}% · {summary.coverage_percent:.0f}% coverage'
        f'{tracking}'
    )


def _uptime_bar(summary: Optional[UptimeSummary]) -> str:
    if (
        summary is None
        or summary.uptime_percent is None
        or summary.coverage_percent is None
        or summary.coverage_percent < 80
    ):
        return '□□□□□□□□□□'
    filled = round(summary.uptime_percent / 10)
    return '■' * filled + '□' * (10 - filled)


def _history_line(
    server: Server,
    summary: Optional[UptimeSummary],
    now: datetime,
) -> str:
    uptime = _uptime_value(summary, now)
    streak = _streak_value(summary, now)
    return f'**{server.alias}** · {uptime}\nOnline streak: {streak}'


def build_server_embed(
    server: Server,
    sample: Optional[StatusSample],
    *,
    show_ip: bool,
    uptime: Optional[UptimeSummary] = None,
    now: Optional[datetime] = None,
) -> discord.Embed:
    now = now or datetime.now(timezone.utc)
    online = sample is not None and sample.online is True
    address = f'`{server.address}`' if show_ip else 'Address hidden'
    description = f'**COBBLEMON SERVER**  ·  {address}'
    color = discord.Color.from_rgb(74, 132, 91) if online else discord.Color.from_rgb(183, 83, 76)
    if sample is None or sample.online is None:
        color = discord.Color.from_rgb(196, 145, 69)

    embed = discord.Embed(
        title=f'🌿  {server.alias}',
        description=description,
        color=color,
    )
    embed.add_field(name='Status', value=_state_label(sample), inline=True)
    embed.add_field(name='👥 Players', value=_players(sample), inline=True)
    embed.add_field(name='⏱ Online streak', value=_streak_value(uptime, now), inline=True)
    embed.add_field(
        name='📈 Uptime · 24 hours',
        value=f'{_uptime_value(uptime, now)}\n`{_uptime_bar(uptime)}`',
        inline=False,
    )
    if sample is not None and sample.version:
        embed.add_field(name='⚙ Version', value=sample.version[:100], inline=True)
    if sample is not None and sample.motd:
        embed.add_field(name='🌱 Message of the day', value=sample.motd[:500], inline=False)
    embed.add_field(name='Last check', value=_sample_line(sample), inline=False)
    embed.set_thumbnail(
        url=f'https://api.mcsrvstat.us/icon/{quote(server.address, safe=".:[]-")}'
    )
    embed.set_footer(text='COBBLEMON NETWORK  ·  refreshed automatically')
    return embed


def build_combined_embed(
    servers: list[Server],
    samples: dict[int, Optional[StatusSample]],
    settings: GuildSettings,
    *,
    page_number: int = 1,
    page_count: int = 1,
    total_servers: Optional[int] = None,
    total_online: Optional[int] = None,
    uptimes: Optional[dict[int, UptimeSummary]] = None,
    now: Optional[datetime] = None,
) -> discord.Embed:
    now = now or datetime.now(timezone.utc)
    uptimes = uptimes or {}
    online_count = sum(
        1 for server in servers
        if (sample := samples.get(server.id)) is not None and sample.online is True
    )
    total_servers = len(servers) if total_servers is None else total_servers
    total_online = online_count if total_online is None else total_online
    description = f'{total_online}/{total_servers} servers online'
    if page_count > 1:
        description += f' | Page {page_number}/{page_count}'
    embed = discord.Embed(
        title='Minecraft Server Status',
        description=description,
        color=(
            discord.Color.brand_green()
            if total_online == total_servers and total_servers > 0
            else discord.Color.orange()
        ),
    )
    if not servers:
        embed.description = 'No Minecraft servers configured.'
    for server in servers:
        sample = samples.get(server.id)
        label = _state_label(sample)
        summary = uptimes.get(server.id)
        value = f'{label}  ·  👥 {_players(sample)}'
        value += f'\n⏱ {_streak_value(summary, now)}'
        value += f'\n📈 {_uptime_value(summary, now)} `{_uptime_bar(summary)}`'
        if sample is not None and sample.version:
            value += f'  ·  {sample.version[:40]}'
        if sample is not None and sample.motd:
            value += f'\n{sample.motd[:120]}'
        value += f'\n{_sample_line(sample)}'
        if settings.show_ip:
            value += f'\n`{server.address[:100]}`'
        embed.add_field(name=server.alias[:256], value=value[:300], inline=False)
    embed.set_footer(text='Updated automatically')
    return embed


def build_dashboard(
    servers: list[Server],
    samples: dict[int, Optional[StatusSample]],
    settings: GuildSettings,
    uptimes: Optional[dict[int, UptimeSummary]] = None,
    *,
    now: Optional[datetime] = None,
) -> dict[str, discord.Embed]:
    now = now or datetime.now(timezone.utc)
    uptimes = uptimes or {}
    if settings.layout == 'combined':
        page_size = 15
        pages = [servers[index:index + page_size] for index in range(0, len(servers), page_size)]
        if not pages:
            pages = [[]]
        total_online = sum(
            1 for server in servers
            if (sample := samples.get(server.id)) is not None and sample.online is True
        )
        embeds = {
            'combined' if index == 0 else f'combined:{index + 1}': build_combined_embed(
                page,
                samples,
                settings,
                page_number=index + 1,
                page_count=len(pages),
                total_servers=len(servers),
                total_online=total_online,
                uptimes=uptimes,
                now=now,
            )
            for index, page in enumerate(pages)
        }
    else:
        embeds = {
            f'server:{server.id}': build_server_embed(
                server,
                samples.get(server.id),
                show_ip=settings.show_ip,
                uptime=uptimes.get(server.id),
                now=now,
            )
            for server in servers
        }
    if settings.history_enabled:
        history = discord.Embed(
            title='🌱  Field Notes · Monitoring History',
            description=(
                '24-hour uptime is based on confirmed checks. Failed checks are omitted from '
                'uptime and reflected in coverage.'
            ),
            color=discord.Color.from_rgb(87, 126, 85),
        )
        if not servers:
            history.description = 'Add a server to begin collecting monitoring history.'
        else:
            for server in servers[:20]:
                summary = uptimes.get(server.id)
                history.add_field(
                    name=f'🍃 {server.alias}'[:256],
                    value=_history_line(server, summary, now)[:1024],
                    inline=False,
                )
            if len(servers) > 20:
                history.set_footer(text=f'Showing 20 of {len(servers)} servers · use !stats for a server')
        embeds['history'] = history
    return embeds
