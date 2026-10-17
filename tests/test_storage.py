import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from storage import Store


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / 'bot.sqlite3'
        self.store = Store(self.database)
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_servers_and_settings_are_isolated_by_guild(self) -> None:
        await self.store.configure_guild(100, dashboard_channel_id=101, show_ip=True)
        server = await self.store.add_server(100, 'main', 'play.example.org')
        await self.store.add_server(200, 'main', 'other.example.org')

        self.assertEqual([server], await self.store.list_servers(100))
        self.assertEqual([], await self.store.list_servers(300))
        self.assertTrue((await self.store.get_guild_settings(100)).show_ip)
        self.assertFalse((await self.store.get_guild_settings(200)).show_ip)

    async def test_duplicate_alias_is_case_insensitive(self) -> None:
        await self.store.add_server(100, 'Main', 'play.example.org')
        with self.assertRaises(Exception):
            await self.store.add_server(100, 'main', 'another.example.org')

    async def test_history_excludes_failed_fetches_and_prunes_old_rows(self) -> None:
        server = await self.store.add_server(100, 'main', 'play.example.org')
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        await self.store.record_status(
            server.id, checked_at=now - timedelta(hours=1), online=True, players_online=4
        )
        await self.store.record_status(
            server.id, checked_at=now - timedelta(minutes=30), online=False, players_online=0
        )
        await self.store.record_status(
            server.id,
            checked_at=now - timedelta(minutes=10),
            online=None,
            error='provider timeout',
        )
        await self.store.record_status(
            server.id,
            checked_at=now - timedelta(days=31),
            online=True,
            players_online=10,
        )

        summary = await self.store.get_status_summary(server.id, days=1, now=now)
        self.assertEqual(2, summary.sample_count)
        self.assertEqual(1, summary.online_count)
        self.assertEqual(50.0, summary.uptime_percent)
        self.assertEqual(2.0, summary.average_players)
        self.assertEqual(1, await self.store.prune_status_samples(before=now - timedelta(days=30)))

    async def test_legacy_import_is_idempotent_and_preserves_message(self) -> None:
        legacy = {
            'guild_id': 100,
            'channel_id': 101,
            'address': 'play.example.org',
            'message_id': 102,
            'show_ip': True,
        }
        self.assertTrue(await self.store.import_legacy(**legacy))
        self.assertFalse(await self.store.import_legacy(**legacy))

        servers = await self.store.list_servers(100)
        self.assertEqual(1, len(servers))
        self.assertEqual('default', servers[0].alias)
        self.assertEqual((101, 102), await self.store.get_message(100, 'combined'))
        settings = await self.store.get_guild_settings(100)
        self.assertEqual(101, settings.dashboard_channel_id)
        self.assertTrue(settings.show_ip)

    async def test_legacy_import_upserts_existing_default_server_and_message(self) -> None:
        await self.store.add_server(100, 'default', 'new.example.org')
        await self.store.set_message(100, 'combined', 201, 202)

        imported = await self.store.import_legacy(
            guild_id=100,
            channel_id=101,
            address='legacy.example.org',
            message_id=102,
            show_ip=True,
        )

        self.assertTrue(imported)
        servers = await self.store.list_servers(100)
        self.assertEqual(1, len(servers))
        self.assertEqual('legacy.example.org', servers[0].address)
        self.assertEqual((101, 102), await self.store.get_message(100, 'combined'))

    async def test_settings_reject_invalid_layout_and_poll_interval(self) -> None:
        with self.assertRaises(ValueError):
            await self.store.configure_guild(100, layout='other')
        with self.assertRaises(ValueError):
            await self.store.configure_guild(100, poll_interval_minutes=0)

    async def test_history_setting_defaults_off_and_is_guild_scoped(self) -> None:
        self.assertFalse((await self.store.get_guild_settings(100)).history_enabled)
        await self.store.configure_guild(100, history_enabled=True)

        self.assertTrue((await self.store.get_guild_settings(100)).history_enabled)
        self.assertFalse((await self.store.get_guild_settings(200)).history_enabled)

    async def test_initialize_migrates_existing_guild_settings_idempotently(self) -> None:
        old_database = Path(self.temp_dir.name) / 'old.sqlite3'
        async with aiosqlite.connect(old_database) as db:
            await db.execute(
                '''
                CREATE TABLE guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    dashboard_channel_id INTEGER,
                    layout TEXT NOT NULL DEFAULT 'combined',
                    poll_interval_minutes INTEGER NOT NULL DEFAULT 5,
                    show_ip INTEGER NOT NULL DEFAULT 0
                )
                '''
            )
            await db.execute('INSERT INTO guild_settings (guild_id) VALUES (100)')
            await db.commit()

        old_store = Store(old_database)
        await old_store.initialize()
        await old_store.initialize()
        settings = await old_store.get_guild_settings(100)
        self.assertFalse(settings.history_enabled)
        await old_store.configure_guild(100, history_enabled=True)
        self.assertTrue((await old_store.get_guild_settings(100)).history_enabled)

    async def test_uptime_coverage_and_streak_ignore_failed_checks(self) -> None:
        server = await self.store.add_server(100, 'main', 'play.example.org')
        now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
        await self.store.record_status(
            server.id, checked_at=now - timedelta(hours=3), online=True
        )
        await self.store.record_status(
            server.id, checked_at=now - timedelta(hours=2), online=True
        )
        await self.store.record_status(
            server.id,
            checked_at=now - timedelta(hours=1),
            online=None,
            error='timeout',
        )
        await self.store.record_status(server.id, checked_at=now, online=True)

        summary = await self.store.get_uptime_summary(
            server.id, interval_minutes=60, now=now
        )

        self.assertEqual(3, summary.successful_count)
        self.assertEqual(3, summary.online_count)
        self.assertEqual(100.0, summary.uptime_percent)
        self.assertEqual(75.0, summary.coverage_percent)
        self.assertEqual(now - timedelta(hours=3), summary.online_since)
        self.assertTrue(summary.streak_is_lower_bound)

    async def test_confirmed_offline_resets_online_streak(self) -> None:
        server = await self.store.add_server(100, 'main', 'play.example.org')
        now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
        await self.store.record_status(
            server.id, checked_at=now - timedelta(hours=4), online=True
        )
        await self.store.record_status(
            server.id,
            checked_at=now - timedelta(hours=3),
            online=None,
            error='timeout',
        )
        await self.store.record_status(
            server.id, checked_at=now - timedelta(hours=2), online=False
        )
        await self.store.record_status(
            server.id, checked_at=now - timedelta(hours=1), online=True
        )

        summary = await self.store.get_uptime_summary(
            server.id, interval_minutes=60, now=now
        )

        self.assertEqual(now - timedelta(hours=1), summary.online_since)
        self.assertFalse(summary.streak_is_lower_bound)

    async def test_quest_snapshots_replace_and_rank_team_progress(self) -> None:
        await self.store.bind_quest_source('world', 100, 101)
        synced_at = datetime(2026, 10, 17, tzinfo=timezone.utc)
        await self.store.save_quest_team_snapshot(
            source_id='world',
            team_id='team-a',
            team_name='A Team',
            progress_percent=50,
            completed_count=3,
            total_count=6,
            completed_quests=['First', 'Second', 'Third'],
            synced_at=synced_at,
        )
        await self.store.save_quest_team_snapshot(
            source_id='world',
            team_id='team-a',
            team_name='A Team Renamed',
            progress_percent=80,
            completed_count=4,
            total_count=5,
            completed_quests=['First', 'Second', 'Third', 'Fourth'],
            synced_at=synced_at + timedelta(minutes=1),
        )
        await self.store.save_quest_team_snapshot(
            source_id='world',
            team_id='team-b',
            team_name='B Team',
            progress_percent=80,
            completed_count=3,
            total_count=5,
            completed_quests=['First', 'Second', 'Third'],
            synced_at=synced_at,
        )

        leaderboard = await self.store.get_quest_leaderboard('world')
        team = await self.store.get_quest_team_snapshot('world', 'team-a')

        self.assertEqual(['team-a', 'team-b'], [row['team_id'] for row in leaderboard])
        self.assertEqual(80, team['progress_percent'])
        self.assertEqual('A Team Renamed', team['team_name'])
        self.assertEqual(4, len(team['completed_quests']))

    async def test_quest_link_code_is_single_use_and_enforces_uuid_uniqueness(self) -> None:
        await self.store.bind_quest_source('world', 100, 101)
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        expires = now + timedelta(minutes=5)
        self.assertTrue(
            await self.store.create_quest_link_code(
                source_id='world',
                guild_id=100,
                discord_user_id=200,
                code_hash='hash-one',
                expires_at=expires,
                now=now,
            )
        )
        self.assertTrue(
            await self.store.consume_quest_link_code(
                source_id='world',
                code_hash='hash-one',
                player_uuid='player-one',
                player_name='Trainer One',
                team_id='team-one',
                now=now,
            )
        )
        self.assertIsNone(
            await self.store.get_quest_link('world', 100, 201)
        )
        self.assertEqual(
            'team-one', (await self.store.get_quest_link('world', 100, 200))['team_id']
        )
        self.assertFalse(
            await self.store.consume_quest_link_code(
                source_id='world',
                code_hash='hash-one',
                player_uuid='player-two',
                player_name='Trainer Two',
                team_id='team-two',
                now=now,
            )
        )

        await self.store.create_quest_link_code(
            source_id='world',
            guild_id=100,
            discord_user_id=201,
            code_hash='hash-two',
            expires_at=expires,
            now=now,
        )
        self.assertFalse(
            await self.store.consume_quest_link_code(
                source_id='world',
                code_hash='hash-two',
                player_uuid='player-one',
                player_name='Trainer One',
                team_id='team-one',
                now=now,
            )
        )

    async def test_expired_quest_link_code_cannot_be_consumed(self) -> None:
        await self.store.bind_quest_source('world', 100, 101)
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        await self.store.create_quest_link_code(
            source_id='world',
            guild_id=100,
            discord_user_id=200,
            code_hash='expired-hash',
            expires_at=now - timedelta(seconds=1),
            now=now - timedelta(minutes=6),
        )

        linked = await self.store.consume_quest_link_code(
            source_id='world',
            code_hash='expired-hash',
            player_uuid='player-one',
            player_name='Trainer One',
            team_id='team-one',
            now=now,
        )

        self.assertFalse(linked)


if __name__ == '__main__':
    unittest.main()