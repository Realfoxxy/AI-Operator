"""Korumalı yollar ve değişiklik denetimi (bölüm 9.4, kural 16–18).

Bölümler:
  1. Yol yardımcıları
  2. Anlık görüntü (take_snapshot) ve fark (diff)
  3. Değişiklik analizi (analyze)
  4. İzleyici (ChangeTracker): değişiklikleri çağrı aralıklarına atfeder
  5. Başlangıç taraması ve dosya ağacı
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .security import SecurityError


class GuardError(SecurityError):
    """Anlık görüntü alınamadı (ör. çok fazla dosya)."""


PROMPT_DIR = ".orkestra/istemler"   # Orkestra'nın geçici istem dosyaları; snapshot dışı
# Dosya sistemi saat çözünürlüğü (Windows'ta ~15 ms) aynı boyutta hızlı bir değişikliği gizleyebilir.
# Son RECENT_S saniyede değişmiş, HASH_LIMIT'ten küçük dosyaların içerik özeti de alınır.
RECENT_S = 5.0
HASH_LIMIT = 4 * 1024 * 1024
MAX_EVENT_PATHS = 200


# ====================================================================== 1. Yol yardımcıları

def is_protected(rel: str, protected) -> bool:
    """rel (göreli POSIX yol) korumalı bir yolun kendisi veya altında mı? (büyük/küçük harfe duyarsız)"""
    r = rel.lower()
    for p in protected:
        p = p.lower().rstrip("/")
        if r == p or r.startswith(p + "/"):
            return True
    return False


def covered_by(rel: str, files) -> bool:
    """rel, görevin files listesindeki bir girişle kapsanıyor mu?"""
    r = rel.lower()
    for f in files:
        fl = f.lower()
        if fl.endswith("/"):
            if r.startswith(fl):
                return True
        elif r == fl:
            return True
    return False


# ====================================================================== 2. Snapshot ve diff

@dataclass(frozen=True)
class Entry:
    kind: str                 # "f" dosya | "l" sembolik bağlantı
    size: int = 0
    mtime_ns: int = 0
    sha256: str | None = None
    target: str | None = None


@dataclass
class Snapshot:
    root: Path
    entries: dict[str, Entry] = field(default_factory=dict)


@dataclass
class ChangeSet:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def all(self) -> list[str]:
        return self.added + self.modified + self.removed

    def __bool__(self) -> bool:
        return bool(self.added or self.modified or self.removed)


def _sha256(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def stat_entry(root: Path, rel: str, protected) -> Entry | None:
    """Tek bir yolun kaydını üretir (bağlantılar izlenmez). Yol yoksa None."""
    p = root / rel
    try:
        st = os.lstat(p)
    except OSError:
        return None
    if os.path.islink(p):
        try:
            target = os.readlink(p)
        except OSError:
            target = ""
        return Entry("l", target=target)
    if not os.path.isfile(p):
        return None
    recent = (time.time_ns() - st.st_mtime_ns) < RECENT_S * 1e9 and st.st_size <= HASH_LIMIT
    sha = _sha256(p) if is_protected(rel, protected) or recent else None
    return Entry("f", st.st_size, st.st_mtime_ns, sha)


def _changed(a: Entry, b: Entry) -> bool:
    """Özet yalnızca iki tarafta da varsa karşılaştırılır (korumalı yollarda her zaman vardır)."""
    if (a.kind, a.size, a.mtime_ns, a.target) != (b.kind, b.size, b.mtime_ns, b.target):
        return True
    return a.sha256 is not None and b.sha256 is not None and a.sha256 != b.sha256


def take_snapshot(workspace: Path, protected, limit: int) -> Snapshot:
    root = Path(workspace)
    snap = Snapshot(root)
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for d in dirnames:
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if rel.lower() == PROMPT_DIR:
                continue
            if os.path.islink(os.path.join(dirpath, d)):
                filenames.append(d)          # klasöre bağlantı: izlenmez, bağlantı olarak kaydedilir
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            entry = stat_entry(root, rel, protected)
            if entry is None:
                continue
            count += 1
            if count > limit:
                raise GuardError(
                    f"Çalışma klasöründe {limit} dosyadan fazlası var; değişiklik denetimi yapılamıyor. "
                    f"Daha dar bir çalışma klasörü seç veya [guard] max_snapshot_files değerini artır."
                )
            snap.entries[rel] = entry
    return snap


def diff(before: Snapshot, after: Snapshot) -> ChangeSet:
    b, a = before.entries, after.entries
    return ChangeSet(
        added=sorted(set(a) - set(b)),
        modified=sorted(k for k in set(a) & set(b) if _changed(a[k], b[k])),
        removed=sorted(set(b) - set(a)),
    )


# ====================================================================== 3. Analiz

@dataclass
class CallInfo:
    no: int
    agent: str
    task: str
    write: bool
    files: list[str] | None = None   # None: kapsam sınırı yok (Orkestra'nın doğrulama komutları)


@dataclass
class Report:
    changes: ChangeSet
    violations: list[tuple[str, str]]     # (yol, neden)
    unreported: list[str]
    writer: CallInfo | None
    readers: list[CallInfo]

    @property
    def agent(self) -> str:
        if self.writer:
            return self.writer.agent
        return ", ".join(sorted({c.agent for c in self.readers})) or "(bilinmiyor)"

    @property
    def task(self) -> str:
        if self.writer:
            return self.writer.task
        return ", ".join(sorted({c.task for c in self.readers})) or "-"


def _link_escapes(root: Path, rel: str, target: str) -> bool:
    base = (root / rel).parent
    try:
        resolved = (base / target).resolve()
        root_r = root.resolve()
    except OSError:
        return True
    return not (resolved == root_r or root_r in resolved.parents)


def analyze(changes: ChangeSet, after: Snapshot, protected, writer: CallInfo | None) -> tuple[list, list]:
    """(ihlaller, bildirilmemiş) döndürür."""
    violations: list[tuple[str, str]] = []
    unreported: list[str] = []
    for rel in changes.all:
        if is_protected(rel, protected):
            violations.append((rel, "korumalı yolda değişiklik"))
            continue
        entry = after.entries.get(rel)
        if entry is not None and entry.kind == "l":
            if rel in changes.added or rel in changes.modified:
                reason = ("çalışma klasörü dışını gösteren sembolik bağlantı"
                          if _link_escapes(after.root, rel, entry.target or "") else "yeni sembolik bağlantı")
                violations.append((rel, reason))
                continue
        if writer is None:
            violations.append((rel, "salt-okunur çağrı sırasında değişiklik"))
        elif writer.files is not None and not covered_by(rel, writer.files):
            unreported.append(rel)
    return violations, unreported


# ====================================================================== 4. İzleyici

class ChangeTracker:
    """Son bilinen snapshot'ı tutar; her çağrı bitiminde yeni snapshot alıp farkı atfeder.

    Bir aralıkta bir yazma çağrısı etkin olduysa (write_lock nedeniyle en fazla bir tane),
    korumalı yollar dışındaki değişiklikler ona atfedilir. Hiç yazma çağrısı yoksa her
    değişiklik ihlaldir ve aralıktaki okuma çağrılarına birlikte atfedilir.
    """

    def __init__(self, workspace: Path, protected, limit: int):
        self.workspace = Path(workspace)
        self.protected = list(protected)
        self.limit = limit
        self.last: Snapshot | None = None
        self._lock = asyncio.Lock()
        self._active: dict[int, CallInfo] = {}
        self._interval: dict[int, CallInfo] = {}

    async def start(self) -> None:
        async with self._lock:
            self.last = await asyncio.to_thread(take_snapshot, self.workspace, self.protected, self.limit)

    def begin(self, info: CallInfo) -> None:
        self._active[info.no] = info
        self._interval[info.no] = info

    async def end(self, no: int) -> Report:
        async with self._lock:
            try:
                after = await asyncio.to_thread(take_snapshot, self.workspace, self.protected, self.limit)
            finally:
                self._active.pop(no, None)
            changes = diff(self.last, after)
            calls = list(self._interval.values())
            writer = next((c for c in calls if c.write), None)
            readers = [c for c in calls if not c.write]
            violations, unreported = analyze(changes, after, self.protected, writer)
            self.last = after
            self._interval = dict(self._active)   # hâlâ süren çağrılar sonraki aralıkta da etkin
            return Report(changes, violations, unreported, writer, readers)

    async def accept_own_write(self, rel: str) -> None:
        """Orkestra'nın kendi yazdığı dosyayı (proje hafızası) son bilinen snapshot'a işler."""
        async with self._lock:
            if self.last is None:
                return
            entry = stat_entry(self.workspace, rel, self.protected)
            if entry is None:
                self.last.entries.pop(rel, None)
            else:
                self.last.entries[rel] = entry

    def tree(self, max_entries: int = 400) -> str:
        """Dosya ağacı: yalnızca göreli yol ve boyut, korumalı yollar hariç."""
        if self.last is None:
            return "(dosya ağacı yok)"
        rows = []
        for rel in sorted(self.last.entries):
            if is_protected(rel, self.protected):
                continue
            e = self.last.entries[rel]
            rows.append(f"{rel} (bağlantı)" if e.kind == "l" else f"{rel} ({e.size} B)")
        if not rows:
            return "(çalışma klasörü boş)"
        extra = len(rows) - max_entries
        out = "\n".join(rows[:max_entries])
        return out + (f"\n… ve {extra} giriş daha" if extra > 0 else "")


# ====================================================================== 5. Başlangıç taraması

WORKSPACE_SCAN = ["CLAUDE.md", "GEMINI.md", "AGENTS.md", ".github/copilot-instructions.md",
                  ".claude", ".gemini", ".mcp.json"]
PARENT_SCAN = ["CLAUDE.md", "GEMINI.md", "AGENTS.md"]


def scan_startup(workspace: Path) -> list[str]:
    """Ajanları yönlendirebilecek talimat/konfig dosyalarını bulur (kural 17)."""
    ws = Path(workspace).resolve()
    found: list[str] = []
    for name in WORKSPACE_SCAN:
        p = ws / name
        if os.path.lexists(p):
            found.append(str(p))
    for parent in ws.parents:
        for name in PARENT_SCAN:
            p = parent / name
            if os.path.lexists(p):
                found.append(str(p))
    return found
