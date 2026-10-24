"""The store's single SQLite connection is shared by the event loop and executor threads.
Run: python tests/test_store_threads.py"""

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.store import Store  # noqa: E402


def test_concurrent_writes_from_threads():
    with tempfile.TemporaryDirectory() as d:
        s = Store(Path(d) / "hub.db")
        errors = []

        def work(n):
            try:
                for i in range(200):
                    s.add_history(f"sensor.t{n}", "temp", 20 + i % 5, time.time() + i)
                    s.set(f"k{n}", i)
                    s.get(f"k{n}")
            except Exception as err:   # "cannot start a transaction within a transaction" before the lock
                errors.append(err)
        ts = [threading.Thread(target=work, args=(n,)) for n in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors, errors[:3]
        assert s.get("k0") == 199 and len(s.history("sensor.t0", "temp", 0)) > 0
        s.close()


if __name__ == "__main__":
    test_concurrent_writes_from_threads()
    print("ok test_concurrent_writes_from_threads")
