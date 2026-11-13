import json
import threading
import time
import unittest

from task_scheduling import MetadataGate, work_priority, METADATA, TRANSFERS


def wait(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline: raise TimeoutError('worker did not reach queue')
        threading.Event().wait(.005)


class PriorityTests(unittest.TestCase):
    def enqueue(self, gate, kinds, *, account='one'):
        order, errors, threads = [], [], []
        for index, kind in enumerate(kinds):
            def run(number=index, priority=kind):
                try:
                    with work_priority(priority, account), gate.slot(): order.append(number)
                except BaseException as error: errors.append(error)
            worker = threading.Thread(target=run); worker.start(); threads.append(worker)
            wait(lambda:len(gate.waiters) == len(threads))
        return order, errors, threads

    def finish(self, threads, errors):
        for worker in threads:
            worker.join(2); self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_interactive_then_mutation_then_background_at_request_boundaries(self):
        gate = MetadataGate(max_wait_seconds=60)
        with gate.slot():
            order, errors, threads = self.enqueue(gate, ['background','normal','interactive'])
            self.assertEqual(order, [])  # Cannot preempt a request already running.
        self.finish(threads, errors)
        self.assertEqual(order, [2,1,0])

    def test_equal_priority_requests_keep_fifo_even_between_accounts(self):
        gate = MetadataGate(max_wait_seconds=60)
        with gate.slot():
            order, errors, threads = self.enqueue(gate, ['normal']*4)
        self.finish(threads, errors)
        self.assertEqual(order, [0,1,2,3])

    def test_aged_background_runs_before_new_interactive_work(self):
        now = [0.0]; gate = MetadataGate(clock=lambda:now[0], max_wait_seconds=5)
        with gate.slot():
            order, errors, threads = self.enqueue(gate, ['background','interactive','normal'])
            now[0] = 6.0
        self.finish(threads, errors)
        self.assertEqual(order, [0,1,2])
        self.assertEqual(gate.snapshot()['max_wait_seconds'], 6.0)

    def test_cancel_and_timeout_remove_waiters_and_do_not_leak_capacity(self):
        gate = MetadataGate(); cancel = threading.Event(); results = []
        def run(stop, timeout):
            try:
                with gate.slot(stop, timeout=timeout): results.append('unexpected')
            except (InterruptedError, TimeoutError) as error: results.append(type(error).__name__)
        with gate.slot():
            one = threading.Thread(target=run,args=(cancel,None)); one.start()
            wait(lambda:len(gate.waiters)==1)
            two = threading.Thread(target=run,args=(None,.05)); two.start()
            wait(lambda:len(gate.waiters)==2)
            cancel.set(); one.join(1); two.join(1)
            self.assertEqual(gate.active,1); self.assertEqual(len(gate.waiters),0)
        self.assertCountEqual(results,['InterruptedError','TimeoutError'])
        with gate.slot(): self.assertEqual(gate.active,1)
        self.assertEqual(gate.active,0)
        self.assertEqual((gate.snapshot()['cancelled'],gate.snapshot()['timed_out']),(1,1))

    def test_exception_releases_active_slot_and_records_occupancy(self):
        now = [0.0]; gate = MetadataGate(clock=lambda:now[0])
        with self.assertRaises(ConnectionError):
            with gate.slot(account='private-account'):
                now[0] = 7
                raise ConnectionError('remote timeout')
        self.assertEqual(gate.active,0)
        self.assertEqual(gate.snapshot()['slot_seconds'],7)
        self.assertNotIn('private-account',json.dumps(gate.snapshot()))

    def test_nested_priority_context_restores_after_failure_and_does_not_cross_threads(self):
        gate = MetadataGate(max_wait_seconds=60); snapshots=[]
        with gate.slot():
            def run():
                with work_priority('background','account'):
                    try:
                        with work_priority('interactive'): raise ValueError('test')
                    except ValueError: pass
                    with gate.slot(): pass
            with work_priority('interactive','parent-account'):
                worker = threading.Thread(target=run); worker.start()
                wait(lambda:len(gate.waiters)==1)
                snapshots.append(gate.waiters[0]['kind'])
        worker.join(1)
        self.assertEqual(snapshots,['background'])
        with self.assertRaises(ValueError):
            with work_priority('unknown'): pass

    def test_account_metrics_group_same_identity_and_bound_retained_history(self):
        gate = MetadataGate(max_accounts=2)
        with work_priority('normal','same-account'):
            with gate.slot(): pass
            with gate.slot(): pass
        rows = gate.snapshot()['accounts']
        self.assertEqual(len(rows),1)
        self.assertEqual(next(iter(rows.values()))['acquired'],2)
        for number in range(10):
            with gate.slot(account=str(number)): pass
        self.assertEqual(len(gate.snapshot()['accounts']),2)
        self.assertEqual(gate.snapshot()['acquired'],12)

    def test_existing_transfer_lanes_and_metadata_capacity_are_independent(self):
        with TRANSFERS['upload'].slot(), TRANSFERS['background'].slot():
            with work_priority('interactive','test'), METADATA.slot():
                self.assertEqual(METADATA.active,1)
        self.assertEqual(METADATA.capacity,1)
        self.assertEqual(METADATA.active,0)


if __name__ == '__main__': unittest.main()
