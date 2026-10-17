import unittest

from status import InvalidStatusResponse, parse_status_response


class ParseStatusResponseTests(unittest.TestCase):
    def test_parses_online_response_without_exposing_player_names(self) -> None:
        status = parse_status_response(
            {
                'online': True,
                'players': {'online': 3, 'max': 20, 'list': ['not rendered']},
                'version': '1.20.1',
                'motd': {'clean': ['A friendly server', 'Welcome!']},
            }
        )

        self.assertTrue(status.online)
        self.assertEqual(3, status.players_online)
        self.assertEqual(20, status.players_max)
        self.assertEqual('1.20.1', status.version)
        self.assertEqual('A friendly server Welcome!', status.motd)
        self.assertFalse(hasattr(status, 'players'))

    def test_offline_response_has_zero_players(self) -> None:
        status = parse_status_response({'online': False})

        self.assertFalse(status.online)
        self.assertEqual(0, status.players_online)
        self.assertEqual(0, status.players_max)

    def test_partial_online_response_uses_unknown_values(self) -> None:
        status = parse_status_response({'online': True, 'players': 'invalid'})

        self.assertIsNone(status.players_online)
        self.assertIsNone(status.players_max)
        self.assertEqual('Unknown', status.version)
        self.assertEqual('', status.motd)

    def test_invalid_online_state_is_not_treated_as_offline(self) -> None:
        for payload in ({}, {'online': 'false'}, None):
            with self.subTest(payload=payload):
                with self.assertRaises(InvalidStatusResponse):
                    parse_status_response(payload)


if __name__ == '__main__':
    unittest.main()