import unittest

import bot as bot_module


class BotCommandTests(unittest.TestCase):
    def test_expected_commands_are_registered(self) -> None:
        for name in ('help', 'servers', 'status', 'stats', 'refresh', 'server', 'ip'):
            with self.subTest(command=name):
                self.assertIsNotNone(bot_module.bot.get_command(name))

    def test_server_management_commands_require_operator_check(self) -> None:
        group = bot_module.bot.get_command('server')
        self.assertIsNotNone(group)
        self.assertEqual(
            {'add', 'remove', 'channel', 'layout', 'interval', 'history'},
            {command.name for command in group.commands},
        )
        for command in group.commands:
            with self.subTest(command=command.name):
                self.assertIn(bot_module.can_manage_guild, command.checks)

    def test_status_and_history_are_guild_scoped(self) -> None:
        for name in ('servers', 'status', 'stats', 'refresh', 'server', 'ip'):
            with self.subTest(command=name):
                command = bot_module.bot.get_command(name)
                self.assertTrue(any(getattr(check, '__name__', '') == 'predicate' for check in command.checks))


if __name__ == '__main__':
    unittest.main()