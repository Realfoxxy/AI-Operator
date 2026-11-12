"""Sürüm 0.3 özellikleri: devretme, sıkı mod, kontrol noktaları, plan düzenleme, oturum, kullanım, Docker."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orkestra.agents import AgentError, CliAgent, MockAgent, usage_from
from orkestra.approvals import StaticApprover
from orkestra.checkpoints import CheckpointError, CheckpointStore
from orkestra.config import AgentConfig, VerifyCommand
from orkestra.engine import RWLock
from orkestra.verify import docker_argv
from tests.helpers import Harness


class _QuotaMock(MockAgent):
    """gemini her işçi çağrısında kota hatası verir."""

    async def run(self, prompt, **kw):
        if self.name == "gemini" and prompt.startswith("<<ROL:ISCI>>"):
            raise AgentError("gemini: CLI hata bildirdi: Quota exceeded for this account (429)")
        return await super().run(prompt, **kw)


class FallbackTests(unittest.TestCase):
    def test_quota_error_hands_task_to_other_agent(self):
        h = Harness(self, cls=_QuotaMock)
        report = asyncio.run(h.orch.run("deneme"))
        self.assertIn("Sahte sentez", report)
        handovers = h.of("devretme")
        self.assertEqual(handovers[0]["eski"], "gemini")
        self.assertEqual(handovers[0]["gorev"], "t1")
        self.assertIn("gemini", h.orch._exhausted)
        self.assertEqual({e["durum"] for e in h.of("gorev_sonucu")}, {"tamam"})
        # Devredilen ajanın istemi kendi kimliğiyle kurulur
        worker_calls = [e for e in h.of("cagri") if e["rol"] == "işçi" and e["gorev"] == "t1"]
        self.assertTrue(any(e["ajan"] != "gemini" and f"Kimliğin: {e['ajan']}" in e["istem"] for e in worker_calls))
        # Kullanılamaz ajana danışma yapılmaz
        answers = [e["cevap"] for e in h.of("danisma") if e["hedef"] == "gemini"]
        self.assertTrue(all("kullanılamıyor" in a for a in answers))

    def test_fallback_disabled_keeps_failure(self):
        h = Harness(self, cls=_QuotaMock, fallback=False)
        asyncio.run(h.orch.run("deneme"))
        results = {e["gorev"]: e["durum"] for e in h.of("gorev_sonucu")}
        self.assertEqual(results["t1"], "hata")
        self.assertEqual(results["t3"], "atlandı")
        self.assertEqual(h.of("devretme"), [])


class StrictModeTests(unittest.TestCase):
    def test_rwlock_excludes_readers_during_write(self):
        lock, log = RWLock(), []

        async def reader(i):
            async with lock.read():
                log.append(("r+", i)); await asyncio.sleep(0.02); log.append(("r-", i))

        async def writer():
            await asyncio.sleep(0.005)
            async with lock.write():
                log.append(("w+", 0)); await asyncio.sleep(0.02); log.append(("w-", 0))

        async def main():
            await asyncio.gather(reader(1), writer(), reader(2))
        asyncio.run(main())
        w_start, w_end = log.index(("w+", 0)), log.index(("w-", 0))
        self.assertEqual(w_end, w_start + 1, log)          # yazma sırasında hiçbir okuma başlamadı/bitmedi

    def test_strict_run_completes(self):
        h = Harness(self, strict_guard=True, mode="asamali")
        self.assertIn("Sahte sentez", asyncio.run(h.orch.run("deneme")))
        self.assertTrue((h.workspace / "ornek" / "notlar.md").exists())


@unittest.skipUnless(shutil.which("git"), "git gerekli")
class CheckpointTests(unittest.TestCase):
    def test_run_creates_checkpoints_and_restore(self):
        h = Harness(self, mode="asamali")
        (h.workspace / "benim.txt").write_text("kullanıcının dosyası\n", encoding="utf-8")
        asyncio.run(h.orch.run("deneme"))
        cps = h.of("kontrol_noktasi")
        self.assertEqual([c["asama"] for c in cps], [0, 1, 2])
        self.assertTrue(cps[0]["baslangic"])
        state = json.loads((h.transcript.dir / "durum.json").read_text(encoding="utf-8"))
        self.assertEqual(len(state["kontrol_noktalari"]), 3)
        store = CheckpointStore(h.workspace, h.cfg.settings.runs_dir)
        self.assertNotIn(str(h.workspace), str(store.git_dir))           # depo çalışma klasörünün dışında
        changes = dict((p, st) for st, p in store.changes_since(cps[0]["sha"]))
        self.assertEqual(changes.get("ornek/notlar.md"), "A")
        self.assertIn("+[gemini]", store.diff(cps[0]["sha"], "ornek/notlar.md").replace(" ", ""))
        before, rows = store.restore(cps[0]["sha"])
        self.assertFalse((h.workspace / "ornek" / "notlar.md").exists())
        self.assertTrue((h.workspace / "benim.txt").exists())
        self.assertTrue((h.workspace / ".orkestra" / "proje_hafizasi.md").exists())   # .orkestra'ya dokunulmaz
        store.restore(before.sha)                                                   # geri alma da geri alınır
        self.assertTrue((h.workspace / "ornek" / "notlar.md").exists())

    def test_invalid_sha_and_hooks_ignored(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        ws = d / "ws"; ws.mkdir()
        store = CheckpointStore(ws, d / "runs")
        store.create("ilk")
        for bad in ("HEAD", "--help", "../x", "abc", ""):
            with self.assertRaises(CheckpointError):
                store.restore(bad)
        # Çalışma klasöründeki bir .git/hooks veya .gitattributes git çağrılarını etkilemez
        (ws / ".git" / "hooks").mkdir(parents=True)
        hook = ws / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\ntouch HACKED\n", encoding="utf-8"); hook.chmod(0o755)
        store.create("ikinci")
        self.assertFalse((ws / "HACKED").exists())


class PlanEditTests(unittest.TestCase):
    def test_user_edit_reassigns_task(self):
        def edit(plan):
            plan = json.loads(json.dumps(plan))
            plan["tasks"][1]["assignee"] = "claude"
            plan["tasks"][1]["instructions"] = "Kullanıcının düzenlediği talimat."
            return plan
        h = Harness(self, approver=StaticApprover(edit=edit))
        asyncio.run(h.orch.run("deneme"))
        plans = h.of("plan")
        self.assertTrue(plans[-1].get("duzenlendi"))
        t2 = [e for e in h.of("cagri") if e["rol"] == "işçi" and e["gorev"] == "t2"]
        self.assertEqual({e["ajan"] for e in t2}, {"claude"})
        self.assertIn("Kullanıcının düzenlediği talimat.", t2[0]["istem"])
        saved = json.loads((h.transcript.dir / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["tasks"][1]["assignee"], "claude")

    def test_invalid_edit_rejected(self):
        def edit(plan):
            plan = json.loads(json.dumps(plan))
            plan["tasks"][0]["needs_write"] = True
            plan["tasks"][0]["files"] = [".claude/settings.json"]
            return plan
        from orkestra.planner import PlanError
        h = Harness(self, approver=StaticApprover(edit=edit))
        with self.assertRaises(PlanError):
            asyncio.run(h.orch.run("deneme"))


class SessionAndUsageTests(unittest.TestCase):
    def test_usage_from_cli_json(self):
        claude = {"result": "x", "session_id": "0f6c2a9e-1111-2222-3333-444455556666", "total_cost_usd": 0.0123,
                  "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5}}
        self.assertEqual(usage_from(claude), {"girdi": 100, "cikti": 5, "maliyet_usd": 0.0123})
        gemini = {"response": "x", "stats": {"models": {"gemini-2.5-pro": {"tokens": {"prompt": 7, "candidates": 3}}}}}
        self.assertEqual(usage_from(gemini), {"girdi": 7, "cikti": 3, "maliyet_usd": None})
        self.assertIsNone(usage_from({"result": "x"}))

    def test_engine_resumes_session_on_revision(self):
        class Reviser(MockAgent):
            reviews = 0

            async def run(self, prompt, **kw):
                if prompt.startswith("<<ROL:DENETCI>>") and Reviser.reviews == 0:
                    Reviser.reviews += 1
                    return '{"verdict": "revise", "feedback": "Eksik."}'
                return await super().run(prompt, **kw)
        h = Harness(self, cls=Reviser)
        asyncio.run(h.orch.run("deneme"))
        resumed = [e for e in h.of("yanit") if e["rol"] == "işçi" and e.get("devam_oturumu")]
        self.assertTrue(resumed, "revizyonda oturum sürdürülmedi")
        usage = h.of("kullanim")[-1]["toplam"]
        self.assertGreater(usage["claude"]["girdi"], 0)
        self.assertEqual(sum(v["cagri"] for v in usage.values()), h.orch.calls)

    def test_resume_args_only_with_valid_session(self):
        cfg = AgentConfig(name="c", display="C", command=[sys.executable], resume_args=["--resume", "{session}"],
                          session_field="session_id")
        agent = CliAgent(cfg)
        captured = {}

        async def fake_exec(*argv, **kw):
            captured["argv"] = argv
            raise OSError("durdur")
        with mock.patch("asyncio.create_subprocess_exec", fake_exec):
            for sid, expect in (("0f6c2a9e-1111-2222-3333-444455556666", True), ("--evil; rm -rf /", False),
                                ("x" * 300, False)):
                with self.assertRaises(OSError):
                    asyncio.run(agent.run("p", write=False, workdir=Path(tempfile.gettempdir()), tag="t1-1", session=sid))
                self.assertEqual("--resume" in captured["argv"], expect, sid)


class DockerSandboxTests(unittest.TestCase):
    def test_docker_argv_is_locked_down(self):
        with mock.patch("shutil.which", return_value="/usr/bin/docker"):
            argv = docker_argv(VerifyCommand("t", ["python", "-m", "unittest"], 30), Path("/tmp/ws"), "python:3.12-slim",
                               "orkestra-dogrulama-x")
        for part in (["--network", "none"], ["--cap-drop", "ALL"], ["--security-opt", "no-new-privileges"]):
            self.assertIn(part[0], argv)
            self.assertEqual(argv[argv.index(part[0]) + 1], part[1])
        self.assertEqual(argv[-4:], ["python:3.12-slim", "python", "-m", "unittest"])
        self.assertIn("orkestra-dogrulama-x", argv)
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(docker_argv(VerifyCommand("t", ["x"], 30), Path("/tmp"), "img"))


if __name__ == "__main__":
    unittest.main()
