from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import quote

if TYPE_CHECKING:
    import aiohttp


class InvalidStatusResponse(ValueError):
    pass


@dataclass(frozen=True)
class ServerStatus:
    online: bool
    players_online: Optional[int]
    players_max: Optional[int]
    version: str
    motd: str


def _nonnegative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def parse_status_response(response: Any) -> ServerStatus:
    if not isinstance(response, dict) or not isinstance(response.get('online'), bool):
        raise InvalidStatusResponse('response must contain a boolean online field')

    if not response['online']:
        return ServerStatus(False, 0, 0, 'Unknown', '')

    players = response.get('players')
    players = players if isinstance(players, dict) else {}
    version = response.get('version')
    version = version.strip() if isinstance(version, str) and version.strip() else 'Unknown'

    motd_data = response.get('motd')
    clean_motd = motd_data.get('clean') if isinstance(motd_data, dict) else None
    if isinstance(clean_motd, list):
        motd = ' '.join(part.strip() for part in clean_motd if isinstance(part, str)).strip()
    else:
        motd = ''

    return ServerStatus(
        online=True,
        players_online=_nonnegative_int(players.get('online')),
        players_max=_nonnegative_int(players.get('max')),
        version=version,
        motd=motd,
    )


async def fetch_status(
    session: aiohttp.ClientSession, address: str
) -> ServerStatus:
    encoded_address = quote(address, safe='.:[]-')
    url = f'https://api.mcsrvstat.us/3/{encoded_address}'
    async with session.get(url) as response:
        response.raise_for_status()
        payload = await response.json(content_type=None)
    return parse_status_response(payload)