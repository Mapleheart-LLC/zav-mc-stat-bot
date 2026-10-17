from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from aiohttp import web

from storage import Store


QUEST_EVENT_PATH = '/api/v1/events'
QUEST_SNAPSHOT_PATH = '/api/v1/snapshots'
QUEST_LINK_PATH = '/api/v1/links/consume'
SIGNED_PATHS = {QUEST_EVENT_PATH, QUEST_SNAPSHOT_PATH, QUEST_LINK_PATH}
MAX_CLOCK_SKEW_SECONDS = 300
SOURCE_ID_PATTERN = re.compile(r'^[A-Za-z0-9._-]{1,128}$')
LINK_CODE_PATTERN = re.compile(r'^[A-F0-9]{8}$')


class QuestApi:
    def __init__(self, store: Store, secret: str) -> None:
        self.store = store
        self.secret = secret.encode('utf-8')
        self.app = web.Application(client_max_size=64 * 1024)
        self.app.router.add_get('/health', self.health)
        self.app.router.add_post(QUEST_EVENT_PATH, self.receive_completion)
        self.app.router.add_post(QUEST_SNAPSHOT_PATH, self.receive_snapshot)
        self.app.router.add_post(QUEST_LINK_PATH, self.consume_link)

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({'status': 'ok', 'schema_version': 1})

    async def receive_completion(self, request: web.Request) -> web.Response:
        body, nonce = await self._authenticated_body(request)
        payload = self._parse_payload(body)
        result = await self.store.receive_quest_completion(
            **payload,
            nonce=nonce,
            received_at=datetime.now(timezone.utc),
        )
        if result == 'unbound':
            raise web.HTTPForbidden(text='Unknown quest source')
        if result == 'replay':
            raise web.HTTPConflict(text='Request nonce has already been used')
        return web.json_response(
            {'accepted': True, 'duplicate': result == 'duplicate'},
            status=200 if result == 'duplicate' else 202,
        )

    async def receive_snapshot(self, request: web.Request) -> web.Response:
        body, nonce = await self._authenticated_body(request)
        payload = self._parse_snapshot(body)
        now = datetime.now(timezone.utc)
        if not await self.store.register_quest_nonce(nonce, now=now):
            raise web.HTTPConflict(text='Request nonce has already been used')
        saved = await self.store.save_quest_team_snapshot(**payload, synced_at=now)
        if not saved:
            raise web.HTTPForbidden(text='Unknown quest source')
        return web.json_response({'accepted': True}, status=202)

    async def consume_link(self, request: web.Request) -> web.Response:
        body, nonce = await self._authenticated_body(request)
        payload = self._parse_link(body)
        now = datetime.now(timezone.utc)
        if not await self.store.register_quest_nonce(nonce, now=now):
            raise web.HTTPConflict(text='Request nonce has already been used')
        code_hash = hashlib.sha256(payload.pop('code').encode('ascii')).hexdigest()
        linked = await self.store.consume_quest_link_code(
            **payload,
            code_hash=code_hash,
            now=now,
        )
        if not linked:
            raise web.HTTPForbidden(text='Link code is invalid, expired, or already used')
        return web.json_response({'linked': True}, status=200)

    async def _authenticated_body(self, request: web.Request) -> tuple[bytes, str]:
        if not self.secret:
            raise web.HTTPServiceUnavailable(text='Quest integration is not configured')
        if request.path not in SIGNED_PATHS or request.query_string:
            raise web.HTTPBadRequest(text='Unsupported request route')

        body = await request.read()
        timestamp = request.headers.get('X-Mothers-Ledger-Timestamp', '')
        nonce = request.headers.get('X-Mothers-Ledger-Nonce', '')
        supplied_signature = request.headers.get('X-Mothers-Ledger-Signature', '')
        if not timestamp.isascii() or not timestamp.isdigit() or not nonce or len(nonce) > 80:
            raise web.HTTPUnauthorized(text='Missing or invalid signature headers')
        try:
            nonce = str(UUID(nonce))
            sent_at = int(timestamp)
        except (ValueError, OverflowError):
            raise web.HTTPUnauthorized(text='Missing or invalid signature headers') from None

        now_seconds = int(datetime.now(timezone.utc).timestamp())
        if abs(now_seconds - sent_at) > MAX_CLOCK_SKEW_SECONDS:
            raise web.HTTPUnauthorized(text='Request timestamp is outside the accepted window')

        canonical = b'\n'.join((
            request.method.encode('ascii'),
            request.path.encode('ascii'),
            timestamp.encode('ascii'),
            nonce.encode('ascii'),
            body,
        ))
        expected_signature = hmac.new(self.secret, canonical, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_signature, supplied_signature.lower()):
            raise web.HTTPUnauthorized(text='Invalid request signature')
        return body, nonce

    @staticmethod
    def _parse_payload(body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text='Body must be valid UTF-8 JSON') from None
        if not isinstance(payload, dict) or payload.get('schema_version') != 1:
            raise web.HTTPBadRequest(text='Unsupported event schema')
        if payload.get('event_type') != 'quest.completed':
            raise web.HTTPBadRequest(text='Unsupported event type')

        source_id = payload.get('source_id')
        if not isinstance(source_id, str) or not SOURCE_ID_PATTERN.fullmatch(source_id):
            raise web.HTTPBadRequest(text='Invalid source_id')
        try:
            event_id = str(UUID(payload.get('event_id', '')))
            team_id = str(UUID(payload.get('team_id', '')))
        except (ValueError, AttributeError, TypeError):
            raise web.HTTPBadRequest(text='event_id and team_id must be UUIDs') from None

        team_name = QuestApi._required_text(payload, 'team_name', 128)
        quest_id = QuestApi._required_text(payload, 'quest_id', 64)
        quest_name = QuestApi._required_text(payload, 'quest_name', 256)
        occurred_at_text = QuestApi._required_text(payload, 'occurred_at', 64)
        try:
            occurred_at = datetime.fromisoformat(occurred_at_text.replace('Z', '+00:00'))
            if occurred_at.tzinfo is None:
                raise ValueError('timestamp has no timezone')
            occurred_at = occurred_at.astimezone(timezone.utc)
        except ValueError:
            raise web.HTTPBadRequest(text='occurred_at must be an ISO-8601 timestamp with timezone') from None

        return {
            'source_id': source_id,
            'event_id': event_id,
            'team_id': team_id,
            'team_name': team_name,
            'quest_id': quest_id,
            'quest_name': quest_name,
            'occurred_at': occurred_at,
        }

    @staticmethod
    def _parse_snapshot(body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text='Body must be valid UTF-8 JSON') from None
        if not isinstance(payload, dict) or payload.get('schema_version') != 1:
            raise web.HTTPBadRequest(text='Unsupported snapshot schema')
        if payload.get('event_type') != 'team.snapshot':
            raise web.HTTPBadRequest(text='Unsupported snapshot event type')
        source_id = payload.get('source_id')
        if not isinstance(source_id, str) or not SOURCE_ID_PATTERN.fullmatch(source_id):
            raise web.HTTPBadRequest(text='Invalid source_id')
        try:
            team_id = str(UUID(payload.get('team_id', '')))
        except (ValueError, AttributeError, TypeError):
            raise web.HTTPBadRequest(text='team_id must be a UUID') from None
        for name in ('progress_percent', 'completed_count', 'total_count'):
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise web.HTTPBadRequest(text=f'{name} must be a non-negative integer')
        if payload['progress_percent'] > 100:
            raise web.HTTPBadRequest(text='progress_percent must not exceed 100')
        completed_quests = payload.get('completed_quests')
        if (
            not isinstance(completed_quests, list)
            or len(completed_quests) > 100
            or any(not isinstance(name, str) or not name.strip() or len(name) > 256 for name in completed_quests)
        ):
            raise web.HTTPBadRequest(text='completed_quests must contain at most 100 quest names')
        return {
            'source_id': source_id,
            'team_id': team_id,
            'team_name': QuestApi._required_text(payload, 'team_name', 128),
            'progress_percent': payload['progress_percent'],
            'completed_count': payload['completed_count'],
            'total_count': payload['total_count'],
            'completed_quests': [name.strip() for name in completed_quests],
        }

    @staticmethod
    def _parse_link(body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text='Body must be valid UTF-8 JSON') from None
        if not isinstance(payload, dict) or payload.get('schema_version') != 1:
            raise web.HTTPBadRequest(text='Unsupported link schema')
        if payload.get('event_type') != 'player.link':
            raise web.HTTPBadRequest(text='Unsupported link event type')
        source_id = payload.get('source_id')
        if not isinstance(source_id, str) or not SOURCE_ID_PATTERN.fullmatch(source_id):
            raise web.HTTPBadRequest(text='Invalid source_id')
        try:
            player_uuid = str(UUID(payload.get('player_uuid', '')))
            team_id = str(UUID(payload.get('team_id', '')))
        except (ValueError, AttributeError, TypeError):
            raise web.HTTPBadRequest(text='player_uuid and team_id must be UUIDs') from None
        code = QuestApi._required_text(payload, 'code', 8).upper()
        if not LINK_CODE_PATTERN.fullmatch(code):
            raise web.HTTPBadRequest(text='code must be 8 hexadecimal characters')
        return {
            'source_id': source_id,
            'player_uuid': player_uuid,
            'team_id': team_id,
            'player_name': QuestApi._required_text(payload, 'player_name', 64),
            'code': code,
        }

    @staticmethod
    def _required_text(payload: dict[str, Any], name: str, max_length: int) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > max_length:
            raise web.HTTPBadRequest(text=f'{name} must be a non-empty string up to {max_length} characters')
        return value.strip()