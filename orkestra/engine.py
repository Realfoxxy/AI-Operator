"""Orkestrasyon motoru (bölüm 6.7, 6.9, 9).

Motor doğrudan print veya Transcript çağırmaz; yalnızca EventBus.publish çağırır.

Bölümler:
   1. Sabitler, hatalar ve sonuç yapıları
   2. Orchestrator: kurulum ve olay yardımcıları
   3. Ajan ve görev durumları (ajan_durumu, gorev_durumu, butce)
   4. Kullanıcı kontrolü: duraklat / devam / durdur / not
   5. Tek çağrı noktası: call() — bütçe, kilitler, değişiklik denetimi, canlı akış
   6. Değişiklik raporu ve ihlal yönetimi
   7. Planlama: make_plan (tek mod ve aşama)
   8. Yürütme: execute (bağımlılık zamanlayıcısı)
   9. Görev: run_task (işçi → doğrulama → denetim döngüsü)
  10. İşçi + danışma: work
  11. Doğrulama komutları
  12. Sentez
  13. Üst akış: run() — başlangıç taraması, tek/aşamalı döngü, durum kaydı, proje hafızası
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import prompts
from .agents import Agent, AgentError
from .approvals import Approver, CliApprover
from .checkpoints import CheckpointError, CheckpointStore
from .config import Config
from .events import EventBus
from .guard import CallInfo, ChangeTracker, GuardError, MAX_EVENT_PATHS, Report, scan_startup
from .planner import Plan, PlanError, Task, parse_plan, parse_review
from .security import SecurityError, redact
from .state import PhaseState, ProjectMemory, RunState, save_state
from .verify import VerifyResult, run_verify

# ====================================================================== 1. Sabitler ve yapılar

ASK_RE = re.compile(r'<<SOR\s+ajan="([a-z0-9_-]{1,40})"\s*>>(.*?)<</SOR>>', re.S)
MAX_ASKS_PER_TURN = 2
MAX_QUESTION_CHARS = 2000
# Kota/erişim sorunu gibi görünen hatalar: ajan bu çalıştırmada "kullanılamaz" işaretlenir (devretme).
QUOTA_RE = re.compile(r"quota|rate.?limit|usage limit|limit (?:reached|exceeded)|too many requests|\b429\b|"
                      r"kota|resource.?exhausted|credit balance|programı bulunamadı|not logged in|login", re.I)
CHUNK_INTERVAL_S = 0.15
CHUNK_MAX_CHARS = 2000
MAX_NOTES = 20
MAX_NOTE_CHARS = 2000


class BudgetExceeded(Exception):
    """Toplam model çağrısı sınırı (max_calls) aşıldı."""


class RunStopped(Exception):
    """Çalıştırma kullanıcı tarafından veya bir ihlal nedeniyle durduruldu."""


class RunCancelled(Exception):
    """Plan veya başlangıç uyarısı onaylanmadı."""


@dataclass
class TaskResult:
    status: str          # "tamam" | "hata" | "atlandı"
    output: str = ""
    error: str = ""
    uyarilar: list[str] = field(default_factory=list)


def strip_asks(text: str) -> str:
    """Tüm SOR bloklarını kaldırır."""
    return ASK_RE.sub("", text or "").strip()


_TASK_STATUS = {"tamam": "tamam", "hata": "hata", "atlandı": "atlandi"}


class RWLock:
    """Sıkı mod için okuma/yazma kilidi: okumalar birlikte, yazma ve doğrulama tek başına çalışır.

    Bekleyen bir yazıcı varken yeni okumalar beklenir (yazıcı açlığı önlenir). Kilit her zaman
    ajan semaforundan SONRA alınır; bu sıra kilitlenmeyi önler.
    """

    def __init__(self):
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting = 0

    @contextlib.asynccontextmanager
    async def read(self):
        async with self._cond:
            await self._cond.wait_for(lambda: not self._writer and self._waiting == 0)
            self._readers += 1
        try:
            yield
        finally:
            async with self._cond:
                self._readers -= 1
                self._cond.notify_all()

    @contextlib.asynccontextmanager
    async def write(self):
        async with self._cond:
            self._waiting += 1
            try:
                await self._cond.wait_for(lambda: not self._writer and self._readers == 0)
            finally:
                self._waiting -= 1
            self._writer = True
        try:
            yield
        finally:
            async with self._cond:
                self._writer = False
                self._cond.notify_all()


class _ChunkBuffer:
    """Canlı akış parçalarını biriktirir; en fazla 150 ms'de bir 'parca' olayı yayınlar."""

    def __init__(self, orch: "Orchestrator", agent: str, gorev: str, key):
        self.orch, self.agent, self.gorev, self.key = orch, agent, gorev, key
        self.buf: list[str] = []
        self.last = 0.0
        self.started = False

    def add(self, text: str) -> None:
        if not text:
            return
        if not self.started:
            self.started = True
            self.orch._chunk_started(self.agent, self.key)
        self.buf.append(text)
        if time.monotonic() - self.last >= CHUNK_INTERVAL_S:
            self.flush()

    def flush(self) -> None:
        text = "".join(self.buf)
        self.buf.clear()
        while text:
            self.orch.emit("parca", persist=False, ajan=self.agent, gorev=self.gorev, metin=text[:CHUNK_MAX_CHARS])
            text = text[CHUNK_MAX_CHARS:]
        self.last = time.monotonic()


# ====================================================================== 2. Orchestrator

