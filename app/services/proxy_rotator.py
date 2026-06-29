import random
import threading
from pathlib import Path


class ProxyRotator:
    def __init__(self, path: Path, *, strategy: str = "round_robin") -> None:
        self.path = path
        self.strategy = strategy
        self._lock = threading.Lock()
        self._index = 0
        self._mtime: float | None = None
        self._proxies: list[str] = []

    def _reload_if_needed(self) -> None:
        if not self.path.exists():
            self._mtime = None
            self._proxies = []
            return

        mtime = self.path.stat().st_mtime
        if self._mtime == mtime:
            return

        self._mtime = mtime
        self._proxies = [
            line.strip()
            for line in self.path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self._index = 0

    def next_proxy(self) -> str | None:
        with self._lock:
            self._reload_if_needed()
            if not self._proxies:
                return None
            if self.strategy == "random":
                return random.choice(self._proxies)
            proxy = self._proxies[self._index % len(self._proxies)]
            self._index += 1
            return proxy


proxy_rotator = ProxyRotator(Path("secrets/proxy_list.txt"))
