"""Bounded fair lanes and adaptive polling for independent cloud-file tasks."""
from collections import deque
from contextlib import contextmanager
import copy
import threading
import time


class FairGate:
    """FIFO admission; waiters are cancellable and do not occupy another lane."""
    def __init__(self, capacity=1):
        if capacity < 1: raise ValueError('Positive capacity required')
        self.capacity = capacity
        self.condition = threading.Condition()
        self.waiters = deque()
        self.active = 0
        self.peak_active = 0

    @contextmanager
    def slot(self, cancelled=None):
        token = object()
        acquired = False
        with self.condition:
            self.waiters.append(token)
            try:
                while True:
                    if cancelled is not None and cancelled.is_set():
                        raise InterruptedError('任务已暂停。')
                    if self.waiters[0] is token and self.active < self.capacity:
                        self.waiters.popleft()
                        self.active += 1
                        self.peak_active = max(self.peak_active, self.active)
                        acquired = True
                        self.condition.notify_all()
                        break
                    self.condition.wait(.1)
            finally:
                if not acquired:
                    self.waiters.remove(token)
                    self.condition.notify_all()
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()


# A long transfer owns only its own lane, never the metadata lane.
METADATA = FairGate()
TRANSFERS = {'upload': FairGate(), 'background': FairGate(), 'interactive': FairGate(2)}


class CheckBatch:
    """Short-lived read sharing. Never used to authorize a mutation or download."""
    def __init__(self, number, *, clock=time.monotonic, max_age=5, max_entries=128, max_items=4096):
        self.number, self.clock, self.max_age = number, clock, max_age
        self.max_entries, self.max_items = max_entries, max_items
        self.lock = threading.Lock()
        self.entries = {}
        self.metrics = dict(loads=0, hits=0, waits=0)

    def _prune(self):
        # A slow transfer may retain a batch object; bound completed read copies.
        for key, entry in list(self.entries.items()):
            if entry['ready'].is_set() and self.clock()-entry['finished'] >= self.max_age:
                del self.entries[key]
        completed = [(key, entry) for key, entry in self.entries.items() if entry['ready'].is_set()]
        count = sum(entry.get('weight', 0) for _, entry in completed)
        for key, entry in completed:
            if len(self.entries) <= self.max_entries and count <= self.max_items: break
            del self.entries[key]
            count -= entry.get('weight', 0)

    def read(self, key, loader, cancelled=None):
        if cancelled is not None and cancelled.is_set(): raise InterruptedError('任务已暂停。')
        with self.lock:
            self._prune()
            entry = self.entries.get(key)
            owner = entry is None
            if owner:
                entry = dict(ready=threading.Event(), finished=None)
                self.entries[key] = entry
                self.metrics['loads'] += 1
            else:
                self.metrics['hits' if entry['ready'].is_set() else 'waits'] += 1
        if owner:
            try: entry['value'] = copy.deepcopy(loader())
            except BaseException as error:
                entry['error'] = error
                with self.lock:
                    if self.entries.get(key) is entry: del self.entries[key]
            finally:
                with self.lock:
                    value = entry.get('value')
                    entry['weight'] = len(value) if isinstance(value, (dict, list, tuple)) else 1
                    entry['finished'] = self.clock()
                    entry['ready'].set()
                    self._prune()
        else:
            while not entry['ready'].wait(.1):
                if cancelled is not None and cancelled.is_set(): raise InterruptedError('任务已暂停。')
        if cancelled is not None and cancelled.is_set(): raise InterruptedError('任务已暂停。')
        if 'error' in entry: raise entry['error']
        return copy.deepcopy(entry['value'])


class BatchCoordinator:
    """Collect nearby due tasks without a completion barrier between workers."""
    def __init__(self, *, window=2, clock=time.monotonic):
        self.window, self.clock = window, clock
        self.condition = threading.Condition()
        self.pending = {}
        self.sequence = 0
        self.metrics = dict(batches=0, tasks=0, largest_batch=0)

    def join(self, account, task_id, cancelled, *, urgent=False):
        with self.condition:
            if cancelled.is_set(): raise InterruptedError('任务已暂停。')
            group = self.pending.get(account)
            # Expired batches are closed before a late arrival joins a new one.
            if group and self.clock() >= group['deadline']:
                self._release(account, group)
                group = None
            if group is None:
                self.sequence += 1
                group = dict(batch=CheckBatch(self.sequence, clock=self.clock),
                             deadline=self.clock()+self.window, members=set(), released=False)
                self.pending[account] = group
            token = object()
            group['members'].add(token)
            if urgent: group['deadline'] = self.clock()
            self.condition.notify_all()
            try:
                while True:
                    if cancelled.is_set(): raise InterruptedError('任务已暂停。')
                    if not group['released'] and self.clock() >= group['deadline']:
                        self._release(account, group)
                    if group['released']: return group['batch']
                    self.condition.wait(min(.1, max(0, group['deadline']-self.clock())))
            finally:
                group['members'].discard(token)
                if not group['members'] and self.pending.get(account) is group:
                    del self.pending[account]
                self.condition.notify_all()

    def _release(self, account, group):
        group['released'] = True
        if self.pending.get(account) is group: del self.pending[account]
        self.metrics['batches'] += 1
        self.metrics['tasks'] += len(group['members'])
        self.metrics['largest_batch'] = max(self.metrics['largest_batch'], len(group['members']))
        self.condition.notify_all()


class IdleSchedule:
    def __init__(self, task_id, *, clock=time.monotonic):
        self.clock = clock
        self.offset = int(task_id[:8], 16) / 0xffffffff
        self.idle_rounds = 0
        self.failures = 0
        self.deadline = 0.0
        self.local_deadline = None
        self.last_interval = 0.0

    def local_change(self):
        if self.local_deadline is None and not self.failures:
            self.local_deadline = self.clock() + 1  # Coalesce a burst of notifications.

    def due(self, *, settings_changed=False):
        now = self.clock()
        if self.failures: return now >= self.deadline
        return settings_changed or now >= self.deadline or (
            self.local_deadline is not None and now >= self.local_deadline)

    def complete(self, changed):
        self.failures = 0
        self.idle_rounds = 0 if changed else min(3, self.idle_rounds + 1)
        delay = (15, 30, 60, 120)[self.idle_rounds]
        # Earlier jitter staggers tasks without exceeding the 120-second ceiling.
        self.last_interval = delay - min(3, delay * .1) * self.offset
        self.deadline = self.clock() + self.last_interval
        self.local_deadline = None

    def failed(self):
        self.failures += 1
        self.last_interval = min(300, 15 * 2**min(self.failures - 1, 5))
        self.deadline = self.clock() + self.last_interval
        self.local_deadline = None
