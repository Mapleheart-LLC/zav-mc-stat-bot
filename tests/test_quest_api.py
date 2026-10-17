import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer

from quest_api import QUEST_EVENT_PATH, QuestApi
from storage import Store


class QuestApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / 'bot.sqlite3')
        await self.store.initialize()
        await self.store.bind_quest_source('world-main', 101, 202)
        await self.store.set_quest_announcements(101, 'world-main', True)
        self.secret = 'test-secret-for-hmac'
        self.client = TestClient(TestServer(QuestApi(self.store, self.secret).app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temp_dir.cleanup()

    def make_request(self, *, nonce: str | None = None, timestamp: int | None = None) -> tuple[bytes, dict[str, str]]:
        body = json.dumps(
            {
                'schema_version': 1,
                'event_id': str(uuid4()),
                'source_id': 'world-main',
                'event_type': 'quest.completed',
                'team_id': str(uuid4()),
                'team_name': 'Paldea Pals',
                'quest_id': '18446744073709551615',
                'quest_name': 'A very big adventure',
                'occurred_at': datetime.now(timezone.utc).isoformat(),
            },
            separators=(',', ':'),
        ).encode('utf-8')
        timestamp_text = str(timestamp or int(datetime.now(timezone.utc).timestamp()))
        nonce_text = nonce or str(uuid4())
        canonical = b'\n'.join((
            b'POST',
            QUEST_EVENT_PATH.encode('ascii'),
            timestamp_text.encode('ascii'),
            nonce_text.encode('ascii'),
            body,
        ))
        signature = hmac.new(self.secret.encode('utf-8'), canonical, hashlib.sha256).hexdigest()
        return body, {
            'X-Mothers-Ledger-Timestamp': timestamp_text,
            'X-Mothers-Ledger-Nonce': nonce_text,
            'X-Mothers-Ledger-Signature': signature,
            'Content-Type': 'application/json',
        }

    def sign_payload(
        self, path: str, payload: dict[str, object], *, nonce: str | None = None
    ) -> tuple[bytes, dict[str, str]]:
        body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        request_nonce = nonce or str(uuid4())
        canonical = b'\n'.join((
            b'POST', path.encode('ascii'), timestamp.encode('ascii'),
            request_nonce.encode('ascii'), body,
        ))
        signature = hmac.new(self.secret.encode('utf-8'), canonical, hashlib.sha256).hexdigest()
        return body, {
            'X-Mothers-Ledger-Timestamp': timestamp,
            'X-Mothers-Ledger-Nonce': request_nonce,
            'X-Mothers-Ledger-Signature': signature,
            'Content-Type': 'application/json',
        }

    async def test_accepts_signed_event_and_queues_one_announcement(self) -> None:
        body, headers = self.make_request()

        response = await self.client.post(QUEST_EVENT_PATH, data=body, headers=headers)

        self.assertEqual(202, response.status)
        self.assertEqual({'accepted': True, 'duplicate': False}, await response.json())
        pending = await self.store.get_pending_quest_announcements()
        self.assertEqual(1, len(pending))
        self.assertEqual('Paldea Pals', pending[0].team_name)
        self.assertEqual('A very big adventure', pending[0].quest_name)

    async def test_rejects_invalid_signature_and_expired_timestamp(self) -> None:
        body, headers = self.make_request()
        headers['X-Mothers-Ledger-Signature'] = '0' * 64
        response = await self.client.post(QUEST_EVENT_PATH, data=body, headers=headers)
        self.assertEqual(401, response.status)

        body, headers = self.make_request(timestamp=1)
        response = await self.client.post(QUEST_EVENT_PATH, data=body, headers=headers)
        self.assertEqual(401, response.status)

    async def test_replay_nonce_is_rejected_and_duplicate_event_is_idempotent(self) -> None:
        body, headers = self.make_request()
        first = await self.client.post(QUEST_EVENT_PATH, data=body, headers=headers)
        self.assertEqual(202, first.status)
        replay = await self.client.post(QUEST_EVENT_PATH, data=body, headers=headers)
        self.assertEqual(409, replay.status)

        duplicate_body = json.loads(body)
        duplicate_body['event_id'] = json.loads(body)['event_id']
        duplicate = json.dumps(duplicate_body, separators=(',', ':')).encode('utf-8')
        new_nonce = str(uuid4())
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        canonical = b'\n'.join((b'POST', QUEST_EVENT_PATH.encode(), timestamp.encode(), new_nonce.encode(), duplicate))
        signature = hmac.new(self.secret.encode(), canonical, hashlib.sha256).hexdigest()
        duplicate_headers = {
            'X-Mothers-Ledger-Timestamp': timestamp,
            'X-Mothers-Ledger-Nonce': new_nonce,
            'X-Mothers-Ledger-Signature': signature,
            'Content-Type': 'application/json',
        }
        duplicate_response = await self.client.post(
            QUEST_EVENT_PATH, data=duplicate, headers=duplicate_headers
        )
        self.assertEqual(200, duplicate_response.status)
        self.assertEqual({'accepted': True, 'duplicate': True}, await duplicate_response.json())
        self.assertEqual(1, len(await self.store.get_pending_quest_announcements()))

    async def test_unbound_source_is_rejected(self) -> None:
        body, headers = self.make_request()
        payload = json.loads(body)
        payload['source_id'] = 'unbound-world'
        body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        timestamp = headers['X-Mothers-Ledger-Timestamp']
        nonce = headers['X-Mothers-Ledger-Nonce']
        canonical = b'\n'.join((b'POST', QUEST_EVENT_PATH.encode(), timestamp.encode(), nonce.encode(), body))
        headers['X-Mothers-Ledger-Signature'] = hmac.new(
            self.secret.encode(), canonical, hashlib.sha256
        ).hexdigest()

        response = await self.client.post(QUEST_EVENT_PATH, data=body, headers=headers)

        self.assertEqual(403, response.status)
        self.assertEqual([], await self.store.get_pending_quest_announcements())

    async def test_signed_snapshot_updates_team_leaderboard(self) -> None:
        body, headers = self.sign_payload(
            '/api/v1/snapshots',
            {
                'schema_version': 1,
                'event_type': 'team.snapshot',
                'source_id': 'world-main',
                'team_id': str(uuid4()),
                'team_name': 'Paldea Pals',
                'progress_percent': 75,
                'completed_count': 3,
                'total_count': 4,
                'completed_quests': ['First steps', 'Catch a Pokémon', 'Win a battle'],
            },
        )

        response = await self.client.post('/api/v1/snapshots', data=body, headers=headers)

        self.assertEqual(202, response.status)
        leaderboard = await self.store.get_quest_leaderboard('world-main')
        self.assertEqual(1, len(leaderboard))
        self.assertEqual(75, leaderboard[0]['progress_percent'])
        self.assertEqual(3, leaderboard[0]['completed_count'])

    async def test_in_game_link_code_is_consumed_once(self) -> None:
        player_uuid = str(uuid4())
        team_id = str(uuid4())
        code = 'A1B2C3D4'
        await self.store.create_quest_link_code(
            source_id='world-main',
            guild_id=101,
            discord_user_id=303,
            code_hash=hashlib.sha256(code.encode('ascii')).hexdigest(),
            expires_at=datetime.now(timezone.utc).replace(microsecond=0)
                + __import__('datetime').timedelta(minutes=5),
        )
        body, headers = self.sign_payload(
            '/api/v1/links/consume',
            {
                'schema_version': 1,
                'event_type': 'player.link',
                'source_id': 'world-main',
                'code': code,
                'player_uuid': player_uuid,
                'player_name': 'TrainerOne',
                'team_id': team_id,
            },
        )

        response = await self.client.post('/api/v1/links/consume', data=body, headers=headers)
        replay = await self.client.post('/api/v1/links/consume', data=body, headers=headers)

        self.assertEqual(200, response.status)
        self.assertEqual(409, replay.status)
        link = await self.store.get_quest_link('world-main', 101, 303)
        self.assertEqual(player_uuid, link['player_uuid'])
        self.assertEqual(team_id, link['team_id'])


if __name__ == '__main__':
    unittest.main()