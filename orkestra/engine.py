"""Orkestrasyon motoru (bölüm 6.7).

Bölümler:
  1. Sabitler ve yardımcılar
  2. Orchestrator: kurulum ve tek çağrı noktası (call)
  3. Planlama (make_plan)
  4. Yürütme: zamanlayıcı (execute)
  5. Görev: işçi + denetim döngüsü (run_task)
  6. İşçi + danışma döngüsü (work)
  7. Sentez (synthesize)
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import prompts
from .agents import Agent, AgentError
from .config import Settings
from .planner import Plan, PlanError, Task, parse_plan, parse_review
from .security import SecurityError, redact
from .transcript import Transcript

# ====================================================================== 1. Sabitler ve yardımcılar

ASK_RE = re.compile(r'<<SOR\s+ajan="([a-z0-9_-]{1,40})"\s*>>(.*?)<</SOR>>', re.S)
MAX_ASKS_PER_TURN = 2
MAX_QUESTION_CHARS = 2000


class BudgetExceeded(Exception):
    """Toplam model çağrısı sınırı (max_calls) aşıldı."""


@dataclass
class TaskResult:
    status: str          # "tamam" | "hata" | "atlandı"
    output: str = ""
    error: str = ""


def strip_asks(text: str) -> str:
    """Tüm SOR bloklarını kaldırır."""
    return ASK_RE.sub("", text or "").strip()


# ====================================================================== 2. Orchestrator ve call()

class Orchestrator:
    def __init__(self, settings: Settings, agents: dict[str, Agent], transcript: Transcript,
                 workspace: Path, log: Callable[[str], None] = print):
        self.settings = settings
        self.agents = agents
        self.transcript = transcript
        self.workspace = Path(workspace)
        self.log = log
        self.calls = 0
        self._sems = {name: asyncio.Semaphore(a.cfg.max_parallel) for name, a in agents.items()}
        self.write_lock = asyncio.Lock()

    @property
    def _cfgs(self):
        return [a.cfg for a in self.agents.values()]

    async def call(self, agent_name: str, prompt: str, *, role: str, tag: str, write: bool = False) -> str:
        """Tek model çağrısı. Bütçe, eşzamanlılık sınırları ve kayıt burada uygulanır."""
        if self.calls >= self.settings.max_calls:
            raise BudgetExceeded(f"Çağrı bütçesi doldu ({self.settings.max_calls}).")
        self.calls += 1
        n = self.calls
        self.transcript.event("cagri", no=n, ajan=agent_name, rol=role, gorev=tag, yazma=write, istem=prompt)
        self.log(f"→ [{n}] {agent_name} {role} {tag}" + (" (✎ yazma)" if write else ""))
        agent = self.agents[agent_name]
        # Semafor ve kilit çağrı başına alınır, görev boyunca tutulmaz: böylece bir işçi
        # başka bir ajana danışırken kilitlenme oluşmaz.
        async with self._sems[agent_name]:
            if write:
                async with self.write_lock:
                    out = await agent.run(prompt, write=True, workdir=self.workspace, tag=f"{tag}-{n}")
            else:
                out = await agent.run(prompt, write=False, workdir=self.workspace, tag=f"{tag}-{n}")
        self.transcript.event("yanit", no=n, ajan=agent_name, rol=role, gorev=tag, metin=out)
        self.log(f"← [{n}] {agent_name} {len(out)} karakter")
        return out

    # ================================================================== 3. Planlama

    async def make_plan(self, goal: str) -> Plan:
        s = self.settings
        names = list(self.agents)
        prompt = prompts.planner(goal, self._cfgs, s.planner, s.max_tasks, s.language)
        out = await self.call(s.planner, prompt, role="planlayıcı", tag="plan")
        try:
            plan = parse_plan(out, names, s.max_tasks)
        except PlanError as e:
            self.log(f"⚠ Plan geçersiz: {e} Planlayıcıdan bir kez düzeltme isteniyor…")
            fix = prompts.planner_fix(prompt, out, str(e))
            out = await self.call(s.planner, fix, role="planlayıcı", tag="plan-duzeltme")
            plan = parse_plan(out, names, s.max_tasks)  # ikinci hata dışarı fırlatılır
        self.transcript.event("plan", plan=plan.to_dict())
        return plan

    # ================================================================== 4. Yürütme

    async def execute(self, plan: Plan, goal: str) -> dict[str, TaskResult]:
        pending: dict[str, Task] = {t.id: t for t in plan.tasks}
        running: dict[asyncio.Task, str] = {}
        results: dict[str, TaskResult] = {}
        budget_hit = False

        def finish(tid: str, res: TaskResult) -> None:
            results[tid] = res
            self.transcript.event("gorev_sonucu", gorev=tid, durum=res.status, hata=res.error)
            icon = {"tamam": "✔", "hata": "✖", "atlandı": "⤼"}.get(res.status, "•")
            self.log(f"{icon} {tid} {res.status}" + (f": {res.error}" if res.error else ""))

        try:
            while pending or running:
                # 4a. Zincirleme atlama
                changed = True
                while changed:
                    changed = False
                    for tid, t in list(pending.items()):
                        bad = [d for d in t.depends_on if d in results and results[d].status != "tamam"]
                        if bad or budget_hit:
                            reason = (f"Bağımlı olduğu görev tamamlanamadı: {bad[0]}" if bad
                                      else "Çağrı bütçesi doldu.")
                            del pending[tid]
                            finish(tid, TaskResult("atlandı", error=reason))
                            changed = True
                # 4b. Hazır görevleri başlat
                for tid, t in list(pending.items()):
                    if all(d in results and results[d].status == "tamam" for d in t.depends_on):
                        del pending[tid]
                        running[asyncio.create_task(self.run_task(t, goal, results))] = tid
                if not running:
                    break
                # 4c. İlk biteni bekle
                done, _ = await asyncio.wait(running.keys(), return_when=asyncio.FIRST_COMPLETED)
                for fut in done:
                    tid = running.pop(fut)
                    try:
                        res = fut.result()
                    except BudgetExceeded as e:
                        budget_hit = True
                        res = TaskResult("hata", error=str(e))
                    except (AgentError, SecurityError, OSError) as e:
                        res = TaskResult("hata", error=redact(str(e)))
                    finish(tid, res)
        finally:
            # Beklenmeyen hata veya iptal (Ctrl+C) durumunda yarım kalan görevler iptal edilir;
            # CliAgent iptalde süreç ağacını sonlandırır.
            for fut in running:
                fut.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)

        # Döngüsüz bir planda buraya kalan görev olmamalı; yine de güvenli tarafta kalınır.
        for tid in list(pending):
            finish(tid, TaskResult("atlandı", error="Bağımlılıkları çözülemedi."))
        return results

    # ================================================================== 5. Görev

    async def run_task(self, task: Task, goal: str, results: dict[str, TaskResult]) -> TaskResult:
        s = self.settings
        deps = {d: results[d].output for d in task.depends_on}
        output = await self.work(task, goal, deps)
        if s.reviewer:
            for _ in range(s.max_revisions):
                raw = await self.call(s.reviewer, prompts.review(goal, task, output, s.language),
                                      role="denetçi", tag=f"{task.id}-denetim")
                verdict, feedback = parse_review(raw)
                self.transcript.event("denetim", gorev=task.id, karar=verdict, geri_bildirim=feedback)
                self.log(f"🔍 {task.id} denetim: {'ONAY ✓' if verdict == 'approve' else 'REVİZYON ↺'}")
                if verdict == "approve":
                    break
                output = await self.work(task, goal, deps, feedback=feedback, previous=output)
        return TaskResult("tamam", output)

    # ================================================================== 6. İşçi + danışma

    async def work(self, task: Task, goal: str, deps: dict[str, str],
                   feedback: str | None = None, previous: str | None = None) -> str:
        s = self.settings
        answers: list[dict] = []
        draft = previous
        for rnd in range(s.max_consult_rounds + 1):
            allow_ask = rnd < s.max_consult_rounds
            prompt = prompts.worker(goal, task, self._cfgs, deps, answers, draft, feedback,
                                    s.language, allow_ask=allow_ask)
            out = await self.call(task.assignee, prompt, role="işçi", tag=task.id, write=task.needs_write)
            asks = ASK_RE.findall(out)[:MAX_ASKS_PER_TURN]
            if not asks or not allow_ask:
                return strip_asks(out)
            draft = strip_asks(out) or draft
            for target, question in asks:
                question = question.strip()[:MAX_QUESTION_CHARS]
                if target not in self.agents or target == task.assignee:
                    answer = f"(Geçersiz hedef: {target})"
                else:
                    self.log(f"💬 {task.id}: {task.assignee} → {target} danışıyor")
                    # Danışman hiçbir zaman yazma izni almaz.
                    raw = await self.call(target, prompts.consult(goal, task.assignee, task, question, s.language),
                                          role="danışman", tag=f"{task.id}-danisma", write=False)
                    answer = strip_asks(raw)
                answers.append({"hedef": target, "soru": question, "cevap": answer})
                self.transcript.event("danisma", gorev=task.id, soran=task.assignee, hedef=target,
                                      soru=question, cevap=answer)
        return strip_asks(draft or "")  # erişilmez: son turda allow_ask kapalıdır

    # ================================================================== 7. Sentez

    async def synthesize(self, goal: str, plan: Plan, results: dict[str, TaskResult]) -> str:
        s = self.settings
        return await self.call(s.planner, prompts.synthesis(goal, plan, results, s.language),
                               role="sentez", tag="sentez")
