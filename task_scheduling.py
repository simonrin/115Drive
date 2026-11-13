"""Bounded fair lanes and adaptive polling for independent cloud-file tasks."""
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import hashlib
import threading
import time


_WORK = ContextVar('sync_metadata_work', default=('normal', None))
_PRIORITIES = {'interactive': 0, 'normal': 1, 'background': 2}


@contextmanager
def work_priority(kind, account=None):
    """Classify work without acquiring a lock or holding a scheduling lane.

    Priority applies at the next metadata request boundary. Existing in-flight
    requests are not interrupted; nested contexts restore the previous value.
    """
    kind = {'mutation': 'normal', 'user': 'normal'}.get(kind, kind)
    if kind not in _PRIORITIES: raise ValueError('Unknown metadata work priority')
    previous = _WORK.get()
    token = _WORK.set((kind, previous[1] if account is None else account))
    try: yield
    finally: _WORK.reset(token)


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


class MetadataGate(FairGate):
    """One bounded metadata lane, priority + FIFO + aging, across accounts.

    Waiting never holds another lane. Every task in a provider account uses
    the same opaque account key for metrics. Admission remains globally
    bounded so an extra account cannot bypass the existing request budget.
    """
    def __init__(self, capacity=1, *, clock=time.monotonic, max_wait_seconds=5, max_accounts=32):
        super().__init__(capacity)
        if max_wait_seconds <= 0 or max_accounts < 1: raise ValueError('Invalid metadata queue limits')
        self.clock, self.max_wait_seconds, self.max_accounts = clock, max_wait_seconds, max_accounts
        self.sequence = 0
        self.metrics = dict(acquired=0, cancelled=0, timed_out=0, wait_seconds=0.0,
                            max_wait_seconds=0.0, slot_seconds=0.0)
        self.accounts = {}

    def _next(self, now):
        # An aged request reaches the highest class. Sequence breaks all ties,
        # so a continuous stream of new foreground work cannot starve it.
        return min(self.waiters, key=lambda item: (
            0 if now - item['queued_at'] >= self.max_wait_seconds else _PRIORITIES[item['kind']],
            item['sequence'])) if self.waiters else None

    def _account(self, key, now):
        if key not in self.accounts:
            waiting = {item['account'] for item in self.waiters}
            candidates = [(value['last_seen'], account) for account, value in self.accounts.items()
                          if not value['active'] and account not in waiting]
            if len(self.accounts) >= self.max_accounts and candidates:
                del self.accounts[min(candidates)[1]]
            # There are at most active + waiting accounts if the fixed-size
            # history is full. Unbounded completed-account histories are pruned.
            self.accounts[key] = dict(acquired=0, cancelled=0, timed_out=0, active=0,
                                      wait_seconds=0.0, slot_seconds=0.0, last_seen=now)
        self.accounts[key]['last_seen'] = now
        return self.accounts[key]

    def _prune_accounts(self):
        waiting = {item['account'] for item in self.waiters}
        candidates = sorted((value['last_seen'], account) for account, value in self.accounts.items()
                            if not value['active'] and account not in waiting)
        for _, key in candidates:
            if len(self.accounts) <= self.max_accounts: break
            del self.accounts[key]

    @contextmanager
    def slot(self, cancelled=None, *, account=None, timeout=None):
        kind, context_account = _WORK.get()
        identity = context_account if account is None else account
        # Never expose credentials, raw account IDs, root paths or file names.
        key = hashlib.sha256(('metadata:' + str(identity or 'unassigned')).encode()).hexdigest()[:24]
        queued = self.clock()
        if timeout is not None and timeout < 0: raise ValueError('Negative metadata timeout')
        acquired, entered = False, None
        with self.condition:
            self.sequence += 1
            token = dict(sequence=self.sequence, queued_at=queued, kind=kind, account=key)
            self.waiters.append(token)
            try:
                while True:
                    now = self.clock()
                    if cancelled is not None and cancelled.is_set():
                        self.metrics['cancelled'] += 1
                        self._account(key, now)['cancelled'] += 1
                        raise InterruptedError('任务已暂停。')
                    if self.active < self.capacity and self._next(now) is token:
                        self.waiters.remove(token)
                        self.active += 1
                        self.peak_active = max(self.peak_active, self.active)
                        acquired, entered = True, now
                        elapsed = max(0.0, now - queued)
                        self.metrics['acquired'] += 1
                        self.metrics['wait_seconds'] += elapsed
                        self.metrics['max_wait_seconds'] = max(self.metrics['max_wait_seconds'], elapsed)
                        row = self._account(key, now)
                        row['active'] += 1; row['acquired'] += 1; row['wait_seconds'] += elapsed
                        self.condition.notify_all()
                        break
                    if timeout is not None and now - queued >= timeout:
                        self.metrics['timed_out'] += 1
                        self._account(key, now)['timed_out'] += 1
                        raise TimeoutError('等待元数据请求超时。')
                    self.condition.wait(.1)
            finally:
                if not acquired:
                    self.waiters.remove(token)
                    self._prune_accounts()
                    self.condition.notify_all()
        try:
            yield
        finally:
            with self.condition:
                now = self.clock()
                elapsed = max(0.0, now - entered)
                self.metrics['slot_seconds'] += elapsed
                row = self._account(key, now)
                row['slot_seconds'] += elapsed; row['active'] -= 1
                self.active -= 1
                self._prune_accounts()
                self.condition.notify_all()

    def snapshot(self):
        """In-memory aggregate only; observation never triggers a disk write."""
        with self.condition:
            now = self.clock()
            return dict(self.metrics, active=self.active, queued=len(self.waiters), peak_active=self.peak_active,
                        oldest_wait_seconds=max((max(0.0, now-item['queued_at']) for item in self.waiters), default=0.0),
                        accounts={key: {name: value for name, value in row.items() if name != 'last_seen'}
                                  for key, row in self.accounts.items()})


