import unittest
from datetime import datetime, timedelta, timezone

from dashboard import build_dashboard
from storage import GuildSettings, Server, StatusSample, UptimeSummary


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.servers = [
            Server(id=index, guild_id=10, alias=f'server-{index}', address=f'mc{index}.example.org')
            for index in range(1, 32)
        ]
        self.samples = {
            server.id: StatusSample(
                checked_at=datetime(2026, 10, 17, tzinfo=timezone.utc),
                online=server.id % 2 == 0,
                players_online=3,
                players_max=20,
                version='1.21.1',
                motd='A Cobblemon server',
                error=None,
            )
            for server in self.servers
        }

    def test_combined_layout_paginates_within_embed_limits(self) -> None:
        embeds = build_dashboard(
            self.servers,
            self.samples,
            GuildSettings(layout='combined', show_ip=True),
        )

        self.assertEqual({'combined', 'combined:2', 'combined:3'}, set(embeds))
        self.assertTrue(all(len(embed.fields) <= 15 for embed in embeds.values()))
        self.assertTrue(all(len(embed) <= 6000 for embed in embeds.values()))
        self.assertTrue(all('15/31 servers online' in embed.description for embed in embeds.values()))
        self.assertEqual(embeds['combined'].color, embeds['combined:2'].color)
        self.assertIn('mc1.example.org', embeds['combined'].fields[0].value)

    def test_per_server_layout_creates_one_embed_per_server(self) -> None:
        embeds = build_dashboard(
            self.servers,
            self.samples,
            GuildSettings(layout='per_server'),
        )

        self.assertEqual(31, len(embeds))
        self.assertIn('server:1', embeds)
        self.assertIn('Address hidden', embeds['server:1'].description)
        self.assertEqual('🌿  server-1', embeds['server:1'].title)

    def test_history_embed_is_optional_and_main_status_shows_uptime(self) -> None:
        server = self.servers[0]
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        uptime = UptimeSummary(
            successful_count=24,
            online_count=23,
            uptime_percent=95.8,
            coverage_percent=100.0,
            monitoring_since=now - timedelta(hours=24),
            online_since=now - timedelta(hours=3),
            streak_is_lower_bound=False,
        )
        summaries = {server.id: uptime}

        without_history = build_dashboard(
            [server],
            self.samples,
            GuildSettings(layout='per_server'),
            summaries,
            now=now,
        )
        with_history = build_dashboard(
            [server],
            self.samples,
            GuildSettings(layout='per_server', history_enabled=True),
            summaries,
            now=now,
        )

        self.assertEqual({'server:1'}, set(without_history))
        self.assertEqual({'server:1', 'history'}, set(with_history))
        status_embed = with_history['server:1']
        streak_field = next(field for field in status_embed.fields if field.name == '⏱ Online streak')
        self.assertIn('3h', streak_field.value)
        uptime_field = next(field for field in status_embed.fields if field.name == '📈 Uptime · 24 hours')
        self.assertIn('95.8%', uptime_field.value)

    def test_empty_combined_dashboard_is_valid(self) -> None:
        embeds = build_dashboard([], {}, GuildSettings())

        self.assertEqual({'combined'}, set(embeds))
        self.assertEqual('No Minecraft servers configured.', embeds['combined'].description)

    def test_history_embed_in_combined_layout(self) -> None:
        embeds = build_dashboard(
            self.servers[:2],
            self.samples,
            GuildSettings(layout='combined', history_enabled=True),
        )

        self.assertIn('combined', embeds)
        self.assertIn('history', embeds)
        self.assertEqual(2, len(embeds['history'].fields))

    def test_low_coverage_is_inconclusive_without_positive_bar(self) -> None:
        server = self.servers[0]
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        summary = UptimeSummary(
            successful_count=1,
            online_count=1,
            uptime_percent=100.0,
            coverage_percent=25.0,
            monitoring_since=now - timedelta(hours=3),
            online_since=now - timedelta(hours=3),
            streak_is_lower_bound=True,
        )

        embed = build_dashboard(
            [server],
            self.samples,
            GuildSettings(layout='per_server'),
            {server.id: summary},
            now=now,
        )['server:1']
        uptime_field = next(field for field in embed.fields if field.name == '📈 Uptime · 24 hours')

        self.assertIn('Inconclusive', uptime_field.value)
        self.assertIn('tracking 3h', uptime_field.value)
        self.assertNotIn('■■', uptime_field.value)


if __name__ == '__main__':
    unittest.main()