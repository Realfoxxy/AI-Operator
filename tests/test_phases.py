"""Aşamalı mod (bölüm 9.1) — sahte ajanlarla."""

from __future__ import annotations

import asyncio
import json
import unittest

from tests.helpers import Harness


class PhasedModeTests(unittest.TestCase):
    def test_two_phases_until_done(self):
        h = Harness(self, mode="asamali")
        report = asyncio.run(h.orch.run("büyük hedef"))
        self.assertIn("Sahte sentez", report)

        # 2 aşama: ilki iskelet (3 görev), ikincisi done=true ve boş
        plans = h.of("plan")
        self.assertEqual([p["asama"] for p in plans], [1, 2])
        self.assertFalse(plans[0]["plan"]["done"])
        self.assertTrue(plans[1]["plan"]["done"])
        self.assertEqual(plans[1]["plan"]["tasks"], [])
        planner_prompts = [e["istem"] for e in h.of("cagri") if e["rol"] == "planlayıcı"]
        self.assertTrue(planner_prompts[0].startswith("<<ROL:PLANLAYICI>> <<ASAMA:1>>"))
        self.assertTrue(planner_prompts[1].startswith("<<ROL:PLANLAYICI>> <<ASAMA:2>>"))
        self.assertIn("ÖNCEKİ AŞAMANIN SONUÇLARI", planner_prompts[1])
        self.assertIn("ornek/notlar.md", planner_prompts[1])           # dosya ağacı

        # ornek/notlar.md oluştu ve olaylar yayınlandı
        self.assertTrue((h.workspace / "ornek" / "notlar.md").exists())
        self.assertTrue(any("ornek/notlar.md" in e["eklenen"] for e in h.of("dosya_degisikligi")))
        asama = [(e["no"], e["durum"]) for e in h.of("asama")]
        self.assertIn((1, "bitti"), asama)
        self.assertIn((2, "bitti"), asama)

        # Yazma içeren aşama için onay istendi; boş aşama için istenmedi
        plan_reqs = [p for k, p in h.approver.requests if k == "plan"]
        self.assertEqual(len(plan_reqs), 1)
        self.assertTrue(plan_reqs[0]["has_write"])

        # Dosyalar ve durum
        run_dir = h.transcript.dir
        for name in ("plan-a1.json", "plan-a2.json", "gorev-a1-t1.md", "sonuc.md", "durum.json"):
            self.assertTrue((run_dir / name).exists(), name)
        state = json.loads((run_dir / "durum.json").read_text(encoding="utf-8"))
        self.assertEqual(state["durum"], "bitti")
        self.assertEqual(len(state["asamalar"]), 2)
        self.assertTrue((h.workspace / ".orkestra" / "proje_hafizasi.md").exists())

    def test_max_phases_marks_incomplete(self):
        h = Harness(self, mode="asamali", max_phases=1)
        asyncio.run(h.orch.run("büyük hedef"))
        synth = [e["istem"] for e in h.of("cagri") if e["rol"] == "sentez"][0]
        self.assertIn("hedef TAMAMLANMADI", synth)

    def test_write_plan_rejected_cancels(self):
        from orkestra.approvals import StaticApprover
        from orkestra.engine import RunCancelled
        h = Harness(self, mode="asamali", approver=StaticApprover(plan="ret"))
        with self.assertRaises(RunCancelled):
            asyncio.run(h.orch.run("büyük hedef"))
        self.assertFalse((h.workspace / "ornek" / "notlar.md").exists())


if __name__ == "__main__":
    unittest.main()
