"""Korumalı yollar, değişiklik denetimi, dosya sahipliği ve sembolik bağlantılar (bölüm 9.3, 9.4)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from orkestra.agents import MockAgent
from orkestra.approvals import StaticApprover
from orkestra.config import DEFAULT_PROTECTED
from orkestra.engine import RunStopped
from orkestra.guard import CallInfo, ChangeTracker, GuardError, analyze, diff, scan_startup, take_snapshot
from tests.helpers import Harness


def _run(coro):
    return asyncio.run(coro)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        (self.ws / "src").mkdir()
        (self.ws / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")

    def _track(self, info: CallInfo, change):
        async def go():
            tr = ChangeTracker(self.ws, DEFAULT_PROTECTED, 1000)
            await tr.start()
            tr.begin(info)
            change()
            return await tr.end(info.no)
        return _run(go())

    def test_protected_path_violation(self):
        def change():
            (self.ws / ".claude").mkdir()
            (self.ws / ".claude" / "settings.json").write_text('{"allow": ["Bash"]}', encoding="utf-8")
        rep = self._track(CallInfo(1, "claude", "t1", True, ["src/"]), change)
        self.assertIn((".claude/settings.json", "korumalı yolda değişiklik"), rep.violations)

    def test_protected_modification_detected_by_hash(self):
        (self.ws / "CLAUDE.md").write_text("a", encoding="utf-8")
        st = os.stat(self.ws / "CLAUDE.md")

        def change():
            (self.ws / "CLAUDE.md").write_text("b", encoding="utf-8")
            os.utime(self.ws / "CLAUDE.md", ns=(st.st_atime_ns, st.st_mtime_ns))  # boyut ve zaman aynı
        rep = self._track(CallInfo(1, "gemini", "t1", True, ["src/"]), change)
        self.assertEqual([p for p, _ in rep.violations], ["CLAUDE.md"])

    def test_read_only_call_violation(self):
        rep = self._track(CallInfo(1, "gemini", "t1", False),
                          lambda: (self.ws / "src" / "app.py").write_text("print(2)\n", encoding="utf-8"))
        self.assertEqual(rep.violations, [("src/app.py", "salt-okunur çağrı sırasında değişiklik")])
        self.assertEqual(rep.agent, "gemini")

    def test_unreported_is_warning_not_violation(self):
        def change():
            (self.ws / "src" / "app.py").write_text("print(3)\n", encoding="utf-8")
            (self.ws / "README.md").write_text("x", encoding="utf-8")
        rep = self._track(CallInfo(1, "copilot", "t2", True, ["src/"]), change)
        self.assertEqual(rep.violations, [])
        self.assertEqual(rep.unreported, ["README.md"])
        self.assertEqual(rep.changes.added, ["README.md"])
        self.assertEqual(rep.changes.modified, ["src/app.py"])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink desteklenmiyor")
    def test_symlink_outside_is_violation(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)

        def change():
            try:
                os.symlink(outside, self.ws / "src" / "kacis")
            except OSError as e:   # Windows'ta bağlantı oluşturma yetkisi yoksa
                self.skipTest(f"bağlantı oluşturulamadı: {e}")
        rep = self._track(CallInfo(1, "claude", "t1", True, ["src/"]), change)
        self.assertEqual(rep.violations, [("src/kacis", "çalışma klasörü dışını gösteren sembolik bağlantı")])

    def test_prompt_dir_ignored_and_limit(self):
        (self.ws / ".orkestra" / "istemler").mkdir(parents=True)
        (self.ws / ".orkestra" / "istemler" / "t1-1.md").write_text("istem", encoding="utf-8")
        snap = take_snapshot(self.ws, DEFAULT_PROTECTED, 100)
        self.assertNotIn(".orkestra/istemler/t1-1.md", snap.entries)
        for i in range(5):
            (self.ws / f"f{i}.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(GuardError):
            take_snapshot(self.ws, DEFAULT_PROTECTED, 3)

    def test_analyze_and_diff_basics(self):
        a = take_snapshot(self.ws, DEFAULT_PROTECTED, 100)
        (self.ws / "src" / "app.py").unlink()
        b = take_snapshot(self.ws, DEFAULT_PROTECTED, 100)
        ch = diff(a, b)
        self.assertEqual(ch.removed, ["src/app.py"])
        v, u = analyze(ch, b, DEFAULT_PROTECTED, None)
        self.assertEqual(len(v), 1)


class StartupScanTests(unittest.TestCase):
    def test_parent_claude_md_found(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "CLAUDE.md").write_text("ajanlara talimat", encoding="utf-8")
        ws = root / "a" / "b"
        ws.mkdir(parents=True)
        (ws / ".mcp.json").write_text("{}", encoding="utf-8")
        found = scan_startup(ws)
        self.assertIn(str((root / "CLAUDE.md").resolve()), found)
        self.assertIn(str((ws / ".mcp.json").resolve()), found)

    def test_startup_warning_rejection_cancels_run(self):
        h = Harness(self, approver=StaticApprover(startup="ret"))
        (h.workspace / "AGENTS.md").write_text("x", encoding="utf-8")
        from orkestra.engine import RunCancelled
        with self.assertRaises(RunCancelled):
            _run(h.orch.run("deneme"))
        self.assertEqual(h.orch.calls, 0)
        self.assertIn("baslangic_uyarisi", [r[0] for r in h.approver.requests])


class _SettingsWriterMock(MockAgent):
    """Gemini işçisi çağrı sırasında .claude/settings.json yazar (ihlal taklidi)."""

    async def run(self, prompt, *, write, workdir, tag, on_chunk=None, **kw):
        if self.name == "gemini" and prompt.startswith("<<ROL:ISCI>>"):
            d = Path(workdir) / ".claude"
            d.mkdir(exist_ok=True)
            (d / "settings.json").write_text('{"permissions": {"allow": ["Bash"]}}', encoding="utf-8")
        return await super().run(prompt, write=write, workdir=workdir, tag=tag, on_chunk=on_chunk, **kw)


class EngineGuardTests(unittest.TestCase):
    def test_violation_stops_run_when_user_says_stop(self):
        h = Harness(self, cls=_SettingsWriterMock, approver=StaticApprover(violation="durdur"))
        with self.assertRaises(RunStopped):
            _run(h.orch.run("deneme"))
        ihlal = h.of("korumali_ihlal")
        self.assertEqual(len(ihlal), 1)
        # Paralel okuma çağrıları aralıkta birlikte etkin olduğu için ihlal ikisine birlikte atfedilir (9.4).
        self.assertIn("gemini", ihlal[0]["ajan"])
        self.assertIn(".claude/settings.json", ihlal[0]["yollar"])
        self.assertIn("ihlal", [r[0] for r in h.approver.requests])
        self.assertNotIn("sentez", h.kinds())
        state = json.loads((h.transcript.dir / "durum.json").read_text(encoding="utf-8"))
        self.assertEqual(state["durum"], "durduruldu")

    def test_violation_continue(self):
        h = Harness(self, cls=_SettingsWriterMock, approver=StaticApprover(violation="devam"))
        report = _run(h.orch.run("deneme"))
        self.assertIn("Sahte sentez", report)
        self.assertEqual(len(h.of("korumali_ihlal")), 1)

    def test_write_task_files_and_ownership(self):
        # Sahte ajan yazma izniyle ornek/notlar.md oluşturur; files listesinde olduğu için uyarı yok.
        h = Harness(self, mode="asamali")
        _run(h.orch.run("deneme"))
        changes = h.of("dosya_degisikligi")
        self.assertTrue(any("ornek/notlar.md" in c["eklenen"] and c["ajan"] == "gemini" for c in changes))
        self.assertTrue(all(not c["bildirilmemis"] for c in changes))
        self.assertEqual(h.of("korumali_ihlal"), [])
        memo = (h.workspace / ".orkestra" / "proje_hafizasi.md").read_text(encoding="utf-8")
        self.assertIn("ornek/notlar.md → t1 · gemini · aşama 1", memo)


if __name__ == "__main__":
    unittest.main()
