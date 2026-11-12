"""Kontrol noktaları: çalışma klasörünün anlık kopyaları, geri alma ve fark (sürüm 0.3).

Tasarım (en basit ve en güvenli seçenek):
- Kullanıcının kendi .git deposuna DOKUNULMAZ. Anlık kopyalar, çalışma klasörünün DIŞINDA
  (runs_dir/_kontrol/<klasör özeti>.git) ayrı bir git deposunda tutulur; --git-dir/--work-tree ile çalışılır.
- Git her zaman liste argümanlarla, kabuksuz çalıştırılır. Sistem/kullanıcı git config'i okunmaz,
  kancalar boş bir klasöre yönlendirilir: ajanların yazdığı dosyalar Orkestra'nın git çağrıları
  sırasında kod çalıştıramaz.
- .orkestra/ (Orkestra'nın iç dosyaları) kopyalara dahil edilmez; çalışma klasöründeki .git zaten
  git tarafından hiçbir zaman eklenmez.
- Geri almadan önce o anki durum da bir kontrol noktası olarak kaydedilir: geri alma da geri alınabilir.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .security import SecurityError, check_argv, make_private_dir

GIT_TIMEOUT_S = 180
MAX_DIFF_BYTES = 400 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class CheckpointError(Exception):
    """Kontrol noktası işlemi başarısız."""


@dataclass
class Checkpoint:
    sha: str
    zaman: int        # unix saniye
    etiket: str


class CheckpointStore:
    def __init__(self, workspace: Path, runs_dir: Path):
        self.workspace = Path(workspace).resolve()
        key = hashlib.sha256(str(self.workspace).encode("utf-8")).hexdigest()[:16]
        self.root = Path(runs_dir) / "_kontrol"
        self.git_dir = self.root / f"{key}.git"
        self.hooks = self.root / "bos-kancalar"
        self.git = shutil.which("git")

    @property
    def available(self) -> bool:
        return self.git is not None

    # ---------------------------------------------------------------- düşük seviye

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "HOME",
                                                            "USERPROFILE")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                    "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
        return env

    def _run(self, *args: str, check: bool = True, binary: bool = False, stdin_text: str | None = None) -> subprocess.CompletedProcess:
        if not self.git:
            raise CheckpointError("git bulunamadı; kontrol noktaları kullanılamıyor.")
        argv = [self.git, "--git-dir", str(self.git_dir), "--work-tree", str(self.workspace),
                "-c", f"core.hooksPath={self.hooks}", "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false",
                "-c", "core.quotepath=false", "-c", "commit.gpgsign=false", "-c", "gc.auto=0",
                "-c", "user.name=Orkestra", "-c", "user.email=orkestra@localhost", *args]
        try:
            check_argv(argv)
        except SecurityError as e:
            raise CheckpointError(str(e)) from e
        try:
            io_kw = {"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}
            proc = subprocess.run(argv, cwd=str(self.workspace), capture_output=True, timeout=GIT_TIMEOUT_S,
                                  env=self._env(), text=not binary, **io_kw,
                                  **({"encoding": "utf-8", "errors": "replace"} if not binary else {}))
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CheckpointError(f"git çalıştırılamadı: {e}") from e
        if check and proc.returncode != 0:
            err = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode("utf-8", "replace")
            raise CheckpointError(f"git hatası: {err.strip()[-500:]}")
        return proc

    def _ensure(self) -> None:
        if (self.git_dir / "HEAD").exists():
            return
        make_private_dir(self.root)
        self.hooks.mkdir(exist_ok=True)
        self.git_dir.mkdir(parents=True, exist_ok=True)
        self._run("init", "-q")
        info = self.git_dir / "info"
        info.mkdir(exist_ok=True)
        (info / "exclude").write_text(".orkestra/\n", encoding="utf-8")

    # ---------------------------------------------------------------- işlemler

    def create(self, etiket: str) -> Checkpoint:
        """Çalışma klasörünün o anki halini kaydeder."""
        self._ensure()
        etiket = re.sub(r"[\x00-\x1f]", " ", etiket)[:200] or "kontrol noktası"
        self._run("add", "-A", "--", ".")
        # Etiket plan başlığı (model çıktısı) içerebilir: argv'ye değil stdin'e verilir (kural 2).
        self._run("commit", "-q", "--allow-empty", "--no-verify", "-F", "-", stdin_text=etiket + "\n")
        sha = self._run("rev-parse", "HEAD").stdout.strip()
        return Checkpoint(sha=sha, zaman=int(self._run("log", "-1", "--format=%ct").stdout.strip() or 0), etiket=etiket)

    def list(self, limit: int = 50) -> list[Checkpoint]:
        if not (self.available and (self.git_dir / "HEAD").exists()):
            return []
        proc = self._run("log", f"-n{limit}", "--format=%H%x09%ct%x09%s", check=False)
        out = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and _SHA_RE.match(parts[0]):
                out.append(Checkpoint(parts[0], int(parts[1]), parts[2]))
        return out

    def _verify_sha(self, sha: str) -> str:
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise CheckpointError("Geçersiz kontrol noktası kimliği.")
        full = self._run("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False).stdout.strip()
        if not full:
            raise CheckpointError("Kontrol noktası bulunamadı.")
        return full

    def changes_since(self, sha: str) -> list[tuple[str, str]]:
        """Kontrol noktasından bu yana değişen dosyalar: [(durum, yol)] — A eklendi, M değişti, D silindi."""
        self._ensure()
        full = self._verify_sha(sha)
        self._run("add", "-A", "--", ".")          # yalnızca bu deponun dizini güncellenir
        out = self._run("diff", "--cached", "--name-status", "--no-renames", full).stdout
        rows = []
        for line in out.splitlines():
            st, _, path = line.partition("\t")
            if st and path:
                rows.append((st[0], path))
        return rows

    def restore(self, sha: str) -> tuple[Checkpoint, list[tuple[str, str]]]:
        """Çalışma klasörünü kontrol noktasına döndürür. Önce o anki hal kaydedilir (geri alınabilir)."""
        full = self._verify_sha(sha)
        before = self.create(f"geri almadan önce (→ {full[:8]})")
        changes = self._run("diff", "--name-status", "--no-renames", before.sha, full).stdout
        # Dizin ve çalışma ağacı hedefe getirilir; hedefte olmayan izlenen dosyalar silinir.
        self._run("read-tree", "-u", "--reset", full)
        self.create(f"geri alındı: {full[:8]}")
        rows = [(line[0], line.split("\t", 1)[1]) for line in changes.splitlines() if "\t" in line]
        return before, rows

    def file_at(self, sha: str, rel: str) -> bytes | None:
        full = self._verify_sha(sha)
        proc = self._run("show", f"{full}:{rel}", check=False, binary=True)
        return proc.stdout if proc.returncode == 0 else None

    def diff(self, sha: str, rel: str) -> str:
        """Kontrol noktasındaki hal ile şu anki dosya arasındaki birleşik fark (unified diff)."""
        old_b = self.file_at(sha, rel) or b""
        p = self.workspace / rel
        try:
            new_b = p.read_bytes() if p.is_file() and not p.is_symlink() else b""
        except OSError:
            new_b = b""
        if len(old_b) > MAX_DIFF_BYTES or len(new_b) > MAX_DIFF_BYTES:
            return "(dosya fark göstermek için çok büyük)"
        if b"\x00" in old_b[:8192] or b"\x00" in new_b[:8192]:
            return "(ikili dosya; fark gösterilemez)"
        old = old_b.decode("utf-8", "replace").splitlines(keepends=True)
        new = new_b.decode("utf-8", "replace").splitlines(keepends=True)
        text = "".join(difflib.unified_diff(old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}"))
        return text or "(fark yok)"
