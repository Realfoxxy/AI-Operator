"""Sürüm 0.4: yapay zekâ kataloğu, güvenli config düzenleme, dosya yazamayan ajanlar, Ollama ajanı."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from orkestra import configedit, katalog
from orkestra.agents import AgentError, MockAgent, OllamaAgent, build_agents
from orkestra.config import AgentConfig, ConfigError, load_config
from orkestra.planner import PlanError, parse_plan
from tests.helpers import ROOT, Harness


def _tmp_config(tc: unittest.TestCase, text: str | None = None, raw: bytes | None = None) -> Path:
    d = Path(tempfile.mkdtemp())
    tc.addCleanup(shutil.rmtree, d, ignore_errors=True)
    p = d / "config.toml"
    if raw is not None:
        p.write_bytes(raw)
    else:
        p.write_text(text if text is not None else (ROOT / "config.toml").read_text(encoding="utf-8"), encoding="utf-8")
    return p


class CatalogTests(unittest.TestCase):
    def test_every_entry_produces_valid_config(self):
        for e in katalog.CATALOG:
            with self.subTest(e.ad):
                p = _tmp_config(self)
                configedit.add_agent(p, e.ad, e.ayar)
                cfg = load_config(p)
                self.assertIn(e.ad, cfg.agents)
                self.assertTrue(configedit.from_catalog(p, e.ad))
                self.assertIn(e.durum, ("hazır", "deneysel"))
                self.assertTrue(e.npm or e.kurulum, "kurulum bilgisi eksik")
        ollama = load_config(self._with("ollama")).agents["ollama"]
        self.assertFalse(ollama.can_write)
        self.assertEqual(ollama.api, "ollama")

    def _with(self, ad):
        p = _tmp_config(self)
        configedit.add_agent(p, ad, katalog.get(ad).ayar)
        return p

    def test_add_twice_and_remove_rules(self):
        p = self._with("codex")
        with self.assertRaises(configedit.ConfigEditError):
            configedit.add_agent(p, "codex", katalog.get("codex").ayar)
        with self.assertRaises(configedit.ConfigEditError):
            configedit.remove_agent(p, "copilot")          # katalogdan eklenmedi
        self.assertTrue((p.parent / "config.toml.yedek").exists())
        configedit.remove_agent(p, "codex")
        self.assertNotIn("codex", load_config(p).agents)
        self.assertNotIn("orkestra-katalog: codex", p.read_text(encoding="utf-8"))

    def test_enable_disable_keeps_crlf_and_bom_and_protects_planner(self):
        text = (ROOT / "config.toml").read_text(encoding="utf-8").replace("\n", "\r\n")
        p = _tmp_config(self, raw=b"\xef\xbb\xbf" + text.encode("utf-8"))
        configedit.set_enabled(p, "gemini", False)
        self.assertNotIn("gemini", load_config(p).agents)
        self.assertNotIn(b"\n", p.read_bytes().replace(b"\r\n", b""))          # CRLF korundu
        self.assertEqual(configedit.agent_states(p)["gemini"], False)
        configedit.set_enabled(p, "gemini", True)
        self.assertIn("gemini", load_config(p).agents)
        self.assertEqual(p.read_text(encoding="utf-8").count("enabled = "), 1)   # satır çoğalmadı
        before = p.read_bytes()
        with self.assertRaises(configedit.ConfigEditError):
            configedit.set_enabled(p, "claude", False)                         # planlayıcı kapatılamaz
        self.assertEqual(p.read_bytes(), before)                                # geçersiz değişiklik yazılmadı
        with self.assertRaises(configedit.ConfigEditError):
            configedit.set_enabled(p, "yok", True)

    def test_api_agent_must_be_local(self):
        bad = dict(katalog.get("ollama").ayar, api_url="http://ornek.com:11434")
        p = _tmp_config(self)
        with self.assertRaises(configedit.ConfigEditError):
            configedit.add_agent(p, "ollama", bad)


class WriteCapabilityTests(unittest.TestCase):
    def _plan(self, assignee, write):
        return json.dumps({"summary": "s", "tasks": [{"id": "t1", "title": "T", "assignee": assignee,
                                                      "instructions": "Yap.", "expected_output": "Ç.",
                                                      "needs_write": write, "files": ["a.txt"] if write else []}]})

    def test_non_writer_cannot_get_write_task(self):
        names = ["claude", "ollama"]
        with self.assertRaises(PlanError):
            parse_plan(self._plan("ollama", True), names, 8, writers={"claude"})
        parse_plan(self._plan("ollama", False), names, 8, writers={"claude"})
        parse_plan(self._plan("claude", True), names, 8, writers={"claude"})

    def test_fallback_skips_non_writers_for_write_tasks(self):
        p = _tmp_config(self)
        configedit.add_agent(p, "ollama", katalog.get("ollama").ayar)
        h = Harness(self, cfg=load_config(p))
        orch = h.orch
        orch._exhausted.add("claude")
        self.assertEqual(orch._alternative("gemini", set(), need_write=True), "copilot")
        self.assertIn(orch._alternative("gemini", {"copilot"}, need_write=False), ("ollama",))
        self.assertIsNone(orch._alternative("gemini", {"copilot"}, need_write=True))

    def test_mock_phased_run_with_non_writer(self):
        p = _tmp_config(self)
        configedit.set_enabled(p, "gemini", False)
        configedit.add_agent(p, "ollama", katalog.get("ollama").ayar)
        h = Harness(self, cfg=load_config(p), mode="asamali")
        asyncio.run(h.orch.run("deneme"))
        writes = [e for e in h.of("cagri") if e["yazma"]]
        self.assertTrue(writes)
        self.assertTrue(all(e["ajan"] != "ollama" for e in writes))
        self.assertIn("DOSYA YAZAMAZ", h.of("cagri")[0]["istem"])            # planlayıcıya bildirildi


class _FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        reply = {"model": body["model"], "message": {"role": "assistant", "content": "  Yerel yanıt.  "},
                 "prompt_eval_count": 12, "eval_count": 3, "done": True}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class OllamaAgentTests(unittest.TestCase):
    def setUp(self):
        self.srv = HTTPServer(("127.0.0.1", 0), _FakeOllama)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.cfg = AgentConfig("ollama", "O", ["ollama"], api="ollama", api_model="deneme:1b",
                               api_url=f"http://127.0.0.1:{self.srv.server_address[1]}", can_write=False, timeout_s=10)

    def test_chat_and_usage(self):
        meta, chunks = {}, []
        out = asyncio.run(OllamaAgent(self.cfg).run("soru", write=False, workdir=Path("."), tag="t1-1",
                                                    on_chunk=chunks.append, meta=meta))
        self.assertEqual(out, "Yerel yanıt.")
        self.assertEqual(meta["usage"], {"girdi": 12, "cikti": 3, "maliyet_usd": None})
        self.assertEqual(chunks, ["  Yerel yanıt.  "])

    def test_write_refused_and_server_down(self):
        with self.assertRaises(AgentError):
            asyncio.run(OllamaAgent(self.cfg).run("x", write=True, workdir=Path("."), tag="t1-1"))
        down = AgentConfig(**{**self.cfg.__dict__, "api_url": "http://127.0.0.1:9"})
        with self.assertRaises(AgentError) as cm:
            asyncio.run(OllamaAgent(down).run("x", write=False, workdir=Path("."), tag="t1-1"))
        self.assertIn("Ollama", str(cm.exception))

    def test_factory_builds_right_types(self):
        cfgs = {"o": self.cfg, "c": AgentConfig("c", "C", ["claude"])}
        agents = build_agents(cfgs)
        self.assertIsInstance(agents["o"], OllamaAgent)
        mocks = build_agents(cfgs, mock=True)
        self.assertIsInstance(mocks["o"], MockAgent)
        self.assertEqual(mocks["o"].writers, ["c"])


class TerminalTeamCommandTests(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(d / "xdg"), "APPDATA": str(d / "xdg"), "NO_COLOR": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.p = _tmp_config(self)
        self.out = io.StringIO()
        sp = mock.patch("sys.stdout", self.out)
        sp.start()
        self.addCleanup(sp.stop)

    def test_commands(self):
        from orkestra.terminal import TerminalApp
        cfg = load_config(self.p)
        app = TerminalApp(cfg, mock=True, mock_speed=0)
        app.handle("/katalog")
        app.handle("/ekle claude-kodcu")
        self.assertIn("claude-kodcu", app.cfg.agents)
        app.handle("/ajan kapat gemini")
        self.assertNotIn("gemini", app.cfg.agents)
        app.handle("/ajanlar")
        app.handle("/cikar claude-kodcu")
        self.assertNotIn("claude-kodcu", app.cfg.agents)
        app.handle("/ekle yok-boyle")
        out = self.out.getvalue()
        for frag in ("Eklenebilir yapay zekâlar", "✔ claude-kodcu ekibe eklendi", "✔ gemini kapatıldı",
                     "○ kapalı", "✔ claude-kodcu ekipten çıkarıldı", "Katalogda 'yok-boyle' yok"):
            self.assertIn(frag, out)


if __name__ == "__main__":
    unittest.main()
