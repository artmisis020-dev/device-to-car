import threading


class CleanupScheduler:
    def __init__(self, every_n_requests=50):
        self.every_n_requests = every_n_requests
        self._lock = threading.Lock()
        self._counter = 0

    def should_run(self):
        with self._lock:
            self._counter += 1
            return self._counter % self.every_n_requests == 0
