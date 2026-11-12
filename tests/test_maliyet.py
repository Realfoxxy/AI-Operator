"""Token/maliyet ve güvenilirlik düzeltmeleri: dosya yazmayan görev, doğrulama yorumlayıcısı, ipuçları."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

from orkestra.agents import MockAgent
from orkestra.config import VerifyCommand, load_config
from orkestra.engine import verify_hint
from orkestra.verify import VerifyResult, run_verify
from tests.helpers import ROOT, Harness


class _NoWriteMock(MockAgent):
    """Yazma izni verilse bile ilk turda dosya yazmayan, kodu yalnızca metin olarak döndüren ajan."""
    turns = 0

    async def run(self, prompt, *, write, workdir, tag, on_chunk=None, **kw):
        if write and prompt.startswith("<<ROL:ISCI>>"):
            _NoWriteMock.turns += 1
            if _NoWriteMock.turns == 1:
                return "```python\nprint('kod burada ama dosyada değil')\n```"
        return await super().run(prompt, write=write, workdir=workdir, tag=tag, on_chunk=on_chunk, **kw)


class ReliabilityTests(unittest.TestCase):
    def test_write_task_that_wrote_nothing_is_revised(self):
        _NoWriteMock.turns = 0
        h = Harness(self, cls=_NoWriteMock, mode="asamali")
        asyncio.run(h.orch.run("hesap makinesi"))
        auto = [e for e in h.of("denetim") if e.get("otomatik")]
        self.assertTrue(any("Hiçbir dosya oluşturmadın" in e["geri_bildirim"] for e in auto))
        self.assertTrue((h.workspace / "ornek" / "notlar.md").exists())      # ikinci turda yazdı
        t1 = [e for e in h.of("gorev_sonucu") if e["gorev"] == "t1"][0]
        self.assertEqual(t1["durum"], "tamam")
        self.assertNotIn("Görev dosya yazmadı; kod yalnızca raporda olabilir", t1["uyarilar"])

    def test_verify_python_uses_orkestra_interpreter(self):
        ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        cmd = VerifyCommand("py", ["python", "-c", "import sys; print(sys.executable)"], 30)
        res = asyncio.run(run_verify(cmd, ws, ["PATH", "SYSTEMROOT"]))
        self.assertTrue(res.ok, res.output)
        self.assertEqual(Path(res.output.strip()).resolve(), Path(sys.executable).resolve())

    def test_verify_hints(self):
        r = VerifyResult("python-test", 5, 0.1, "\n----\nRan 0 tests in 0.000s\n\nNO TESTS RAN", False)
        self.assertIn("unittest.TestCase", verify_hint(r))
        self.assertEqual(verify_hint(VerifyResult("x", 1, 0.1, "AssertionError", False)), "")

    def test_planner_prompt_discourages_splitting_small_goals(self):
        h = Harness(self)
        asyncio.run(h.orch.run("hesap makinesi"))
        prompt = h.of("cagri")[0]["istem"]
        self.assertIn("TEK görev olmalı", prompt)
        self.assertIn("python-test = python -m unittest discover -q", prompt)

    def test_usage_totals_keep_cache_separate(self):
        h = Harness(self)
        asyncio.run(h.orch.run("deneme"))
        last = h.of("kullanim")[-1]
        self.assertIn("onbellek", last["toplam"]["claude"])
        self.assertTrue(all("no" in e for e in h.of("kullanim")))


class AnalysisTests(unittest.TestCase):
    def test_analyze_run_summary(self):
        from orkestra.analiz import analyze_run
        h = Harness(self, mode="asamali")
        asyncio.run(h.orch.run("hesap makinesi"))
        h.transcript.close()
        text = "\n".join(analyze_run(h.transcript.dir))
        for frag in ("Rol bazında", "planlayıcı", "işçi", "denetçi", "sentez", "t1: ✓", "Küçük bir iş aşamalı modda",
                     "danışma yapılmış"):
            self.assertIn(frag, text)
        self.assertNotIn("Sahte plan", text)          # istem/yanıt metinleri gösterilmez

    def test_cancelled_run_not_reported_as_missing_write(self):
        from orkestra.analiz import analyze_run
        from orkestra.approvals import StaticApprover
        from orkestra.engine import RunCancelled
        h = Harness(self, mode="asamali", approver=StaticApprover(plan="ret"))
        with self.assertRaises(RunCancelled):
            asyncio.run(h.orch.run("x"))
        h.transcript.close()
        text = "\n".join(analyze_run(h.transcript.dir))
        self.assertIn("t1: – çalışmadı", text)
        self.assertIn("iptal edildi", text)
        self.assertNotIn("hiç dosya değiştirmemiş", text)


if __name__ == "__main__":
    unittest.main()
