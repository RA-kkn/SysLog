import unittest
from unittest.mock import patch, Mock


class CompressionTests(unittest.TestCase):
    def test_plan_is_codec_only(self):
        import compression
        sql=compression.plan()
        self.assertEqual(sql.count('MODIFY COLUMN'),20)
        self.assertEqual(sql.count('ZSTD(9)'),20)
        self.assertEqual(sql.count('Delta'),2)
        self.assertNotIn('OPTIMIZE',sql)
        self.assertNotIn('MODIFY TTL',sql)

    def test_backup_failure_prevents_alter(self):
        import compression
        client=Mock()
        with patch.object(compression,'get_client',return_value=client), \
             patch.object(compression,'validate'), \
             patch.object(compression,'snapshot',side_effect=FileExistsError):
            with self.assertRaises(FileExistsError):
                compression.apply('already-exists.json')
        client.command.assert_not_called()
