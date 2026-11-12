"""Proje geliştirme terminali ve hesap bağlama (sürüm 0.3)."""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from orkestra import accounts
from orkestra.config import AgentConfig, load_config
from orkestra.security import SecurityError
from orkestra.terminal import TerminalApp
from tests.helpers import ROOT


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "xdg"), "APPDATA": str(self.tmp / "xdg"),
                                               "NO_COLOR": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = load_config(ROOT / "config.toml")
        self.cfg.settings.runs_dir = self.tmp / "runs"
        self.cfg.settings.workspace = self.tmp / "projeler"
        self.out = io.StringIO()
        p = mock.patch("sys.stdout", self.out)
        p.start()
        self.addCleanup(p.stop)


class TerminalTests(_Env):
    def _wait(self, cond, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        self.fail("zaman aşımı; çıktı:\n" + self.out.getvalue()[-2000:])

    def test_project_run_note_and_history(self):
        app = TerminalApp(self.cfg, mock=True, mock_speed=0)
        app.project = app._check_ws(app.project)
        app.handle("/proje demo")
        self.assertEqual(app.project, (self.tmp / "projeler" / "demo").resolve())
        app.handle("/bilinmeyen")
        self.assertIn("Bilinmeyen komut", self.out.getvalue())
        app.handle("/not idle iken not")
        self.assertIn("Aktif çalıştırma yok", self.out.getvalue())
        app.handle("/dogrulama ekle pytest python -m pytest -q")
        self.assertEqual(app.cfg.verify.get("pytest").argv, ["python", "-m", "pytest", "-q"])
        app.handle("/dogrulama sil pytest")
        app.handle("Basit bir not uygulaması")
        self._wait(lambda: app.runner and app.runner.approver.pending)        # yazma içeren plan onay ister
        self.assertIn("dosya YAZACAK", self.out.getvalue())
        app.handle("e")
        self._wait(lambda: app.runner.done.is_set())
        self.assertIn("Sahte sentez", app.runner.report)
        out = self.out.getvalue()
        for frag in ("SONUÇ RAPORU", "▶ t1", "✔ t3 tamam", "Kontrol noktası", "Kullanım:"):
            self.assertIn(frag, out)
        self.assertTrue((app.project / "ornek" / "notlar.md").exists())
        app.handle("/gorevler")
        app.handle("/dosyalar")
        app.handle("/gecmis")
        app.handle("/kontrol")
        app.handle("/fark ornek/notlar.md")
        out = self.out.getvalue()
        self.assertIn("+ ornek/notlar.md", out)
        self.assertIn("başlangıç: Basit bir not uygulaması", out)
        self.assertIn("+[gemini] tarafından yazıldı", out)
        app.handle("/geri-al 1")          # etkileşimsiz: onay alınamaz, hiçbir şey değişmez
        self.assertTrue((app.project / "ornek" / "notlar.md").exists())

    def test_reject_plan_and_settings(self):
        app = TerminalApp(self.cfg, mock=True, mock_speed=0)
        app.project = app._check_ws(app.project)
        app.handle("/butce 2")
        self.assertIn("en az 3", self.out.getvalue())
        app.handle("/butce 30")
        self.assertEqual(app.cfg.settings.max_calls, 30)
        app.handle("/mod tek")
        self.assertEqual(app.cfg.settings.mode, "tek")
        app.handle("/sikimod ac")
        self.assertTrue(app.cfg.settings.strict_guard)
        app.handle("/otomatik ac")
        app.handle("salt okunur hedef")                 # tek mod, yazma yok → otomatik onay
        self._wait(lambda: app.runner.done.is_set())
        self.assertIn("otomatik onaylandı", self.out.getvalue())
        app.handle("/mod asamali")
        app.handle("/otomatik kapat")
        app.handle("ikinci hedef")
        self._wait(lambda: app.runner.approver.pending)
        app.handle("h")
        self._wait(lambda: app.runner.done.is_set())
        self.assertIn("İptal", self.out.getvalue())
        self.assertFalse((app.project / "ornek" / "notlar.md").exists())

    def test_rejects_project_inside_repo(self):
        app = TerminalApp(self.cfg, mock=True, mock_speed=0)
        app.handle(f"/proje {ROOT}")
        self.assertIn("✖", self.out.getvalue())


class AccountTests(_Env):
    def test_tool_mapping_and_rows(self):
        self.assertEqual(accounts.tool_of(AgentConfig("a", "A", ["claude.cmd"])), "claude")
        self.assertEqual(accounts.tool_of(AgentConfig("a", "A", ["/usr/bin/gemini"])), "gemini")
        self.assertIsNone(accounts.tool_of(AgentConfig("a", "A", ["baska"])))
        rows = accounts.rows(self.cfg)
        self.assertEqual([r["ajan"] for r in rows], ["claude", "gemini", "copilot"])
        self.assertTrue(rows[0]["planlayici"])
        self.assertEqual(rows[1]["paket"], "@google/gemini-cli")

    def test_install_rejects_unknown_packages(self):
        with mock.patch("shutil.which", return_value="/usr/bin/npm"):
            with self.assertRaises(SecurityError):
                accounts.install(["kotu-paket; rm -rf /"])

    def test_probe_records_status_without_secrets(self):
        ok_cfg = AgentConfig("deneme", "D", [sys.executable, "-c", "import sys; sys.stdin.read(); print('TAMAM')"])
        ok, msg = accounts.probe_sync(ok_cfg, self.tmp)
        self.assertTrue(ok, msg)
        bad_cfg = AgentConfig("kotu", "K", [sys.executable, "-c", "import sys; print('api_key=gizli123', file=sys.stderr); sys.exit(1)"])
        ok, msg = accounts.probe_sync(bad_cfg, self.tmp)
        self.assertFalse(ok)
        status = accounts.load_status()
        self.assertTrue(status["deneme"]["basarili"])
        self.assertFalse(status["kotu"]["basarili"])
        self.assertNotIn("gizli123", str(status))


if __name__ == "__main__":
    unittest.main()