# A long transfer owns only its own lane, never the metadata lane.
METADATA = MetadataGate()
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


class AccountChecks:
    """One ordinary scan round per account; transfers never hold a round open.

    Workers notify scanned() before reconciliation/transfer, then finished().
    Busy workers are excluded from the next round. A local change can issue a
    separate urgent scan without restarting the other account directories.
    """
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.accounts = {}
        self.sequence = 0
        self.metrics = dict(batches=0, tasks=0, largest_batch=0)

    def configure(self, tasks):
        members = {}
        for task in tasks:
            if not task['paused']:
                members.setdefault(task['account'], set()).add(task['id'])
        with self.lock:
            for account, ids in members.items():
                group = self.accounts.setdefault(account, dict(members=set(), busy={}, retry={},
                    round=None, schedule=IdleSchedule('0'*32, clock=self.clock)))
                group['members'] = ids
            for account, group in self.accounts.items():
                group['members'] = members.get(account, set())
                current = group['round']
                if current:
                    current['pending'].intersection_update(group['members'])
                    self._settle(group)

    def _settle(self, group):
        current = group['round']
        if current and not current['pending']:
            group['schedule'].complete(current['changed'])
            group['round'] = None

    def take(self, account, task_id, cancelled, *, urgent=False):
        with self.lock:
            if cancelled.is_set(): raise InterruptedError('任务已暂停。')
            group = self.accounts.get(account)
            if not group or task_id not in group['members'] or task_id in group['busy']:
                return None
            if self.clock() < group['retry'].get(task_id, 0): return None
            current = group['round']
            if current and task_id in current['issued']:
                if not urgent: return None
                # The ordinary round remains intact while one changed task
                # reacts promptly. Its completed sibling scans are not repeated.
                self.sequence += 1
                batch = CheckBatch(self.sequence, clock=self.clock)
            else:
                if current is None:
                    if not urgent and not group['schedule'].due(): return None
                    self.sequence += 1
                    pending = {t for t in group['members'] if t not in group['busy']
                               and self.clock() >= group['retry'].get(t, 0)}
                    current = group['round'] = dict(batch=CheckBatch(self.sequence, clock=self.clock),
                        pending=pending, issued=set(), changed=False)
                    self.metrics['batches'] += 1
                current['pending'].add(task_id)
                current['issued'].add(task_id)
                batch = current['batch']
                self.metrics['largest_batch'] = max(self.metrics['largest_batch'], len(current['issued']))
            group['busy'][task_id] = batch
            self.metrics['tasks'] += 1
            return batch

    def scanned(self, account, task_id, batch, changed):
        with self.lock:
            group = self.accounts.get(account)
            if not group: return
            current = group['round']
            if current and current['batch'] is batch and task_id in current['pending']:
                current['pending'].discard(task_id)
                current['changed'] |= bool(changed)
                self._settle(group)

    def finished(self, account, task_id, batch, *, changed=False, retry_seconds=0):
        with self.lock:
            group = self.accounts.get(account)
            if not group: return
            if group['busy'].get(task_id) is batch: group['busy'].pop(task_id, None)
            if retry_seconds: group['retry'][task_id] = self.clock() + retry_seconds
            else: group['retry'].pop(task_id, None)
            self.scanned(account, task_id, batch, changed or bool(retry_seconds))

    def unavailable(self, account, task_id, *, retry_seconds=30):
        with self.lock:
            group = self.accounts.get(account)
            if not group: return
            group['busy'].pop(task_id, None)
            group['retry'][task_id] = self.clock() + retry_seconds
            current = group['round']
            if current:
                current['pending'].discard(task_id)
                current['changed'] = True
                self._settle(group)
