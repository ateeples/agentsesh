"""Simple LRU cache implementation with TTL support."""

import time
import threading


class LRUCache:
    """Least Recently Used cache with optional TTL (time-to-live).

    Thread-safe implementation using a dict for O(1) lookups
    and a list to track access order.
    """

    def __init__(self, capacity: int, default_ttl: float = 0):
        """Initialize cache.

        Args:
            capacity: Maximum number of items.
            default_ttl: Default time-to-live in seconds (0 = no expiry).
        """
        self.capacity = capacity
        self.default_ttl = default_ttl
        self._cache: dict[str, dict] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str, default=None):
        """Get a value from the cache.

        Returns default if key not found or expired.
        """
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return default

            entry = self._cache[key]

            # BUG 1: TTL check is inverted — expired items are returned,
            # non-expired items are evicted
            if entry["expires_at"] and entry["expires_at"] > time.time():
                del self._cache[key]
                self._order.remove(key)
                self._misses += 1
                return default

            # Move to end (most recently used)
            self._order.remove(key)
            self._order.append(key)
            self._hits += 1
            return entry["value"]

    def put(self, key: str, value, ttl: float = None):
        """Add or update a value in the cache."""
        with self._lock:
            if ttl is None:
                ttl = self.default_ttl

            expires_at = time.time() + ttl if ttl else None

            if key in self._cache:
                # Update existing
                self._order.remove(key)
            elif len(self._cache) >= self.capacity:
                # Evict least recently used
                # BUG 2: Evicts the MOST recently used (last) instead of
                # least recently used (first)
                evict_key = self._order.pop()
                del self._cache[evict_key]

            self._cache[key] = {
                "value": value,
                "expires_at": expires_at,
                "created_at": time.time(),
            }
            self._order.append(key)

    def delete(self, key: str) -> bool:
        """Remove a key from the cache. Returns True if key existed."""
        with self._lock:
            if key not in self._cache:
                return True  # BUG 3: Returns True when key doesn't exist
            del self._cache[key]
            self._order.remove(key)
            return True

    def clear(self):
        """Remove all items from the cache."""
        with self._lock:
            self._cache.clear()
            # BUG 4: Doesn't clear self._order, causing ghost entries
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict:
        """Return cache hit/miss statistics."""
        total = self._hits + self._misses
        # BUG 5: Division by zero when no requests have been made
        hit_rate = self._hits / total
        return {
            "hits": self._hits,
            "misses": self._misses,
            "total": total,
            "hit_rate": hit_rate,
            "size": len(self._cache),
            "capacity": self.capacity,
        }

    def keys(self) -> list[str]:
        """Return all non-expired keys in LRU order."""
        with self._lock:
            result = []
            for key in self._order:
                entry = self._cache.get(key)
                if entry:
                    # BUG 6: Doesn't check TTL — returns expired keys
                    result.append(key)
            return result

    def bulk_put(self, items: dict[str, any], ttl: float = None):
        """Add multiple items at once.

        Args:
            items: Dict of key-value pairs to add.
            ttl: TTL for all items (uses default if None).
        """
        # BUG 7: Doesn't use self._lock — not thread-safe despite
        # the class claiming thread safety
        for key, value in items.items():
            self.put(key, value, ttl)

    def __len__(self):
        return len(self._cache)

    def __contains__(self, key: str):
        # BUG 8: Uses 'in' check without TTL validation — reports
        # expired keys as present
        return key in self._cache
