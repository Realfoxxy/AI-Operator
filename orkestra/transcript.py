"""Çalıştırma kaydı: runs/<zaman>/kayit.jsonl (bölüm 6.6)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .security import make_private_dir, redact


def _redact_value(v):
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, dict):
        return {k: _redact_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_redact_value(x) for x in v]
    return v


class Transcript:
    """Çalıştırma kaydı. Sürüm 0.2'de bir EventBus abonesidir: bus.add_listener(transcript.on_event)."""

    def __init__(self, runs_dir: Path, *, existing: Path | None = None):
        if existing is not None:
            # 'devam': aynı klasördeki kayit.jsonl dosyasına eklenir.
            self.dir = Path(existing)
            self._fh = open(self.dir / "kayit.jsonl", "a", encoding="utf-8")
            return
        root = make_private_dir(Path(runs_dir))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = root / stamp
        n = 2
        while True:
            try:
                path.mkdir()  # exist_ok=False: çakışmada sonraki ek denenir
                break
            except FileExistsError:
                path = root / f"{stamp}-{n}"
                n += 1
        make_private_dir(path)
        self.dir = path
        self._fh = open(path / "kayit.jsonl", "a", encoding="utf-8")

    def event(self, kind: str, **data) -> None:
        if self._fh is None:
            return
        row = {"zaman": datetime.now().isoformat(timespec="seconds"), "tur": kind}
        row.update({k: _redact_value(v) for k, v in data.items()})
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fh.flush()

    def on_event(self, event: dict, persist: bool) -> None:
        """EventBus aboneliği. persist=False olaylar (canlı akış parçaları) yazılmaz."""
        if not persist or self._fh is None:
            return
        row = {"zaman": event.get("zaman") or datetime.now().isoformat(timespec="seconds"),
               "tur": event["tur"], "id": event.get("id")}
        row.update({k: _redact_value(v) for k, v in event.items() if k not in ("zaman", "tur", "id")})
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fh.flush()

    def save(self, name: str, text: str) -> Path:
        p = self.dir / name
        p.write_text(redact(text or ""), encoding="utf-8")
        return p

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
