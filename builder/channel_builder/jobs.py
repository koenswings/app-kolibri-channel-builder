"""One background build queue (FIFO, one build at a time).

Manual builds from the web form and automatic USB-stick builds share this
queue, so a teacher's build waits its turn ("queued") while another build
runs. Every page shows the queue, so a second teacher sees what is going on.
"""
import itertools
import logging
import threading
import time
from collections import deque

log = logging.getLogger("channel_builder.jobs")

QUEUED, RUNNING, DONE, FAILED, SKIPPED = "queued", "running", "done", "failed", "skipped"
KEEP_FINISHED = 20


class Job(object):
    _ids = itertools.count(1)

    def __init__(self, kind, title, run, form=None, meta=None):
        self.id = str(next(Job._ids))
        self.kind, self.title, self.run = kind, title, run  # run(job) -> result dict
        self.form = dict(form or {})
        self.form.pop("nc_password", None)  # never keep a password
        self.meta = dict(meta or {})
        self.state, self.done, self.total = QUEUED, 0, 0
        self.message, self.result = "", None
        self.created, self.started, self.finished = time.time(), None, None

    def progress(self, done, total):
        self.done, self.total = done, total

    def public(self, position=None):
        d = dict(id=self.id, kind=self.kind, title=self.title, state=self.state,
                 done=self.done, total=self.total, message=self.message)
        if position is not None:
            d["position"] = position
        if self.result:
            d["result"] = {k: self.result.get(k) for k in
                           ("channel_id", "version", "resources", "warnings", "skipped")}
        return d


class BuildQueue(object):
    def __init__(self, lock):
        self.lock = lock  # the global BUILD_LOCK: one build at a time
        self.cv = threading.Condition()
        self.queue = deque()
        self.current = None
        self.finished = deque(maxlen=KEEP_FINISHED)
        self.jobs = {}
        self.listeners = []  # callables(job) on finish
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="build-worker", daemon=True)
            self._thread.start()

    def submit(self, job):
        with self.cv:
            self.jobs[job.id] = job
            self.queue.append(job)
            self.cv.notify_all()
        self.start()
        return job

    def get(self, job_id):
        return self.jobs.get(job_id)

    def position(self, job):
        with self.cv:
            for i, j in enumerate(self.queue):
                if j is job:
                    return i + 1 + (1 if self.current else 0)
        return None

    def snapshot(self):
        with self.cv:
            cur = [self.current.public()] if self.current else []
            off = 1 if self.current else 0
            q = [j.public(i + 1 + off) for i, j in enumerate(self.queue)]
            return dict(running=cur, queued=q, finished=[j.public() for j in reversed(self.finished)])

    def wait(self, job, timeout=None):
        end = None if timeout is None else time.time() + timeout
        with self.cv:
            while job.state in (QUEUED, RUNNING):
                left = None if end is None else end - time.time()
                if left is not None and left <= 0:
                    return False
                self.cv.wait(left)
        return True

    def _worker(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                job = self.queue.popleft()
                self.current = job
                job.state, job.started = RUNNING, time.time()
            try:
                with self.lock:
                    job.result = job.run(job)
                if job.state == RUNNING:
                    job.state = DONE
            except BaseException as e:  # noqa: B902  (SystemExit from Builder = "no files")
                job.state, job.message = FAILED, str(e) or e.__class__.__name__
                log.warning("build %s (%s) failed: %s", job.id, job.title, job.message)
            job.finished = time.time()
            for fn in list(self.listeners):
                try:
                    fn(job)
                except Exception:  # pragma: no cover
                    log.exception("listener failed")
            with self.cv:
                self.current = None
                self.finished.append(job)
                self.cv.notify_all()
