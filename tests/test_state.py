"""Durum kaydı ve kaldığı yerden devam (bölüm 9.6, kural 26)."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orkestra.config import load_config
from orkestra.engine import BudgetExceeded
from orkestra.state import ProjectMemory, StateError, load_state
from tests.helpers import ROOT, Harness


class ResumeTests(unittest.TestCase):
    def test_budget_then_resume(self):
        first = Harness(self, max_calls=4)
        with self.assertRaises(BudgetExceeded):
            asyncio.run(first.orch.run("deneme"))
        run_dir = first.transcript.dir
        first.transcript.close()
        raw = json.loads((run_dir / "durum.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["surum"], 2)
        self.assertEqual(raw["toplam_cagri"], 4)
        done_before = {tid for tid, r in raw["asamalar"][0]["sonuclar"].items() if r["durum"] == "tamam"}
        self.assertTrue(done_before)                       # en az bir görev tamamlandı
        self.assertNotEqual(done_before, {"t1", "t2", "t3"})   # ama hepsi değil
        self.assertFalse(raw["asamalar"][0]["bitti"])

        cfg = load_config(ROOT / "config.toml")
        cfg.settings.runs_dir = first.tmp / "runs"
        state, plans = load_state(run_dir, cfg)
        second = Harness(self, tmp=first.tmp, state=state, resume_plans=plans, run_dir=run_dir, cfg=cfg)
        report = asyncio.run(second.orch.run(state.hedef))
        self.assertIn("Sahte sentez", report)

        worker_tasks = {e["gorev"] for e in second.of("cagri") if e["rol"] == "işçi"}
        self.assertFalse(worker_tasks & done_before, "tamamlanmış görevler yeniden çalıştı")
        self.assertEqual(second.of("devam")[0]["onceki_calls"], 4)
        final = json.loads((run_dir / "durum.json").read_text(encoding="utf-8"))
        self.assertEqual(final["durum"], "bitti")
        self.assertEqual({r["durum"] for r in final["asamalar"][0]["sonuclar"].values()}, {"tamam"})
        self.assertEqual(final["toplam_cagri"], 4 + second.orch.calls)
        # Aynı kayit.jsonl dosyasına eklenir
        kinds = [json.loads(x)["tur"] for x in (run_dir / "kayit.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn("devam", kinds)
        self.assertTrue((run_dir / "sonuc.md").exists())

    def test_cli_devam(self):
        from orkestra.__main__ import main
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        text = (ROOT / "config.toml").read_text(encoding="utf-8")
        text = text.replace("max_calls = 60", "max_calls = 4")
        cfg_path = tmp / "config.toml"
        cfg_path.write_text(text, encoding="utf-8")
        ws = tmp / "calisma"
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(main(["run", "--mock", "--yes", "--config", str(cfg_path),
                                   "--workspace", str(ws), "deneme"]), 1)
        run_dir = next((tmp / "runs").iterdir())
        cfg_path.write_text(text.replace("max_calls = 4", "max_calls = 60"), encoding="utf-8")
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(main(["devam", str(run_dir), "--mock", "--yes", "--config", str(cfg_path)]), 0)
        self.assertEqual(json.loads((run_dir / "durum.json").read_text(encoding="utf-8"))["durum"], "bitti")


class CorruptStateTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        asyncio.run(self.h.orch.run("deneme"))
        self.run_dir = self.h.transcript.dir
        self.cfg = load_config(ROOT / "config.toml")
        self.cfg.settings.runs_dir = self.h.tmp / "runs"
        self.good = json.loads((self.run_dir / "durum.json").read_text(encoding="utf-8"))

    def _write(self, data) -> None:
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        (self.run_dir / "durum.json").write_text(text, encoding="utf-8")

    def test_good_state_loads(self):
        state, plans = load_state(self.run_dir, self.cfg)
        self.assertEqual(state.hedef, "deneme")
        self.assertEqual(len(plans[0].tasks), 3)

    def test_rejects_corrupt(self):
        cases = []
        cases.append("{bozuk json")
        bad = json.loads(json.dumps(self.good)); bad["surum"] = 99; cases.append(bad)
        bad = json.loads(json.dumps(self.good)); bad["asamalar"][0]["plan"]["tasks"][0]["id"] = "../etc"; cases.append(bad)
        bad = json.loads(json.dumps(self.good)); bad["asamalar"][0]["plan"]["tasks"][0]["assignee"] = "kotu"; cases.append(bad)
        bad = json.loads(json.dumps(self.good)); bad["calisma_klasoru"] = str(Path.home()); cases.append(bad)
        bad = json.loads(json.dumps(self.good)); bad["asamalar"][0]["sonuclar"]["t1"]["durum"] = "harika"; cases.append(bad)
        bad = json.loads(json.dumps(self.good)); bad["asamalar"][0]["sonuclar"]["t9"] = {"durum": "tamam"}; cases.append(bad)
        for case in cases:
            self._write(case)
            with self.assertRaises(StateError):
                load_state(self.run_dir, self.cfg)

    def test_rejects_bad_run_dir_name(self):
        other = self.h.tmp / "runs" / "kotu-ad"
        shutil.copytree(self.run_dir, other)
        with self.assertRaises(StateError):
            load_state(other, self.cfg)


class MemoryTests(unittest.TestCase):
    def test_memory_persists_and_truncates(self):
        h = Harness(self)
        asyncio.run(h.orch.run("ilk hedef"))
        m = ProjectMemory.load(h.workspace)
        self.assertEqual(len(m.goals), 1)
        self.assertEqual(len(m.completed), 3)
        for i in range(200):
            m.add_completed(1, f"t{i}", "Başlık", "x" * 300)
        self.assertLessEqual(len(m.render()), 16000)


if __name__ == "__main__":
    unittest.main()
