"""Doğrulama komutları: beyaz liste, ortam süzme, başarısızlıkta otomatik revizyon (bölüm 9.5)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest import mock

from orkestra.agents import MockAgent
from orkestra.config import VerifyCommand, load_config
from orkestra.planner import PlanError, parse_plan
from orkestra.verify import filter_env, is_secret_name, run_verify
from tests.helpers import ROOT, Harness

AGENTS = ["claude", "gemini", "copilot"]


def _plan(verify):
    return json.dumps({"summary": "s", "tasks": [{
        "id": "t1", "title": "T", "assignee": "gemini", "instructions": "Yap.",
        "expected_output": "Çıktı.", "needs_write": False, "verify": verify}]})


class WhitelistTests(unittest.TestCase):
    def test_unknown_verify_name_rejected(self):
        with self.assertRaises(PlanError):
            parse_plan(_plan(["rm-rf"]), AGENTS, 8, verify_names=["python-test"])
        plan = parse_plan(_plan(["python-test"]), AGENTS, 8, verify_names=["python-test"])
        self.assertEqual(plan.tasks[0].verify, ["python-test"])
        with self.assertRaises(PlanError):
            parse_plan(_plan("python-test"), AGENTS, 8, verify_names=["python-test"])


class EnvFilterTests(unittest.TestCase):
    def test_secret_names(self):
        for name in ("MY_API_KEY", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "DB_PASSWORD", "GITHUB_PAT",
                     "GOOGLE_APPLICATION_CREDENTIALS"):
            self.assertTrue(is_secret_name(name), name)
        for name in ("PATH", "PATHEXT", "HOME", "LANG"):
            self.assertFalse(is_secret_name(name), name)

    def test_env_filtered_in_child_process(self):
        ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        cmd = VerifyCommand("ortam", [sys.executable, "-c",
                                      "import os, json; print(json.dumps(sorted(os.environ)))"], 30)
        env = {"MY_API_KEY": "gizli-deger", "GITHUB_PAT": "x", "ORKESTRA_DENEME": "1"}
        with mock.patch.dict(os.environ, env):
            self.assertNotIn("MY_API_KEY", filter_env(["PATH", "MY_API_KEY"]))
            res = asyncio.run(run_verify(cmd, ws, ["PATH", "MY_API_KEY", "GITHUB_PAT", "HOME"]))
        self.assertTrue(res.ok, res.output)
        names = json.loads(res.output.strip().splitlines()[-1])
        self.assertIn("PATH", names)
        self.assertNotIn("MY_API_KEY", names)
        self.assertNotIn("GITHUB_PAT", names)
        self.assertNotIn("ORKESTRA_DENEME", names)   # listede olmayan aktarılmaz

    def test_timeout_and_missing(self):
        ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        slow = VerifyCommand("yavas", [sys.executable, "-c", "import time; time.sleep(30)"], 1)
        res = asyncio.run(run_verify(slow, ws, ["PATH"]))
        self.assertFalse(res.ok)
        self.assertIsNone(res.exit_code)
        self.assertIn("Zaman aşımı", res.output)
        missing = asyncio.run(run_verify(VerifyCommand("yok", ["olmayan-program-xyz"], 10), ws, ["PATH"]))
        self.assertFalse(missing.ok)


class _VerifyPlanMock(MockAgent):
    """Planlayıcı tek görevli ve doğrulamalı bir plan döndürür."""

    async def run(self, prompt, *, write, workdir, tag, on_chunk=None):
        if prompt.startswith("<<ROL:PLANLAYICI>>"):
            return _plan(["kirik"])
        return await super().run(prompt, write=write, workdir=workdir, tag=tag, on_chunk=on_chunk)


class EngineVerifyTests(unittest.TestCase):
    def _cfg(self, code: int):
        cfg = load_config(ROOT / "config.toml")
        cfg.verify.commands = [VerifyCommand("kirik", [sys.executable, "-c",
                                                       f"print('TEST CIKTISI'); raise SystemExit({code})"], 30)]
        return cfg

    def test_failure_triggers_auto_revision_without_reviewer(self):
        h = Harness(self, cls=_VerifyPlanMock, cfg=self._cfg(1))
        report = asyncio.run(h.orch.run("deneme"))
        self.assertIn("Sahte sentez", report)
        roles = [(e["ajan"], e["rol"]) for e in h.of("cagri")]
        # plan → işçi → (doğrulama başarısız) → işçi (otomatik revizyon, denetçi çağrılmadan) → sentez
        self.assertEqual(roles, [("claude", "planlayıcı"), ("gemini", "işçi"), ("gemini", "işçi"),
                                 ("claude", "sentez")])
        auto = [e for e in h.of("denetim") if e.get("otomatik")]
        self.assertEqual(len(auto), 1)
        self.assertIn("TEST CIKTISI", auto[0]["geri_bildirim"])
        self.assertEqual(len(h.of("dogrulama")), 2)
        result = h.of("gorev_sonucu")[0]
        self.assertEqual(result["durum"], "tamam")
        self.assertIn("Doğrulama başarısız: kirik", result["uyarilar"])
        approvals = [p for k, p in h.approver.requests if k == "plan"]
        self.assertEqual(approvals[0]["verify_commands"][0]["name"], "kirik")

    def test_success_goes_to_reviewer_with_results(self):
        h = Harness(self, cls=_VerifyPlanMock, cfg=self._cfg(0))
        asyncio.run(h.orch.run("deneme"))
        review_prompts = [e["istem"] for e in h.of("cagri") if e["rol"] == "denetçi"]
        self.assertEqual(len(review_prompts), 1)
        self.assertIn("DOĞRULAMA SONUÇLARI", review_prompts[0])
        self.assertIn("BAŞARILI", review_prompts[0])


if __name__ == "__main__":
    unittest.main()
