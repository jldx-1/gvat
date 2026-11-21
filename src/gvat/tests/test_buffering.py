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
        self.assertEqual(plan['prefetch_chunks'], 449)
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

    def test_small_budget_does_not_prefetch_beyond_cache_capacity(self):
        workspace = self.workspace()
        workspace.buffer_memory = 64 * 48 * 4 * 32
        with patch('gvat_local.buffering.available_memory', return_value=1024 ** 3):
            plan = buffer_plan(workspace, [{'width': 64, 'height': 48}] * 100, 32)
        self.assertEqual((plan['max_chunks'], plan['prefetch_chunks']), (1, 0))

    def test_chunk_size_shrinks_to_the_memory_cap(self):
        workspace = self.workspace()
        frame_bytes = 800 * 600 * 4
        frames = [{'width': 800, 'height': 600}] * 100
        for cap in (frame_bytes // 2, frame_bytes * 2, frame_bytes * 17):
            workspace.buffer_memory = cap
            with self.subTest(cap=cap), patch('gvat_local.buffering.available_memory', return_value=1024 ** 3):
                plan = buffer_plan(workspace, frames, 17)
            self.assertGreaterEqual(plan['chunk_size'], 1)
            decoded_bytes = frame_bytes * plan['chunk_size'] * plan['max_chunks']
            self.assertLessEqual(decoded_bytes, plan['max_bytes'])
            self.assertLessEqual(decoded_bytes, max(frame_bytes, cap))

    def test_low_available_ram_and_short_samples_also_limit_the_chunk_size(self):
        workspace = self.workspace()
        frames = [{'width': 800, 'height': 600}] * 100
        frame_bytes = 800 * 600 * 4
        with patch('gvat_local.buffering.available_memory', return_value=frame_bytes * 4):
            plan = buffer_plan(workspace, frames, 17)
        self.assertEqual((plan['chunk_size'], plan['max_chunks']), (2, 1))
        with patch('gvat_local.buffering.available_memory', return_value=1024 ** 3):
            plan = buffer_plan(workspace, frames[:1], 17)
        self.assertEqual((plan['chunk_size'], plan['max_chunks'], plan['max_bytes']), (1, 1, frame_bytes))
