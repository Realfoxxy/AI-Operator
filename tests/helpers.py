"""Testler için ortak kurulum: geçici klasörler, sahte ajanlar ve olay toplayıcı."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from orkestra.agents import MockAgent
from orkestra.approvals import StaticApprover
from orkestra.config import load_config
from orkestra.engine import Orchestrator
from orkestra.events import ConsolePrinter, EventBus
from orkestra.state import ProjectMemory, RunState
from orkestra.transcript import Transcript

ROOT = Path(__file__).resolve().parent.parent


class Harness:
    """Sahte ajanlı bir Orchestrator kurar; olayları ve konsol satırlarını toplar."""

    def __init__(self, tc: unittest.TestCase, cls=MockAgent, approver=None, tmp: Path | None = None,
                 state: RunState | None = None, resume_plans=None, run_dir: Path | None = None,
                 cfg=None, **settings):
        if tmp is None:
            tmp = Path(tempfile.mkdtemp())
            tc.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.tmp = tmp
        self.cfg = cfg or load_config(ROOT / "config.toml")
        self.cfg.settings.runs_dir = tmp / "runs"
        self.workspace = tmp / "calisma"
        self.workspace.mkdir(exist_ok=True)
        self.cfg.settings.workspace = self.workspace
        for k, v in settings.items():
            setattr(self.cfg.settings, k, v)
        self.bus = EventBus()
        self.events: list[dict] = []
        self.logs: list[str] = []
        self.bus.add_listener(lambda ev, persist: self.events.append(ev))
        self.bus.add_listener(ConsolePrinter({n: c.display for n, c in self.cfg.agents.items()},
                                             out=self.logs.append))
        self.transcript = Transcript(self.cfg.settings.runs_dir, existing=run_dir)
        tc.addCleanup(self.transcript.close)
        self.bus.add_listener(self.transcript.on_event)
        names = list(self.cfg.agents)
        self.agents = {n: cls(c, names, hiz=0) for n, c in self.cfg.agents.items()}
        self.approver = approver or StaticApprover()
        self.orch = Orchestrator(self.cfg, self.agents, self.bus, self.workspace, self.approver,
                                 run_dir=self.transcript.dir, state=state, resume_plans=resume_plans,
                                 memory=ProjectMemory.load(self.workspace))

    def kinds(self) -> list[str]:
        return [e["tur"] for e in self.events]

    def of(self, tur: str) -> list[dict]:
        return [e for e in self.events if e["tur"] == tur]
