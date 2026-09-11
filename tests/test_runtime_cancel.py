from __future__ import annotations

import unittest

from awb.providers.runtime_cancel import (
    active_generations,
    clear_cancel_barrier,
    event_for_current_job,
    generation_finished,
    generation_started,
    request_cancel,
)


class RuntimeCancelTests(unittest.TestCase):
    def tearDown(self):
        while active_generations() > 0:
            generation_finished()
        clear_cancel_barrier()

    def test_global_stop_reaches_unbound_local_generation(self):
        event = event_for_current_job()
        self.assertFalse(event.is_set())
        request_cancel()
        self.assertTrue(event.is_set())

    def test_barrier_cannot_clear_while_model_generation_is_active(self):
        generation_started()
        request_cancel()
        with self.assertRaises(RuntimeError):
            clear_cancel_barrier()
        generation_finished()
        clear_cancel_barrier()
        self.assertFalse(event_for_current_job().is_set())


if __name__ == '__main__':
    unittest.main()
