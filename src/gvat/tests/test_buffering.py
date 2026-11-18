import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gvat_local.buffering import buffer_plan, memory_size


class BufferingTests(unittest.TestCase):
    def workspace(self):
        return SimpleNamespace(video_indexes={'video': {'pts': list(range(900)), 'time_base': [1, 30]}},
                               buffer_seconds=15, buffer_memory=None)

    def test_4k_buffer_can_span_several_gigabytes(self):
        frames = [{'width': 3840, 'height': 2160}] * 900
        with patch('gvat_local.buffering.available_memory', return_value=64 * 1024 ** 3):
            plan = buffer_plan(self.workspace(), frames, 1)
        self.assertEqual(plan['max_bytes'], 3840 * 2160 * 4 * 30 * 15)
        self.assertEqual(plan['max_chunks'], 450)
        self.assertFalse(plan['capped'])

    def test_buffer_reserves_half_available_ram_and_honors_a_smaller_cap(self):
        frames = [{'width': 3840, 'height': 2160}] * 900
        workspace = self.workspace()
        with patch('gvat_local.buffering.available_memory', return_value=8 * 1024 ** 3):
            plan = buffer_plan(workspace, frames, 1)
            self.assertEqual(plan['max_bytes'], 4 * 1024 ** 3)
            self.assertTrue(plan['capped'])
            workspace.buffer_memory = memory_size('2GiB')
            self.assertEqual(buffer_plan(workspace, frames, 1)['max_bytes'], 2 * 1024 ** 3)

    def test_memory_size_rejects_invalid_values(self):
        self.assertEqual(memory_size('1.5GiB'), 1610612736)
        for value in ('0', '-1GiB', 'lots', 'NaN'):
            with self.assertRaises(ValueError):
                memory_size(value)
