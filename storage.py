from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import AsyncIterator, Optional

import aiosqlite


@dataclass(frozen=True)
class Server:
    id: int
    guild_id: int
    alias: str
    address: str


@dataclass(frozen=True)
class GuildSettings:
    dashboard_channel_id: Optional[int] = None
    layout: str = 'combined'
    poll_interval_minutes: int = 5
    show_ip: bool = False
    history_enabled: bool = False


@dataclass(frozen=True)
class StatusSummary:
    sample_count: int
    online_count: int
    uptime_percent: Optional[float]
    average_players: Optional[float]


@dataclass(frozen=True)
class StatusSample:
    checked_at: datetime
    online: Optional[bool]
    players_online: Optional[int]
    players_max: Optional[int]
    version: Optional[str]
    motd: Optional[str]
    error: Optional[str]


@dataclass(frozen=True)
class UptimeSummary:
    successful_count: int
    online_count: int
    uptime_percent: Optional[float]
    coverage_percent: Optional[float]
    monitoring_since: Optional[datetime]
    online_since: Optional[datetime]
    streak_is_lower_bound: bool


@dataclass(frozen=True)
class QuestSource:
    source_id: str
    guild_id: int
    channel_id: int
    announcements_enabled: bool


@dataclass(frozen=True)
class QuestAnnouncement:
    id: int
    source_id: str
    guild_id: int
    channel_id: int
    team_name: str
    quest_name: str
    attempts: int


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        await connection.execute('PRAGMA foreign_keys = ON')
        try:
            yield connection
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise
        finally:
            await connection.close()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connection() as db:
            await db.executescript(
                '''
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    dashboard_channel_id INTEGER,
                    layout TEXT NOT NULL DEFAULT 'combined'
                        CHECK (layout IN ('combined', 'per_server')),
                    poll_interval_minutes INTEGER NOT NULL DEFAULT 5
                        CHECK (poll_interval_minutes BETWEEN 1 AND 60),
                    show_ip INTEGER NOT NULL DEFAULT 0 CHECK (show_ip IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS servers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL REFERENCES guild_settings(guild_id) ON DELETE CASCADE,
                    alias TEXT NOT NULL,
                    address TEXT NOT NULL,
                    UNIQUE (guild_id, alias COLLATE NOCASE)
                );
                CREATE TABLE IF NOT EXISTS dashboard_messages (
                    guild_id INTEGER NOT NULL REFERENCES guild_settings(guild_id) ON DELETE CASCADE,
                    message_key TEXT NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, message_key)
                );
                CREATE TABLE IF NOT EXISTS status_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                    checked_at TEXT NOT NULL,
                    online INTEGER CHECK (online IN (0, 1) OR online IS NULL),
                    players_online INTEGER,
                    players_max INTEGER,
                    version TEXT,
                    motd TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS status_samples_server_time
                    ON status_samples(server_id, checked_at);
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quest_sources (
                    source_id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL REFERENCES guild_settings(guild_id) ON DELETE CASCADE,
                    channel_id INTEGER NOT NULL,
                    announcements_enabled INTEGER NOT NULL DEFAULT 0
                        CHECK (announcements_enabled IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS quest_events (
                    source_id TEXT NOT NULL REFERENCES quest_sources(source_id) ON DELETE CASCADE,
                    event_id TEXT NOT NULL,
                    team_id TEXT NOT NULL,
                    team_name TEXT NOT NULL,
                    quest_id TEXT NOT NULL,
                    quest_name TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS quest_events_source_team
                    ON quest_events(source_id, team_id, occurred_at);
                CREATE TABLE IF NOT EXISTS quest_nonces (
                    nonce TEXT PRIMARY KEY,
                    seen_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quest_announcement_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL REFERENCES quest_sources(source_id) ON DELETE CASCADE,
                    event_id TEXT NOT NULL,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    retry_after INTEGER NOT NULL DEFAULT 0,
                    sent_at TEXT,
                    UNIQUE (source_id, event_id),
                    FOREIGN KEY (source_id, event_id)
                        REFERENCES quest_events(source_id, event_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS quest_team_snapshots (
                    source_id TEXT NOT NULL REFERENCES quest_sources(source_id) ON DELETE CASCADE,
                    team_id TEXT NOT NULL,
                    team_name TEXT NOT NULL,
                    progress_percent INTEGER NOT NULL CHECK (progress_percent BETWEEN 0 AND 100),
                    completed_count INTEGER NOT NULL CHECK (completed_count >= 0),
                    total_count INTEGER NOT NULL CHECK (total_count >= 0),
                    completed_quests TEXT NOT NULL,
                    synced_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, team_id)
                );
                CREATE TABLE IF NOT EXISTS quest_link_codes (
                    code_hash TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES quest_sources(source_id) ON DELETE CASCADE,
                    discord_user_id TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS quest_link_codes_user
                    ON quest_link_codes(source_id, discord_user_id, expires_at);
                CREATE TABLE IF NOT EXISTS quest_links (
                    source_id TEXT NOT NULL REFERENCES quest_sources(source_id) ON DELETE CASCADE,
                    discord_user_id TEXT NOT NULL,
                    player_uuid TEXT NOT NULL,
                    player_name TEXT NOT NULL,
                    team_id TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, discord_user_id),
                    UNIQUE (source_id, player_uuid)
                );
                CREATE TABLE IF NOT EXISTS quest_sources_teams (
                    source_id TEXT NOT NULL REFERENCES quest_sources(source_id) ON DELETE CASCADE,
                    team_id TEXT NOT NULL,
                    team_name TEXT NOT NULL,
                    PRIMARY KEY (source_id, team_id)
                );
                '''
            )
            cursor = await db.execute('PRAGMA table_info(guild_settings)')
            columns = {row['name'] for row in await cursor.fetchall()}
            if 'history_enabled' not in columns:
                await db.execute(
                    'ALTER TABLE guild_settings ADD COLUMN history_enabled INTEGER NOT NULL DEFAULT 0'
                )

    async def get_guild_settings(self, guild_id: int) -> GuildSettings:
        async with self._connection() as db:
            cursor = await db.execute(
                'SELECT * FROM guild_settings WHERE guild_id = ?', (guild_id,)
            )
            row = await cursor.fetchone()
        if row is None:
            return GuildSettings()
        return GuildSettings(
            dashboard_channel_id=row['dashboard_channel_id'],
            layout=row['layout'],
            poll_interval_minutes=row['poll_interval_minutes'],
            show_ip=bool(row['show_ip']),
            history_enabled=bool(row['history_enabled']),
        )

    async def configure_guild(
        self,
        guild_id: int,
        *,
        dashboard_channel_id: Optional[int] = None,
        layout: Optional[str] = None,
        poll_interval_minutes: Optional[int] = None,
        show_ip: Optional[bool] = None,
        history_enabled: Optional[bool] = None,
    ) -> GuildSettings:
        current = await self.get_guild_settings(guild_id)
        updated = GuildSettings(
            dashboard_channel_id=(
                dashboard_channel_id
                if dashboard_channel_id is not None
                else current.dashboard_channel_id
            ),
            layout=layout if layout is not None else current.layout,
            poll_interval_minutes=(
                poll_interval_minutes
                if poll_interval_minutes is not None
                else current.poll_interval_minutes
            ),
            show_ip=show_ip if show_ip is not None else current.show_ip,
            history_enabled=(
                history_enabled if history_enabled is not None else current.history_enabled
            ),
        )
        if updated.layout not in {'combined', 'per_server'}:
            raise ValueError('layout must be "combined" or "per_server"')
        if not 1 <= updated.poll_interval_minutes <= 60:
            raise ValueError('poll interval must be between 1 and 60 minutes')

        async with self._connection() as db:
            await db.execute(
                '''
                INSERT INTO guild_settings (
                    guild_id, dashboard_channel_id, layout, poll_interval_minutes, show_ip,
                    history_enabled
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    dashboard_channel_id = excluded.dashboard_channel_id,
                    layout = excluded.layout,
                    poll_interval_minutes = excluded.poll_interval_minutes,
                    show_ip = excluded.show_ip,
                    history_enabled = excluded.history_enabled
                ''',
                (
                    guild_id,
                    updated.dashboard_channel_id,
                    updated.layout,
                    updated.poll_interval_minutes,
                    int(updated.show_ip),
                    int(updated.history_enabled),
                ),
            )
        return updated

    async def list_servers(self, guild_id: int) -> list[Server]:
        async with self._connection() as db:
            cursor = await db.execute(
                'SELECT * FROM servers WHERE guild_id = ? ORDER BY alias COLLATE NOCASE',
                (guild_id,),
            )
            rows = await cursor.fetchall()
        return [Server(**dict(row)) for row in rows]

    async def get_server(self, guild_id: int, alias: str) -> Optional[Server]:
        async with self._connection() as db:
            cursor = await db.execute(
                'SELECT * FROM servers WHERE guild_id = ? AND alias = ? COLLATE NOCASE',
                (guild_id, alias),
            )
            row = await cursor.fetchone()
        return Server(**dict(row)) if row is not None else None

    async def add_server(self, guild_id: int, alias: str, address: str) -> Server:
        await self.configure_guild(guild_id)
        async with self._connection() as db:
            cursor = await db.execute(
                'INSERT INTO servers (guild_id, alias, address) VALUES (?, ?, ?)',
                (guild_id, alias.strip(), address.strip()),
            )
            server_id = cursor.lastrowid
        server = await self.get_server(guild_id, alias)
        if server is None or server.id != server_id:
            raise RuntimeError('Failed to load newly created server')
        return server

    async def remove_server(self, guild_id: int, alias: str) -> bool:
        server = await self.get_server(guild_id, alias)
        if server is None:
            return False
        async with self._connection() as db:
            await db.execute('DELETE FROM servers WHERE id = ?', (server.id,))
        return True

    async def get_message(self, guild_id: int, message_key: str) -> Optional[tuple[int, int]]:
        async with self._connection() as db:
            cursor = await db.execute(
                '''
                SELECT channel_id, message_id FROM dashboard_messages
                WHERE guild_id = ? AND message_key = ?
                ''',
                (guild_id, message_key),
            )
            row = await cursor.fetchone()
        return (row['channel_id'], row['message_id']) if row is not None else None

    async def set_message(
        self, guild_id: int, message_key: str, channel_id: int, message_id: int
    ) -> None:
        await self.configure_guild(guild_id)
        async with self._connection() as db:
            await db.execute(
                '''
                INSERT INTO dashboard_messages (guild_id, message_key, channel_id, message_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, message_key) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    message_id = excluded.message_id
                ''',
                (guild_id, message_key, channel_id, message_id),
            )

    async def list_messages(self, guild_id: int) -> list[tuple[str, int, int]]:
        async with self._connection() as db:
            cursor = await db.execute(
                '''
                SELECT message_key, channel_id, message_id FROM dashboard_messages
                WHERE guild_id = ? ORDER BY message_key
                ''',
                (guild_id,),
            )
            rows = await cursor.fetchall()
        return [(row['message_key'], row['channel_id'], row['message_id']) for row in rows]

    async def delete_message(self, guild_id: int, message_key: str) -> None:
        async with self._connection() as db:
            await db.execute(
                'DELETE FROM dashboard_messages WHERE guild_id = ? AND message_key = ?',
                (guild_id, message_key),
            )

    async def bind_quest_source(
        self, source_id: str, guild_id: int, channel_id: int
    ) -> bool:
        await self.configure_guild(guild_id)
        async with self._connection() as db:
            cursor = await db.execute(
                '''
                INSERT INTO quest_sources (source_id, guild_id, channel_id)
                VALUES (?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET channel_id = excluded.channel_id
                WHERE quest_sources.guild_id = excluded.guild_id
                ''',
                (source_id, guild_id, channel_id),
            )
        return cursor.rowcount > 0

    async def list_quest_sources(self, guild_id: int) -> list[QuestSource]:
        async with self._connection() as db:
            cursor = await db.execute(
                'SELECT * FROM quest_sources WHERE guild_id = ? ORDER BY source_id',
                (guild_id,),
            )
            rows = await cursor.fetchall()
        return [
            QuestSource(
                source_id=row['source_id'],
                guild_id=row['guild_id'],
                channel_id=row['channel_id'],
                announcements_enabled=bool(row['announcements_enabled']),
            )
            for row in rows
        ]

    async def get_quest_source(self, source_id: str) -> Optional[QuestSource]:
        async with self._connection() as db:
            cursor = await db.execute(
                'SELECT * FROM quest_sources WHERE source_id = ?', (source_id,)
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return QuestSource(
            source_id=row['source_id'],
            guild_id=row['guild_id'],
            channel_id=row['channel_id'],
            announcements_enabled=bool(row['announcements_enabled']),
        )

    async def set_quest_announcements(
        self, guild_id: int, source_id: str, enabled: bool
    ) -> bool:
        async with self._connection() as db:
            cursor = await db.execute(
                '''
                UPDATE quest_sources SET announcements_enabled = ?
                WHERE source_id = ? AND guild_id = ?
                ''',
                (int(enabled), source_id, guild_id),
            )
            if not enabled and cursor.rowcount:
                await db.execute(
                    '''
                    DELETE FROM quest_announcement_outbox
                    WHERE source_id = ? AND sent_at IS NULL
                    ''',
                    (source_id,),
                )
        return cursor.rowcount > 0

    async def receive_quest_completion(
        self,
        *,
        source_id: str,
        event_id: str,
        team_id: str,
        team_name: str,
        quest_id: str,
        quest_name: str,
        occurred_at: datetime,
        nonce: str,
        received_at: Optional[datetime] = None,
    ) -> str:
        received = (received_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        async with self._connection() as db:
            cursor = await db.execute(
                'SELECT * FROM quest_sources WHERE source_id = ?', (source_id,)
            )
            source = await cursor.fetchone()
            if source is None:
                return 'unbound'

            await db.execute(
                'DELETE FROM quest_nonces WHERE seen_at < ?',
                (int(received.timestamp()) - 600,),
            )
            try:
                await db.execute(
                    'INSERT INTO quest_nonces (nonce, seen_at) VALUES (?, ?)',
                    (nonce, int(received.timestamp())),
                )
            except aiosqlite.IntegrityError:
                return 'replay'

            cursor = await db.execute(
                '''
                INSERT INTO quest_events (
                    source_id, event_id, team_id, team_name, quest_id, quest_name,
                    occurred_at, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, event_id) DO NOTHING
                ''',
                (
                    source_id,
                    event_id,
                    team_id,
                    team_name,
                    quest_id,
                    quest_name,
                    occurred_at.astimezone(timezone.utc).isoformat(),
                    received.isoformat(),
                ),
            )
            if cursor.rowcount == 0:
                return 'duplicate'

            if source['announcements_enabled']:
                await db.execute(
                    '''
                    INSERT INTO quest_announcement_outbox (
                        source_id, event_id, guild_id, channel_id
                    ) VALUES (?, ?, ?, ?)
                    ''',
                    (source_id, event_id, source['guild_id'], source['channel_id']),
                )
        return 'accepted'

    async def get_pending_quest_announcements(
        self, *, now: Optional[datetime] = None, limit: int = 20
    ) -> list[QuestAnnouncement]:
        timestamp = int((now or datetime.now(timezone.utc)).timestamp())
        async with self._connection() as db:
            cursor = await db.execute(
                '''
                SELECT outbox.id, outbox.source_id, outbox.guild_id, outbox.channel_id,
                       outbox.attempts, event.team_name, event.quest_name
                FROM quest_announcement_outbox AS outbox
                JOIN quest_events AS event
                  ON event.source_id = outbox.source_id AND event.event_id = outbox.event_id
                WHERE outbox.sent_at IS NULL AND outbox.retry_after <= ?
                ORDER BY outbox.id LIMIT ?
                ''',
                (timestamp, limit),
            )
            rows = await cursor.fetchall()
        return [QuestAnnouncement(**dict(row)) for row in rows]

    async def mark_quest_announcement_sent(self, announcement_id: int) -> None:
        async with self._connection() as db:
            await db.execute(
                '''
                UPDATE quest_announcement_outbox SET sent_at = ?
                WHERE id = ? AND sent_at IS NULL
                ''',
                (datetime.now(timezone.utc).isoformat(), announcement_id),
            )

    async def defer_quest_announcement(
        self, announcement_id: int, *, retry_after: datetime
    ) -> None:
        async with self._connection() as db:
            await db.execute(
                '''
                UPDATE quest_announcement_outbox
                SET attempts = attempts + 1, retry_after = ?
                WHERE id = ? AND sent_at IS NULL
                ''',
                (int(retry_after.timestamp()), announcement_id),
            )

    async def save_quest_team_snapshot(
        self,
        *,
        source_id: str,
        team_id: str,
        team_name: str,
        progress_percent: int,
        completed_count: int,
        total_count: int,
        completed_quests: list[str],
        synced_at: Optional[datetime] = None,
    ) -> bool:
        synced = (synced_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        async with self._connection() as db:
            cursor = await db.execute(
                'SELECT 1 FROM quest_sources WHERE source_id = ?', (source_id,)
            )
            if await cursor.fetchone() is None:
                return False
            await db.execute(
                '''
                INSERT INTO quest_team_snapshots (
                    source_id, team_id, team_name, progress_percent, completed_count,
                    total_count, completed_quests, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, team_id) DO UPDATE SET
                    team_name = excluded.team_name,
                    progress_percent = excluded.progress_percent,
                    completed_count = excluded.completed_count,
                    total_count = excluded.total_count,
                    completed_quests = excluded.completed_quests,
                    synced_at = excluded.synced_at
                ''',
                (
                    source_id,
                    team_id,
                    team_name,
                    progress_percent,
                    completed_count,
                    total_count,
                    json.dumps(completed_quests[:100]),
                    synced.isoformat(),
                ),
            )
            await db.execute(
                '''
                INSERT INTO quest_sources_teams (source_id, team_id, team_name)
                VALUES (?, ?, ?)
                ON CONFLICT(source_id, team_id) DO UPDATE SET team_name = excluded.team_name
                ''',
                (source_id, team_id, team_name),
            )
        return True

    async def get_quest_team_snapshot(self, source_id: str, team_id: str) -> Optional[dict[str, Any]]:
        async with self._connection() as db:
            cursor = await db.execute(
                '''
                SELECT team_id, team_name, progress_percent, completed_count, total_count,
                       completed_quests, synced_at
                FROM quest_team_snapshots WHERE source_id = ? AND team_id = ?
                ''',
                (source_id, team_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return {
            'team_id': row['team_id'],
            'team_name': row['team_name'],
            'progress_percent': row['progress_percent'],
            'completed_count': row['completed_count'],
            'total_count': row['total_count'],
            'completed_quests': json.loads(row['completed_quests']),
            'synced_at': datetime.fromisoformat(row['synced_at']),
        }

    async def get_quest_leaderboard(
        self, source_id: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        async with self._connection() as db:
            cursor = await db.execute(
                '''
                SELECT team_id, team_name, progress_percent, completed_count, total_count, synced_at
                FROM quest_team_snapshots WHERE source_id = ?
                ORDER BY progress_percent DESC, completed_count DESC, team_name COLLATE NOCASE
                LIMIT ?
                ''',
                (source_id, limit),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def create_quest_link_code(
        self,
        *,
        source_id: str,
        guild_id: int,
        discord_user_id: int,
        code_hash: str,
        expires_at: datetime,
        now: Optional[datetime] = None,
    ) -> bool:
        current = int((now or datetime.now(timezone.utc)).timestamp())
        expiry = int(expires_at.timestamp())
        async with self._connection() as db:
            cursor = await db.execute(
                '''
                SELECT 1 FROM quest_sources WHERE source_id = ? AND guild_id = ?
                ''',
                (source_id, guild_id),
            )
            if await cursor.fetchone() is None:
                return False
            await db.execute(
                'DELETE FROM quest_link_codes WHERE source_id = ? AND discord_user_id = ?',
                (source_id, str(discord_user_id)),
            )
            await db.execute(
                '''
                INSERT INTO quest_link_codes (code_hash, source_id, discord_user_id, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                ''',
                (code_hash, source_id, str(discord_user_id), expiry, current),
            )
        return True

    async def consume_quest_link_code(
        self,
        *,
        source_id: str,
        code_hash: str,
        player_uuid: str,
        player_name: str,
        team_id: str,
        now: Optional[datetime] = None,
    ) -> bool:
        current = int((now or datetime.now(timezone.utc)).timestamp())
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    '''
                    SELECT discord_user_id FROM quest_link_codes
                    WHERE code_hash = ? AND source_id = ? AND expires_at >= ?
                    ''',
                    (code_hash, source_id, current),
                )
                row = await cursor.fetchone()
                if row is None:
                    return False
                await db.execute(
                    '''
                    INSERT INTO quest_links (
                        source_id, discord_user_id, player_uuid, player_name, team_id, linked_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, discord_user_id) DO UPDATE SET
                        player_uuid = excluded.player_uuid,
                        player_name = excluded.player_name,
                        team_id = excluded.team_id,
                        linked_at = excluded.linked_at
                    ''',
                    (
                        source_id,
                        row['discord_user_id'],
                        player_uuid,
                        player_name,
                        team_id,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                await db.execute('DELETE FROM quest_link_codes WHERE code_hash = ?', (code_hash,))
        except aiosqlite.IntegrityError:
            return False
        return True

    async def get_quest_link(
        self, source_id: str, guild_id: int, discord_user_id: int
    ) -> Optional[dict[str, str]]:
        async with self._connection() as db:
            cursor = await db.execute(
                '''
                SELECT link.player_uuid, link.player_name, link.team_id
                FROM quest_links AS link
                JOIN quest_sources AS source ON source.source_id = link.source_id
                WHERE link.source_id = ? AND source.guild_id = ? AND link.discord_user_id = ?
                ''',
                (source_id, guild_id, str(discord_user_id)),
            )
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def unlink_quest_player(
        self, source_id: str, guild_id: int, discord_user_id: int
    ) -> bool:
        async with self._connection() as db:
            cursor = await db.execute(
                '''
                DELETE FROM quest_links
                WHERE source_id = ? AND discord_user_id = ?
                  AND source_id IN (SELECT source_id FROM quest_sources WHERE guild_id = ?)
                ''',
                (source_id, str(discord_user_id), guild_id),
            )
        return cursor.rowcount > 0

    async def record_status(
        self,
        server_id: int,
        *,
        checked_at: datetime,
        online: Optional[bool],
        players_online: Optional[int] = None,
        players_max: Optional[int] = None,
        version: Optional[str] = None,
        motd: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        timestamp = checked_at.astimezone(timezone.utc).isoformat()
        async with self._connection() as db:
            await db.execute(
                '''
                INSERT INTO status_samples (
                    server_id, checked_at, online, players_online, players_max, version, motd, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    server_id,
                    timestamp,
                    None if online is None else int(online),
                    players_online,
                    players_max,
                    version,
                    motd,
                    error,
                ),
            )

    async def get_latest_status(self, server_id: int) -> Optional[StatusSample]:
        async with self._connection() as db:
            cursor = await db.execute(
                '''
                SELECT checked_at, online, players_online, players_max, version, motd, error
                FROM status_samples WHERE server_id = ? ORDER BY checked_at DESC, id DESC LIMIT 1
                ''',
                (server_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return StatusSample(
            checked_at=datetime.fromisoformat(row['checked_at']),
            online=None if row['online'] is None else bool(row['online']),
            players_online=row['players_online'],
            players_max=row['players_max'],
            version=row['version'],
            motd=row['motd'],
            error=row['error'],
        )

    async def get_status_summary(
        self, server_id: int, *, days: int, now: Optional[datetime] = None
    ) -> StatusSummary:
        if days <= 0:
            raise ValueError('days must be positive')
        end = now or datetime.now(timezone.utc)
        since = (end - timedelta(days=days)).astimezone(timezone.utc).isoformat()
        async with self._connection() as db:
            cursor = await db.execute(
                '''
                SELECT COUNT(online) AS sample_count,
                       COALESCE(SUM(online), 0) AS online_count,
                       AVG(players_online) AS average_players
                FROM status_samples
                WHERE server_id = ? AND checked_at >= ? AND online IS NOT NULL
                ''',
                (server_id, since),
            )
            row = await cursor.fetchone()
        sample_count = row['sample_count']
        online_count = row['online_count']
        return StatusSummary(
            sample_count=sample_count,
            online_count=online_count,
            uptime_percent=(online_count / sample_count * 100) if sample_count else None,
            average_players=row['average_players'],
        )

    async def get_uptime_summary(
        self,
        server_id: int,
        *,
        interval_minutes: int,
        hours: int = 24,
        now: Optional[datetime] = None,
    ) -> UptimeSummary:
        if interval_minutes < 1 or hours < 1:
            raise ValueError('interval_minutes and hours must be positive')

        end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        window_start = end - timedelta(hours=hours)
        retention_start = end - timedelta(days=30)
        async with self._connection() as db:
            cursor = await db.execute(
                '''
                SELECT checked_at, online FROM status_samples
                WHERE server_id = ? AND checked_at >= ? AND checked_at <= ?
                ORDER BY checked_at, id
                ''',
                (server_id, window_start.isoformat(), end.isoformat()),
            )
            window_rows = await cursor.fetchall()
            cursor = await db.execute(
                '''
                SELECT checked_at, online FROM status_samples
                WHERE server_id = ? AND checked_at >= ? AND checked_at <= ? AND online IS NOT NULL
                ORDER BY checked_at, id
                ''',
                (server_id, retention_start.isoformat(), end.isoformat()),
            )
            retained_rows = await cursor.fetchall()

        confirmed = [row for row in window_rows if row['online'] is not None]
        successful_count = len(confirmed)
        online_count = sum(1 for row in confirmed if row['online'] == 1)
        monitoring_since = (
            datetime.fromisoformat(window_rows[0]['checked_at']) if window_rows else None
        )
        coverage_percent: Optional[float] = None
        if monitoring_since is not None:
            monitored_from = max(window_start, monitoring_since)
            interval_seconds = interval_minutes * 60
            expected_count = max(
                1,
                int((end - monitored_from).total_seconds() // interval_seconds) + 1,
            )
            coverage_percent = min(successful_count / expected_count * 100, 100.0)

        online_since: Optional[datetime] = None
        streak_is_lower_bound = False
        saw_offline = False
        for row in retained_rows:
            if row['online'] == 0:
                online_since = None
                saw_offline = True
            elif row['online'] == 1 and online_since is None:
                online_since = datetime.fromisoformat(row['checked_at'])
                streak_is_lower_bound = not saw_offline

        return UptimeSummary(
            successful_count=successful_count,
            online_count=online_count,
            uptime_percent=(online_count / successful_count * 100) if successful_count else None,
            coverage_percent=coverage_percent,
            monitoring_since=monitoring_since,
            online_since=online_since,
            streak_is_lower_bound=streak_is_lower_bound,
        )

    async def prune_status_samples(self, *, before: datetime) -> int:
        timestamp = before.astimezone(timezone.utc).isoformat()
        async with self._connection() as db:
            cursor = await db.execute(
                'DELETE FROM status_samples WHERE checked_at < ?', (timestamp,)
            )
        return cursor.rowcount

    async def import_legacy(
        self,
        *,
        guild_id: int,
        channel_id: int,
        address: str,
        message_id: Optional[int],
        show_ip: bool,
    ) -> bool:
        async with self._connection() as db:
            cursor = await db.execute(
                'SELECT value FROM metadata WHERE key = ?', ('legacy_import_v1',)
            )
            if await cursor.fetchone() is not None:
                return False

            await db.execute(
                '''
                INSERT INTO guild_settings (guild_id, dashboard_channel_id, show_ip)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    dashboard_channel_id = COALESCE(
                        guild_settings.dashboard_channel_id, excluded.dashboard_channel_id
                    ),
                    show_ip = excluded.show_ip
                ''',
                (guild_id, channel_id, int(show_ip)),
            )
            await db.execute(
                '''
                INSERT INTO servers (guild_id, alias, address) VALUES (?, 'default', ?)
                ON CONFLICT(guild_id, alias) DO UPDATE SET address = excluded.address
                ''',
                (guild_id, address),
            )
            cursor = await db.execute(
                'SELECT id FROM servers WHERE guild_id = ? AND alias = ? COLLATE NOCASE',
                (guild_id, 'default'),
            )
            server_id = (await cursor.fetchone())['id']
            if message_id is not None:
                await db.execute(
                    '''
                    INSERT INTO dashboard_messages (guild_id, message_key, channel_id, message_id)
                    VALUES (?, 'combined', ?, ?)
                    ON CONFLICT(guild_id, message_key) DO UPDATE SET
                        channel_id = excluded.channel_id,
                        message_id = excluded.message_id
                    ''',
                    (guild_id, channel_id, message_id),
                )
            await db.execute(
                'INSERT INTO metadata (key, value) VALUES (?, ?)',
                ('legacy_import_v1', str(server_id)),
            )
        return True

    async def register_quest_nonce(
        self, nonce: str, *, now: Optional[datetime] = None
    ) -> bool:
        current = int((now or datetime.now(timezone.utc)).timestamp())
        async with self._connection() as db:
            await db.execute('DELETE FROM quest_nonces WHERE seen_at < ?', (current - 600,))
            try:
                await db.execute(
                    'INSERT INTO quest_nonces (nonce, seen_at) VALUES (?, ?)',
                    (nonce, current),
                )
            except aiosqlite.IntegrityError:
                return False
        return True