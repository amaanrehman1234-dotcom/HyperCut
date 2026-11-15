import statistics
import threading
from collections import deque


class SpikeCounter:
    """Counts chat messages per bucket; flags a bucket far above the rolling baseline."""

    def __init__(self, multiplier=2.5, min_msgs=10, cooldown=30, window=30, warmup=3):
        self.multiplier, self.min_msgs, self.cooldown, self.warmup = multiplier, min_msgs, cooldown, warmup
        self.history = deque(maxlen=window)
        self.count = self.last = 0
        self.baseline = 0.0
        self.last_spike = float("-inf")
        self.lock = threading.Lock()

    def add(self, n=1):
        with self.lock:
            self.count += n

    def roll(self, now):
        """Close the current bucket. Returns True if it is a spike."""
        with self.lock:
            n, self.count = self.count, 0
        h = self.history
        self.baseline = statistics.median(h) if h else 0.0
        warm = len(h) >= self.warmup
        h.append(n)
        self.last = n
        hit = warm and n >= max(self.min_msgs, self.multiplier * self.baseline) and now - self.last_spike >= self.cooldown
        if hit:
            self.last_spike = now
        return hit