class Orchestrator:
    def __init__(self, config: Config, agents: dict[str, Agent], bus: EventBus, workspace: Path,
                 approver: Approver | None = None, *, run_dir: Path | None = None,
                 state: RunState | None = None, resume_plans: list[Plan] | None = None,
                 memory: ProjectMemory | None = None):
        self.config = config
        self.settings = config.settings
        self.agents = agents
        self.bus = bus
        self.workspace = Path(workspace)
        self.approver = approver or CliApprover()
        self.run_dir = Path(run_dir) if run_dir else None
        self.state = state
        self.resume_plans = list(resume_plans or [])
        self.memory = memory
        self.notes: list[dict] = state.notlar if state else []

        self.calls = 0
        self.verify_runs = 0
        self._base_calls = state.toplam_cagri if state else 0
        self._base_verify = state.toplam_dogrulama if state else 0

        self._sems = {name: asyncio.Semaphore(a.cfg.max_parallel) for name, a in agents.items()}
        self.write_lock = asyncio.Lock()
        self.tracker = ChangeTracker(self.workspace, config.guard.protected_paths, config.guard.max_snapshot_files)
        self._tracker_ready = False
        self._init_lock = asyncio.Lock()

        self._resume_evt = asyncio.Event()
        self._resume_evt.set()
        self.paused = False
        self.stopped = False
        self._violation_lock = asyncio.Lock()
        self._main_task: asyncio.Task | None = None

        self._agent_calls: dict[str, dict] = {n: {} for n in agents}
        self._unreported: dict[str, list[str]] = {}
        self._phase_files: dict[str, tuple[str, str]] = {}
        self._issues: list[str] = []
        self._consult_seq = 0
        self._budget_hit = False

        # Sürüm 0.3
        self._rw = RWLock()                                   # sıkı mod
        self._exhausted: set[str] = set()                     # bu çalıştırmada kullanılamaz ajanlar
        self._override: dict[str, str] = {}                   # görev → devredilen ajan
        self._sessions: dict[tuple[str, str], str] = {}       # (görev, ajan) → oturum kimliği
        self.usage: dict[str, dict] = {}                      # ajan → toplam kullanım
        self._last_edit: Plan | None = None
        self.start_checkpoint: str | None = None
        store = CheckpointStore(self.workspace, self.settings.runs_dir) if self.settings.checkpoints else None
        self.checkpoints = store if store is not None and store.available else None

    @property
    def _cfgs(self):
        return [a.cfg for a in self.agents.values()]

    def emit(self, tur: str, *, persist: bool = True, **veri) -> dict:
        return self.bus.publish(tur, persist=persist, **veri)

    def _save_file(self, name: str, text: str) -> None:
        if self.run_dir is not None:
            (self.run_dir / name).write_text(redact(text or ""), encoding="utf-8")

    def _save_state(self) -> None:
        if self.run_dir is None or self.state is None:
            return
        self.state.toplam_cagri = self._base_calls + self.calls
        self.state.toplam_dogrulama = self._base_verify + self.verify_runs
        self.state.notlar = self.notes
        save_state(self.run_dir, self.state)

    async def _ensure_tracker(self) -> None:
        if self._tracker_ready:
            return
        async with self._init_lock:
            if not self._tracker_ready:
                await self.tracker.start()
                self._tracker_ready = True

    async def _ask(self, kind: str, payload: dict, validate=None) -> str:
        """Onay ister. Plan onayında kullanıcı planı düzenlediyse doğrulanmış plan self._last_edit'e konur."""
        istek_id = secrets.token_hex(6)
        # Not: olay sözlüğünde 'tur' anahtarı olay türüne ayrıldığı için istek türü 'istek_turu' adıyla taşınır.
        self.emit("onay_istegi", istek_id=istek_id, istek_turu=kind, veri=payload)
        karar = await self.approver.request(kind, payload, istek_id, validate=validate)
        self._last_edit = self.approver.pop_edit(istek_id)
        self.emit("onay_cevabi", istek_id=istek_id, karar=karar, duzenlendi=self._last_edit is not None)
        return karar

    # ------------------------------------------------------------------ devretme (sürüm 0.3)

    def _usable(self, name: str) -> bool:
        return name in self.agents and name not in self._exhausted

    @property
    def _writers(self) -> set[str]:
        return {n for n, a in self.agents.items() if a.cfg.can_write}

    def _alternative(self, failed: str, tried: set[str], need_write: bool = False) -> str | None:
        if not self.settings.fallback:
            return None
        order = [self.settings.planner] + [n for n in self.agents if n != self.settings.planner]
        return next((n for n in order if n != failed and n not in tried and self._usable(n)
                     and (not need_write or n in self._writers)), None)

    def effective_assignee(self, task: Task) -> str:
        return self._override.get(task.id, task.assignee)

    def _handover(self, old: str, new: str, task: Task | None, role: str, reason: str) -> None:
        if task is not None and role == "işçi":
            self._override[task.id] = new
        self.emit("devretme", gorev=task.id if task else "-", rol=role, eski=old, yeni=new, neden=reason)

    async def _call_fb(self, agent: str, build, *, role: str, tag: str, task: Task | None = None,
                       write: bool = False) -> tuple[str, str]:
        """call() + devretme. build(ajan) o ajana göre istemi üretir. (çıktı, kullanılan ajan) döndürür."""
        tried: set[str] = set()
        while True:
            if not self._usable(agent):
                alt = self._alternative(agent, tried, need_write=write)
                if alt is None:
                    raise AgentError(f"{agent}: bu çalıştırmada kullanılamıyor ve devredilecek ajan yok.")
                self._handover(agent, alt, task, role, "ajan daha önce kullanılamaz işaretlendi")
                agent = alt
            tried.add(agent)
            session_key = (task.id, agent) if task is not None and role == "işçi" else None
            try:
                out = await self.call(agent, build(agent), role=role, tag=tag, task=task, write=write,
                                      session_key=session_key)
                return out, agent
            except AgentError as e:
                if QUOTA_RE.search(str(e)):
                    self._exhausted.add(agent)
                alt = self._alternative(agent, tried, need_write=write)
                if alt is None:
                    raise
                self._handover(agent, alt, task, role, redact(str(e))[:300])
                agent = alt

    # ================================================================== 3. Durum olayları

    def _agent_status(self, agent: str, key, durum: str, rol: str = "", gorev: str = "", detay: str = "") -> None:
        """ajan_durumu yayınlar. Ajanın birden fazla çağrısı varsa en son başlayanın durumu gösterilir."""
        calls = self._agent_calls.setdefault(agent, {})
        if durum in ("bosta", "hata"):
            calls.pop(key, None)
        else:
            calls[key] = (durum, rol, gorev, detay)
        if durum == "bosta" and calls:
            durum, rol, gorev, detay = calls[list(calls)[-1]]
        self.emit("ajan_durumu", ajan=agent, durum=durum, rol=rol, gorev=gorev, detay=detay, aktif=len(calls))

    def _chunk_started(self, agent: str, key) -> None:
        cur = self._agent_calls.get(agent, {}).get(key)
        if cur and cur[0] == "dusunuyor":
            self._agent_status(agent, key, "yaziyor", cur[1], cur[2], cur[3])

    def _task_status(self, task: Task, durum: str) -> None:
        self.emit("gorev_durumu", gorev=task.id, ajan=task.assignee, durum=durum)

    def _emit_budget(self) -> None:
        self.emit("butce", kullanilan=self.calls, sinir=self.settings.max_calls,
                  dogrulama_kullanilan=self.verify_runs, dogrulama_sinir=self.config.verify.max_verify_runs)

    # ================================================================== 4. Kullanıcı kontrolü
    # Bu metotlar motorun döngüsünde çağrılmalıdır (web sunucusu loop.call_soon_threadsafe kullanır).

    def pause(self) -> None:
        if not self.paused and not self.stopped:
            self.paused = True
            self._resume_evt.clear()
            self.emit("durum", durum="duraklatildi")

    def resume(self) -> None:
        if self.paused and not self.stopped:
            self.paused = False
            self._resume_evt.set()
            self.emit("durum", durum="devam_ediyor")

    def stop(self) -> None:
        """Çalışan görevler iptal edilir (süreç ağaçları sonlandırılır), durum kaydedilir. Sentez yapılmaz."""
        if self.stopped:
            return
        self.stopped = True
        self._resume_evt.set()
        if self._main_task is not None and not self._main_task.done():
            self._main_task.cancel()

    def add_note(self, metin: str, hedef: str | None = None) -> dict:
        if not isinstance(metin, str) or not metin.strip():
            raise ValueError("Not boş olamaz.")
        if len(metin) > MAX_NOTE_CHARS:
            raise ValueError(f"Not en fazla {MAX_NOTE_CHARS} karakter olabilir.")
        if len(self.notes) >= MAX_NOTES:
            raise ValueError(f"En fazla {MAX_NOTES} not eklenebilir.")
        if hedef is not None and hedef not in self.agents:
            raise ValueError(f"Bilinmeyen ajan: {str(hedef)[:40]}")
        note = {"metin": metin.strip(), "hedef": hedef}
        self.notes.append(note)
        self.emit("kullanici_notu", metin=note["metin"], hedef=hedef)
        self._save_state()
        return note

    async def _gate(self) -> None:
        """Duraklatılmışsa yeni çağrı başlatmadan bekler; durdurulmuşsa RunStopped fırlatır."""
        if self.stopped:
            raise RunStopped("Çalıştırma durduruldu.")
        await self._resume_evt.wait()
        if self.stopped:
            raise RunStopped("Çalıştırma durduruldu.")

    # ================================================================== 5. call()

    def _lock_for(self, write: bool):
        if self.settings.strict_guard:
            return self._rw.write() if write else self._rw.read()
        return self.write_lock if write else contextlib.nullcontext()

    async def call(self, agent_name: str, prompt: str, *, role: str, tag: str,
                   task: Task | None = None, write: bool = False,
                   session_key: tuple[str, str] | None = None) -> str:
        """Tek model çağrısı. Bütçe, eşzamanlılık, değişiklik denetimi ve olaylar burada uygulanır."""
        await self._gate()
        if self.calls >= self.settings.max_calls:
            raise BudgetExceeded(f"Çağrı bütçesi doldu ({self.settings.max_calls}).")
        await self._ensure_tracker()
        self.calls += 1
        n = self.calls
        gorev = task.id if task else tag
        self.emit("cagri", no=n, ajan=agent_name, rol=role, gorev=tag, yazma=write, istem=prompt)
        agent = self.agents[agent_name]
        chunks = _ChunkBuffer(self, agent_name, gorev, n)
        error: BaseException | None = None
        report: Report | None = None
        out = ""
        try:
            self._agent_status(agent_name, n, "sirada", role, gorev)
            # Semafor ve kilit çağrı başına alınır, görev boyunca tutulmaz: böylece bir işçi
            # başka bir ajana danışırken kilitlenme oluşmaz.
            async with self._sems[agent_name]:
                async with self._lock_for(write):
                    files = list(task.files) if (write and task) else None
                    if write:
                        busy, detay = "dosya_yaziyor", ", ".join(files or [])
                    else:
                        busy, detay = ("denetliyor" if role == "denetçi" else "dusunuyor"), ""
                    self._agent_status(agent_name, n, busy, role, gorev, detay)
                    self.tracker.begin(CallInfo(n, agent_name, gorev, write, files if write else None))
                    meta: dict = {}
                    session = self._sessions.get(session_key) if session_key else None
                    try:
                        out = await agent.run(prompt, write=write, workdir=self.workspace,
                                              tag=f"{tag}-{n}", on_chunk=chunks.add, session=session, meta=meta)
                    except BaseException as e:  # noqa: BLE001 - değişiklik denetimi her durumda yapılmalı
                        error = e
                    chunks.flush()
                    # Snapshot yazma kilidi içinde alınır: bir sonraki yazıcı başlamadan atıf netleşir.
                    try:
                        report = await self.tracker.end(n)
                    except GuardError as ge:
                        error = error or ge
            if error is not None:
                detail = "iptal" if isinstance(error, asyncio.CancelledError) else redact(str(error))[:300]
                self._agent_status(agent_name, n, "hata", role, gorev, detail)
                if report is not None:
                    await self._handle_report(report, ask=not isinstance(error, asyncio.CancelledError))
                raise error
        finally:
            if n in self._agent_calls.get(agent_name, {}):
                self._agent_status(agent_name, n, "bosta")
        self.emit("yanit", no=n, ajan=agent_name, rol=role, gorev=tag, metin=out, devam_oturumu=bool(session))
        if session_key and meta.get("session"):
            self._sessions[session_key] = meta["session"]
        self._record_usage(agent_name, meta.get("usage"))
        self._emit_budget()
        if report is not None:
            await self._handle_report(report)
        return out

    def _record_usage(self, agent: str, usage: dict | None) -> None:
        tot = self.usage.setdefault(agent, {"girdi": 0, "cikti": 0, "maliyet_usd": 0.0, "cagri": 0})
        tot["cagri"] += 1
        if usage:
            tot["girdi"] += usage.get("girdi") or 0
            tot["cikti"] += usage.get("cikti") or 0
            if usage.get("maliyet_usd") is not None:
                tot["maliyet_usd"] = round(tot["maliyet_usd"] + usage["maliyet_usd"], 6)
        self.emit("kullanim", ajan=agent, son=usage or {}, toplam={k: dict(v) for k, v in self.usage.items()})

    # ================================================================== 6. Değişiklik raporu ve ihlaller

    async def _handle_report(self, report: Report, *, ask: bool = True) -> None:
        ch = report.changes
        if ch:
            self.emit("dosya_degisikligi", gorev=report.task, ajan=report.agent,
                      eklenen=ch.added[:MAX_EVENT_PATHS], degisen=ch.modified[:MAX_EVENT_PATHS],
                      silinen=ch.removed[:MAX_EVENT_PATHS], bildirilmemis=report.unreported[:MAX_EVENT_PATHS])
            w = report.writer
            if w is not None and w.files is not None:     # ajan yazma çağrısı (doğrulama değil)
                for path in ch.added + ch.modified:
                    self._phase_files[path] = (w.task, w.agent)
                for path in ch.removed:
                    self._phase_files.pop(path, None)
                if report.unreported:
                    lst = self._unreported.setdefault(w.task, [])
                    lst.extend(p for p in report.unreported if p not in lst)
        if not report.violations:
            return
        yollar = [p for p, _ in report.violations][:MAX_EVENT_PATHS]
        neden = "; ".join(sorted({r for _, r in report.violations}))
        self.emit("korumali_ihlal", gorev=report.task, ajan=report.agent, yollar=yollar, neden=neden)
        self._issues.append(f"İhlal ({report.agent}, {report.task}): {neden}: {', '.join(yollar[:10])}")
        if ask:
            await self._violation_decision(report, yollar, neden)

    async def _violation_decision(self, report: Report, yollar: list[str], neden: str) -> None:
        async with self._violation_lock:
            if self.stopped:
                raise RunStopped("Çalıştırma durduruldu.")
            was_paused = self.paused
            self._resume_evt.clear()          # karar verilene kadar yeni çağrı başlatılmaz
            karar = await self._ask("ihlal", {"ajan": report.agent, "gorev": report.task,
                                              "yollar": yollar, "neden": neden})
            if karar == "devam":
                if not was_paused:
                    self._resume_evt.set()
                return
            self.stopped = True
            self._resume_evt.set()
            raise RunStopped("Korumalı yol ihlali nedeniyle çalıştırma durduruldu. "
                             "Orkestra değişiklikleri geri almadı; gerekirse git ile geri al.")

    # ================================================================== 7. Planlama

    def _memory_text(self) -> str:
        return self.memory.render() if self.memory and not self.memory.empty else ""

    async def make_plan(self, goal: str, *, phase: int | None = None, prev_results: str = "") -> Plan:
        s = self.settings
        # İstem, duraklatma beklendikten sonra kurulur: duraklatılmışken eklenen notlar da istemde yer alır.
        await self._gate()
        await self._ensure_tracker()
        names = list(self.agents)
        verify_names = [c.name for c in self.config.verify.commands]
        protected = self.config.guard.protected_paths
        common = dict(verify_cmds=self.config.verify.commands, protected=protected,
                      tree=self.tracker.tree(), memory=self._memory_text(), notes=self.notes)

        def build(agent: str) -> str:
            if phase is None:
                return prompts.planner(goal, self._cfgs, agent, s.max_tasks, s.language, **common)
            return prompts.phase_planner(goal, self._cfgs, agent, s.max_tasks, s.language, phase=phase,
                                         max_phases=s.max_phases, prev_results=prev_results, **common)

        def parse(text: str) -> Plan:
            return parse_plan(text, names, s.max_tasks, verify_names=verify_names, protected=protected,
                              writers=self._writers,
                              phased=phase is not None)

        out, used = await self._call_fb(s.planner, build, role="planlayıcı", tag="plan")
        try:
            plan = parse(out)
        except PlanError as e:
            self.emit("hata", uyari=True, mesaj=f"Plan geçersiz: {e} Planlayıcıdan bir kez düzeltme isteniyor.")
            out, _ = await self._call_fb(used, lambda a: prompts.planner_fix(build(a), out, str(e)),
                                         role="planlayıcı", tag="plan-duzeltme")
            plan = parse(out)  # ikinci hata dışarı fırlatılır
        self.emit("plan", plan=plan.to_dict(), asama=phase)
        return plan

    # ================================================================== 8. Yürütme

    async def execute(self, plan: Plan, goal: str, *, prior: dict[str, TaskResult] | None = None,
                      on_result=None) -> dict[str, TaskResult]:
        results: dict[str, TaskResult] = dict(prior or {})
        pending: dict[str, Task] = {t.id: t for t in plan.tasks if t.id not in results}
        running: dict[asyncio.Task, str] = {}
        budget_hit = False

        def finish(tid: str, res: TaskResult) -> None:
            results[tid] = res
            task = next(t for t in plan.tasks if t.id == tid)
            self.emit("gorev_sonucu", gorev=tid, durum=res.status, hata=res.error, uyarilar=res.uyarilar)
            self._task_status(task, _TASK_STATUS.get(res.status, res.status))
            if on_result:
                on_result(tid, res)

        try:
            while pending or running:
                # 8a. Zincirleme atlama
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
                # 8b. Hazır görevleri başlat
                for tid, t in list(pending.items()):
                    if all(d in results and results[d].status == "tamam" for d in t.depends_on):
                        del pending[tid]
                        running[asyncio.create_task(self.run_task(t, goal, results))] = tid
                if not running:
                    break
                # 8c. İlk biteni bekle
                done, _ = await asyncio.wait(running.keys(), return_when=asyncio.FIRST_COMPLETED)
                for fut in done:
                    tid = running.pop(fut)
                    try:
                        res = fut.result()
                    except BudgetExceeded as e:
                        budget_hit = self._budget_hit = True
                        res = TaskResult("hata", error=str(e))
                    except (AgentError, SecurityError, OSError) as e:
                        res = TaskResult("hata", error=redact(str(e)))
                    finish(tid, res)
        finally:
            # Durdurma, ihlal, beklenmeyen hata veya Ctrl+C: yarım kalan görevler iptal edilir;
            # CliAgent iptalde süreç ağacını sonlandırır.
            for fut in running:
                fut.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)

        for tid in list(pending):   # döngüsüz bir planda olmamalı; güvenli tarafta kalınır
            finish(tid, TaskResult("atlandı", error="Bağımlılıkları çözülemedi."))
        return results

    # ================================================================== 9. Görev

    def _truncate_dep(self, text: str) -> str:
        limit = self.settings.dep_output_chars
        return text if len(text) <= limit else text[:limit] + "\n… (kısaltıldı)"

    async def run_task(self, task: Task, goal: str, results: dict[str, TaskResult]) -> TaskResult:
        s = self.settings
        self._task_status(task, "calisiyor")
        deps = {d: self._truncate_dep(results[d].output) for d in task.depends_on}
        warnings: list[str] = []
        output = await self.work(task, goal, deps)
        used = 0      # harcanan revizyon hakkı (otomatik doğrulama revizyonu veya denetçi revizyonu)
        while True:
            vres: list[VerifyResult] = []
            if task.verify:
                self._task_status(task, "dogrulamada")
                vres = await self._run_verifications(task, warnings)
            failed = [r for r in vres if not r.ok]
            if failed and used < s.max_revisions:
                # Doğrulama başarısız: denetçiye sorulmadan otomatik revizyon (bir hak harcar).
                used += 1
                feedback = "Doğrulama komutları başarısız oldu; hataları düzelt.\n\n" + "\n\n".join(
                    f"$ {r.name} (çıkış kodu {r.exit_code})\n{r.output[-3000:]}" for r in failed)
                self.emit("denetim", gorev=task.id, karar="revise", geri_bildirim=feedback, otomatik=True)
                self._task_status(task, "calisiyor")
                output = await self.work(task, goal, deps, feedback=feedback, previous=output)
                continue
            if failed:
                warnings.append("Doğrulama başarısız: " + ", ".join(r.name for r in failed))
            if not s.reviewer or used >= s.max_revisions:
                break
            self._task_status(task, "denetimde")
            review_prompt = prompts.review(goal, task, output, s.language, verify_results=vres,
                                           unreported=self._unreported.get(task.id, []))
            raw, _ = await self._call_fb(s.reviewer, lambda a: review_prompt, role="denetçi",
                                         tag=f"{task.id}-denetim", task=task)
            verdict, feedback = parse_review(raw)
            self.emit("denetim", gorev=task.id, karar=verdict, geri_bildirim=feedback, otomatik=False)
            if verdict == "approve":
                break
            used += 1
            self._task_status(task, "calisiyor")
            output = await self.work(task, goal, deps, feedback=feedback, previous=output)
        unrep = self._unreported.get(task.id)
        if unrep:
            warnings.append("Bildirilmemiş dosya değişiklikleri: " + ", ".join(unrep[:10]))
        return TaskResult("tamam", output, uyarilar=warnings)

    # ================================================================== 10. İşçi + danışma

    async def work(self, task: Task, goal: str, deps: dict[str, str],
                   feedback: str | None = None, previous: str | None = None) -> str:
        s = self.settings
        answers: list[dict] = []
        draft = previous
        for rnd in range(s.max_consult_rounds + 1):
            allow_ask = rnd < s.max_consult_rounds
            await self._gate()
            tree, memory = self.tracker.tree(), self._memory_text()

            def build(agent: str) -> str:
                return prompts.worker(goal, task, self._cfgs, deps, answers, draft, feedback, s.language,
                                      allow_ask=allow_ask, tree=tree, memory=memory, notes=self.notes,
                                      as_agent=agent)
            out, me = await self._call_fb(self.effective_assignee(task), build, role="işçi", tag=task.id,
                                          task=task, write=task.needs_write)
            asks = ASK_RE.findall(out)[:MAX_ASKS_PER_TURN]
            if not asks or not allow_ask:
                return strip_asks(out)
            draft = strip_asks(out) or draft
            for target, question in asks:
                question = question.strip()[:MAX_QUESTION_CHARS]
                if target not in self.agents or target == me:
                    answer = f"(Geçersiz hedef: {target})"
                elif not self._usable(target):
                    answer = f"({target} bu çalıştırmada kullanılamıyor; kendi değerlendirmenle devam et.)"
                else:
                    self._consult_seq += 1
                    key = ("danisma", self._consult_seq)
                    self.emit("danisma_basladi", gorev=task.id, soran=me, hedef=target, soru=question)
                    self._agent_status(me, key, "danisiyor", "işçi", task.id, target)
                    try:
                        # Danışman hiçbir zaman yazma izni almaz.
                        raw = await self.call(target, prompts.consult(goal, me, task, question, s.language),
                                              role="danışman", tag=f"{task.id}-danisma", task=task, write=False)
                    except AgentError as e:
                        if QUOTA_RE.search(str(e)):
                            self._exhausted.add(target)
                        raw = f"({target} yanıt veremedi; kendi değerlendirmenle devam et.)"
                    finally:
                        self._agent_status(me, key, "bosta")
                    answer = strip_asks(raw)
                answers.append({"hedef": target, "soru": question, "cevap": answer})
                self.emit("danisma", gorev=task.id, soran=me, hedef=target, soru=question, cevap=answer)
        return strip_asks(draft or "")  # erişilmez: son turda allow_ask kapalıdır

    # ================================================================== 11. Doğrulama

    async def _run_verifications(self, task: Task, warnings: list[str]) -> list[VerifyResult]:
        vcfg = self.config.verify
        results: list[VerifyResult] = []
        for name in task.verify:
            cmd = vcfg.get(name)
            if cmd is None:
                continue
            if self.verify_runs >= vcfg.max_verify_runs:
                warnings.append(f"Doğrulama sınırı doldu; '{name}' çalıştırılmadı.")
                continue
            await self._gate()
            await self._ensure_tracker()
            self.verify_runs += 1
            key = -self.verify_runs   # doğrulama çalışmaları izleyicide negatif numarayla tutulur
            # Doğrulama yazma kilidiyle çalışır: testlerin ürettiği dosyalar bir ajana atfedilmez,
            # ama korumalı yollardaki değişiklikler yine ihlal sayılır.
            async with self._lock_for(True):
                self.tracker.begin(CallInfo(key, "dogrulama", task.id, True, None))
                try:
                    res = await run_verify(cmd, self.workspace, vcfg.env_passthrough, sandbox=vcfg.sandbox,
                                           image=vcfg.docker_image)
                finally:
                    report = await self.tracker.end(key)
            self.emit("dogrulama", gorev=task.id, komut=res.name, cikis_kodu=res.exit_code,
                      sure_sn=res.duration_s, cikti=res.output, basarili=res.ok)
            self._emit_budget()
            await self._handle_report(report)
            results.append(res)
        return results

    # ================================================================== 12. Sentez

    async def synthesize(self, goal: str, plan: Plan, results: dict[str, TaskResult], *,
                         incomplete: bool = False) -> str:
        s = self.settings
        text, _ = await self._call_fb(s.planner, lambda a: prompts.synthesis(goal, plan, results, s.language,
                                                                             incomplete=incomplete),
                                      role="sentez", tag="sentez")
        self.emit("sentez", metin=text)
        return text

    # ================================================================== 13. Üst akış

    async def run(self, goal: str) -> str:
        """Tüm çalıştırma: tarama → plan(lar) → yürütme → sentez. Sentez raporunu döndürür."""
        self._main_task = asyncio.current_task()
        if self.state is None:
            self.state = RunState(hedef=goal, mod=self.settings.mode, calisma_klasoru=str(self.workspace),
                                  notlar=self.notes)
        st = self.state
        resumed = bool(st.asamalar)
        self.emit("durum", durum="basladi")
        try:
            if resumed:
                kalan = sum(1 for ph, plan in zip(st.asamalar, self.resume_plans) if not ph.bitti
                            for t in plan.tasks if ph.sonuclar.get(t.id, {}).get("durum") != "tamam")
                self.emit("devam", onceki_calls=st.toplam_cagri, kalan_gorevler=kalan)
            await self._startup_checks()
            await self._checkpoint("devam başlangıcı" if resumed else f"başlangıç: {goal.strip().splitlines()[0][:80]}", 0,
                                   start=True)
            if self.memory is not None and not resumed:
                self.memory.add_goal(goal)
            self._save_state()
            if st.mod == "asamali":
                report = await self._run_phased(goal)
            else:
                report = await self._run_single(goal)
            self._save_file("sonuc.md", report)
            st.durum = "bitti"
            self._save_state()
            self.emit("durum", durum="bitti")
            return report
        except RunCancelled as e:
            st.durum = "iptal"
            self._save_state()
            self.emit("iptal", neden=str(e))
            raise
        except RunStopped:
            self._mark_stopped()
            raise
        except asyncio.CancelledError:
            self._mark_stopped()
            if self.stopped:          # arayüzden Durdur: iptal yutulur, RunStopped olarak bildirilir
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
                raise RunStopped("Çalıştırma kullanıcı tarafından durduruldu.") from None
            raise                      # Ctrl+C: asyncio.run KeyboardInterrupt'a çevirir
        except BaseException:
            st.durum = "hata"
            self._save_state()
            self.emit("durum", durum="hata")
            raise

    async def _checkpoint(self, etiket: str, asama: int, *, start: bool = False) -> None:
        """Çalışma klasörünün anlık kopyasını alır (git varsa). Hata çalıştırmayı durdurmaz."""
        if self.checkpoints is None:
            return
        try:
            cp = await asyncio.to_thread(self.checkpoints.create, etiket)
        except CheckpointError as e:
            self.emit("hata", uyari=True, mesaj=f"Kontrol noktası alınamadı: {e}")
            return
        if start:
            self.start_checkpoint = cp.sha
        if self.state is not None:
            self.state.kontrol.append({"sha": cp.sha, "etiket": cp.etiket, "asama": asama, "zaman": cp.zaman})
        self.emit("kontrol_noktasi", sha=cp.sha, etiket=cp.etiket, asama=asama, baslangic=start)

    def _mark_stopped(self) -> None:
        if self.state is not None:
            self.state.durum = "durduruldu"
        self._save_state()
        self.emit("durum", durum="durduruldu")

    async def _startup_checks(self) -> None:
        found = await asyncio.to_thread(scan_startup, self.workspace)
        if found:
            karar = await self._ask("baslangic_uyarisi", {"dosyalar": found})
            if karar != "onay":
                raise RunCancelled("Başlangıç uyarısı onaylanmadı.")
        await self._ensure_tracker()

    def _task_md(self, task: Task, res: TaskResult | None) -> str:
        lines = [f"# [{task.id}] {task.title}", "", f"- Ajan: {task.assignee}",
                 f"- Durum: {res.status if res else 'çalışmadı'}"]
        if res and res.uyarilar:
            lines.append("- Uyarılar: " + "; ".join(res.uyarilar))
        lines.append("")
        lines.append(res.output if res and res.status == "tamam" else f"Hata: {res.error if res else '-'}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _results_from_state(ph: PhaseState) -> dict[str, TaskResult]:
        return {tid: TaskResult(r["durum"], r.get("cikti", ""), r.get("hata", ""), list(r.get("uyarilar", [])))
                for tid, r in ph.sonuclar.items()}

    async def _run_phase_tasks(self, k: int, plan: Plan, ph: PhaseState, goal: str,
                               prefix: str) -> tuple[dict[str, TaskResult], bool, Plan]:
        """Aşamanın tamamlanmamış görevlerini çalıştırır. (sonuçlar, çalıştırıldı_mı, plan) döndürür.

        Kullanıcı onay sırasında planı düzenlediyse düzenlenmiş plan geri döner.
        """
        prior = {tid: r for tid, r in self._results_from_state(ph).items() if r.status == "tamam"}
        remaining = [t for t in plan.tasks if t.id not in prior]
        if not remaining:
            return prior, False, plan
        for t in plan.tasks:
            self._task_status(t, "tamam" if t.id in prior else "bekliyor")
        payload = {
            "plan": plan.to_dict(), "asama": k,
            "has_write": any(t.needs_write for t in remaining),
            "verify_commands": [{"name": c.name, "argv": c.argv} for c in
                                (self.config.verify.get(v) for v in plan.verify_names) if c],
        }
        phased = bool(self.state and self.state.mod == "asamali")
        s = self.settings

        def validate(d: dict) -> Plan:
            # Kullanıcının düzenlediği plan da aynı sıkı kurallarla doğrulanır (kural 9).
            return parse_plan(json.dumps(d, ensure_ascii=False), list(self.agents), s.max_tasks, writers=self._writers,
                              verify_names=[c.name for c in self.config.verify.commands],
                              protected=self.config.guard.protected_paths, phased=phased)

        if await self._ask("plan", payload, validate=validate) != "onay":
            raise RunCancelled("Kullanıcı planı onaylamadı.")
        if self._last_edit is not None:
            plan = self._last_edit
            ph.plan = plan.to_dict()
            prior = {tid: r for tid, r in prior.items() if any(t.id == tid for t in plan.tasks)}
            ph.sonuclar = {tid: r for tid, r in ph.sonuclar.items() if tid in prior}
            remaining = [t for t in plan.tasks if t.id not in prior]
            self._save_file(f"plan-a{k}.json" if phased else "plan.json",
                            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
            self.emit("plan", plan=plan.to_dict(), asama=k if phased else None, duzenlendi=True)
            self._save_state()
        for t in remaining:
            self._task_status(t, "atandi")

        def on_result(tid: str, res: TaskResult) -> None:
            ph.sonuclar[tid] = {"durum": res.status, "cikti": res.output, "hata": res.error,
                                "uyarilar": res.uyarilar}
            self._save_state()

        results = await self.execute(plan, goal, prior=prior, on_result=on_result)
        if self._budget_hit:
            # Bütçe dolduysa aşama "bitti" sayılmaz: durum kaydedilir ve 'devam' kalan görevleri tamamlar.
            for t in plan.tasks:
                self._save_file(f"gorev-{prefix}{t.id}.md", self._task_md(t, results.get(t.id)))
            self._save_state()
            raise BudgetExceeded(f"Çağrı bütçesi doldu ({self.settings.max_calls}). Kalan görevler için: "
                                 f"python -m orkestra devam {self.run_dir or '<run_klasoru>'}")
        return results, True, plan

    async def _finish_phase(self, k: int, plan: Plan, ph: PhaseState, results: dict[str, TaskResult],
                            prefix: str) -> None:
        ph.bitti = True
        for t in plan.tasks:
            self._save_file(f"gorev-{prefix}{t.id}.md", self._task_md(t, results.get(t.id)))
        await self._update_memory(k, plan, results)
        await self._checkpoint(f"aşama {k} bitti: {(plan.phase_title or plan.summary)[:80]}", k)
        self._save_state()
        if self.state and self.state.mod == "asamali":
            self.emit("asama", no=k, baslik=plan.phase_title, durum="bitti")

    async def _update_memory(self, k: int, plan: Plan, results: dict[str, TaskResult]) -> None:
        if self.memory is None:
            return
        m = self.memory
        m.add_decision(k, plan.summary)
        for path, (task_id, agent) in sorted(self._phase_files.items()):
            m.record_file(path, task_id, agent, k)
        self._phase_files = {}
        for t in plan.tasks:
            r = results.get(t.id)
            if r is None:
                continue
            if r.status == "tamam":
                m.add_completed(k, t.id, t.title, r.output)
            else:
                m.add_issue(f"[A{k} {t.id}] {t.title}: {r.status} — {r.error[:200]}")
            for w in r.uyarilar:
                m.add_issue(f"[A{k} {t.id}] {w}")
        for issue in self._issues:
            m.add_issue(issue)
        self._issues = []
        written = await asyncio.to_thread(m.save)
        for rel in written:
            await self.tracker.accept_own_write(rel)

    async def _run_single(self, goal: str) -> str:
        st = self.state
        if st.asamalar:
            ph, plan = st.asamalar[0], self.resume_plans[0]
        else:
            plan = await self.make_plan(goal)
            ph = PhaseState(no=1, plan=plan.to_dict())
            st.asamalar.append(ph)
            self._save_file("plan.json", json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
            self._save_state()
        # Devamda "hata" ve "atlandı" görevler de yeniden çalıştırılır (bölüm 9.6).
        results, ran, plan = await self._run_phase_tasks(1, plan, ph, goal, "")
        if ran or not ph.bitti:
            await self._finish_phase(1, plan, ph, results, "")
        return await self.synthesize(goal, plan, results)

    @staticmethod
    def _phase_summary(plan: Plan, results: dict[str, TaskResult]) -> str:
        rows = [f"Aşama: {plan.phase_title or '-'} — {plan.summary}"]
        for t in plan.tasks:
            r = results.get(t.id)
            status = r.status if r else "çalışmadı"
            rows.append(f"[{t.id}] {t.title} ({t.assignee}) — {status}")
            if r and r.status == "tamam":
                rows.append("  " + " ".join(r.output.split())[:1500])
            elif r and r.error:
                rows.append(f"  Hata: {r.error[:500]}")
            for w in (r.uyarilar if r else []):
                rows.append(f"  Uyarı: {w}")
        return "\n".join(rows)

    async def _run_phased(self, goal: str) -> str:
        s, st = self.settings, self.state
        all_plans: list[tuple[int, Plan, dict[str, TaskResult]]] = []
        prev_text = ""
        done = False
        k = 1
        # Kayıtlı aşamalar (devam): yarım kalan aşama ve son aşamanın tamamlanmamış görevleri çalıştırılır.
        # Daha önce bitmiş aşamalardaki başarısız görevler, sonraki aşamaların planlayıcısına iletilmiştir.
        last_no = st.asamalar[-1].no if st.asamalar else 0
        for ph, plan in zip(st.asamalar, self.resume_plans):
            results = self._results_from_state(ph)
            if not ph.bitti or ph.no == last_no:
                self.emit("asama", no=ph.no, baslik=plan.phase_title, durum="calisiyor")
                results, ran, plan = await self._run_phase_tasks(ph.no, plan, ph, goal, f"a{ph.no}-")
                if ran or not ph.bitti:
                    await self._finish_phase(ph.no, plan, ph, results, f"a{ph.no}-")
            all_plans.append((ph.no, plan, results))
            prev_text = self._phase_summary(plan, results)
            k = ph.no + 1
            done = plan.done
        incomplete = False
        while not done:
            if k > s.max_phases:
                incomplete = True
                break
            self.emit("asama", no=k, baslik="", durum="planlaniyor")
            plan = await self.make_plan(goal, phase=k, prev_results=prev_text)
            ph = PhaseState(no=k, plan=plan.to_dict())
            st.asamalar.append(ph)
            self._save_file(f"plan-a{k}.json", json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
            self._save_state()
            self.emit("asama", no=k, baslik=plan.phase_title, durum="calisiyor")
            results, _, plan = await self._run_phase_tasks(k, plan, ph, goal, f"a{k}-")
            await self._finish_phase(k, plan, ph, results, f"a{k}-")
            all_plans.append((k, plan, results))
            prev_text = self._phase_summary(plan, results)
            done = plan.done
            k += 1

        # Sentez: tüm aşamaların görevleri tek plan gibi verilir (kimlikler aşama önekiyle).
        tasks: list[Task] = []
        merged: dict[str, TaskResult] = {}
        for no, plan, results in all_plans:
            for t in plan.tasks:
                nid = f"a{no}-{t.id}"
                tasks.append(dataclasses.replace(t, id=nid, depends_on=[f"a{no}-{d}" for d in t.depends_on]))
                if t.id in results:
                    merged[nid] = results[t.id]
        summary = " / ".join(f"Aşama {no}: {p.phase_title or p.summary[:80]}" for no, p, _ in all_plans)
        combined = Plan(summary=summary or "Aşamalı çalıştırma", tasks=tasks)
        return await self.synthesize(goal, combined, merged, incomplete=incomplete)
