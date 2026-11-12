"""Durum kaydı, kaldığı yerden devam ve proje hafızası (bölüm 9.2, 9.6, kural 26).

Bölümler:
  1. RunState: durum.json yapısı, atomik yazma ve sıkı yükleme
  2. ProjectMemory: <workspace>/.orkestra/proje_hafizasi.md
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config
from .events import redact_value
from .planner import Plan, PlanError, parse_plan
from .security import SecurityError, make_private_dir, validate_layout, validate_workspace

STATE_VERSION = 2
STATE_FILE = "durum.json"
RUN_NAME_RE = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]+)?$")
RESULT_STATUSES = {"tamam", "hata", "atlandı"}
RUN_STATUSES = {"calisiyor", "bitti", "durduruldu", "hata", "iptal"}


class StateError(Exception):
    """durum.json okunamadı veya geçersiz."""


# ====================================================================== 1. RunState

@dataclass
class PhaseState:
    no: int
    plan: dict
    sonuclar: dict[str, dict] = field(default_factory=dict)   # görev id → {durum, cikti, hata, uyarilar}
    bitti: bool = False


@dataclass
class RunState:
    hedef: str
    mod: str
    calisma_klasoru: str
    asamalar: list[PhaseState] = field(default_factory=list)
    notlar: list[dict] = field(default_factory=list)
    toplam_cagri: int = 0
    toplam_dogrulama: int = 0
    durum: str = "calisiyor"
    surum: int = STATE_VERSION
    kontrol: list[dict] = field(default_factory=list)      # sürüm 0.3: kontrol noktaları

    def to_dict(self) -> dict:
        return {
            "surum": self.surum, "hedef": self.hedef, "mod": self.mod,
            "calisma_klasoru": self.calisma_klasoru, "asama": len(self.asamalar),
            "asamalar": [{"no": a.no, "plan": a.plan, "sonuclar": a.sonuclar, "bitti": a.bitti}
                         for a in self.asamalar],
            "notlar": self.notlar, "toplam_cagri": self.toplam_cagri,
            "toplam_dogrulama": self.toplam_dogrulama, "durum": self.durum,
            "kontrol_noktalari": self.kontrol,
        }


def save_state(run_dir: Path, state: RunState) -> Path:
    """Atomik yazma: önce geçici dosya, sonra os.replace."""
    path = Path(run_dir) / STATE_FILE
    tmp = path.with_name(STATE_FILE + ".tmp")
    data = json.dumps(redact_value(state.to_dict()), ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def _req(d: dict, key: str, typ, where: str = "durum.json"):
    v = d.get(key)
    if typ is int and isinstance(v, bool):
        raise StateError(f"{where}: '{key}' alanı geçersiz.")
    if not isinstance(v, typ):
        raise StateError(f"{where}: '{key}' alanı eksik veya geçersiz.")
    return v


def _nonneg_int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def load_state(run_dir: Path, cfg: Config) -> tuple[RunState, list[Plan]]:
    """durum.json'u yükler ve her şeyi geçerli config'e göre yeniden doğrular (kural 26)."""
    run_dir = Path(run_dir)
    if not RUN_NAME_RE.match(run_dir.name):
        raise StateError(f"Geçersiz çalıştırma klasörü adı: {run_dir.name}")
    try:
        raw = json.loads((run_dir / STATE_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise StateError(f"{run_dir / STATE_FILE} bulunamadı.") from e
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise StateError(f"durum.json okunamadı: {e}") from e
    if not isinstance(raw, dict):
        raise StateError("durum.json bir JSON nesnesi olmalı.")
    if raw.get("surum") != STATE_VERSION:
        raise StateError(f"durum.json sürümü desteklenmiyor: {raw.get('surum')!r}")

    goal = _req(raw, "hedef", str)
    if not goal.strip() or len(goal) > 20000:
        raise StateError("durum.json: hedef geçersiz.")
    mode = raw.get("mod")
    if mode not in ("tek", "asamali"):
        raise StateError("durum.json: 'mod' geçersiz.")
    try:
        ws = validate_workspace(Path(_req(raw, "calisma_klasoru", str)))
        validate_layout(ws, cfg.settings.runs_dir, Path(__file__).resolve().parent.parent, cfg.dir)
    except SecurityError as e:
        raise StateError(f"durum.json: çalışma klasörü reddedildi: {e}") from e

    notes = _req(raw, "notlar", list)
    for n in notes:
        if not (isinstance(n, dict) and isinstance(n.get("metin"), str) and len(n["metin"]) <= 2000
                and (n.get("hedef") is None or n.get("hedef") in cfg.agents)):
            raise StateError("durum.json: geçersiz kullanıcı notu.")
    if len(notes) > 20:
        raise StateError("durum.json: çok fazla kullanıcı notu.")

    phased = mode == "asamali"
    phases: list[PhaseState] = []
    plans: list[Plan] = []
    raw_phases = _req(raw, "asamalar", list)
    if not phased and len(raw_phases) > 1:
        raise StateError("durum.json: tek modda birden fazla aşama olamaz.")
    if len(raw_phases) > cfg.settings.max_phases:
        raise StateError("durum.json: aşama sayısı max_phases değerini aşıyor.")
    for i, rp in enumerate(raw_phases, 1):
        where = f"durum.json aşama {i}"
        if not isinstance(rp, dict) or rp.get("no") != i:
            raise StateError(f"{where}: geçersiz aşama kaydı.")
        plan_dict = _req(rp, "plan", dict, where)
        try:
            plan = parse_plan(json.dumps(plan_dict, ensure_ascii=False), list(cfg.agents),
                              cfg.settings.max_tasks, verify_names=[c.name for c in cfg.verify.commands],
                              protected=cfg.guard.protected_paths, phased=phased,
                              writers={n for n, a in cfg.agents.items() if a.can_write})
        except PlanError as e:
            raise StateError(f"{where}: plan yeniden doğrulanamadı: {e}") from e
        ids = {t.id for t in plan.tasks}
        results = _req(rp, "sonuclar", dict, where)
        clean: dict[str, dict] = {}
        for tid, r in results.items():
            if tid not in ids or not isinstance(r, dict) or r.get("durum") not in RESULT_STATUSES:
                raise StateError(f"{where}: geçersiz görev sonucu: {str(tid)[:40]!r}")
            uy = r.get("uyarilar", [])
            if not (isinstance(r.get("cikti", ""), str) and isinstance(r.get("hata", ""), str)
                    and isinstance(uy, list) and all(isinstance(x, str) for x in uy)):
                raise StateError(f"{where}: görev sonucu alanları geçersiz: {tid}")
            dg = r.get("dogrulama", [])
            if not (isinstance(dg, list) and all(isinstance(x, str) for x in dg)):
                dg = []
            clean[tid] = {"durum": r["durum"], "cikti": r.get("cikti", ""), "hata": r.get("hata", ""),
                          "uyarilar": uy, "dogrulama": dg}
        bitti = rp.get("bitti", False)
        if not isinstance(bitti, bool):
            raise StateError(f"{where}: 'bitti' geçersiz.")
        phases.append(PhaseState(no=i, plan=plan.to_dict(), sonuclar=clean, bitti=bitti))
        plans.append(plan)

    kontrol = []
    raw_k = raw.get("kontrol_noktalari", [])
    if not isinstance(raw_k, list):
        raise StateError("durum.json: 'kontrol_noktalari' bir liste olmalı.")
    for k in raw_k[-200:]:
        if not (isinstance(k, dict) and isinstance(k.get("sha"), str) and re.fullmatch(r"[0-9a-f]{40}", k["sha"])
                and isinstance(k.get("etiket", ""), str)):
            raise StateError("durum.json: geçersiz kontrol noktası kaydı.")
        kontrol.append({"sha": k["sha"], "etiket": k.get("etiket", "")[:200], "asama": _nonneg_int(k.get("asama", 0)),
                        "zaman": _nonneg_int(k.get("zaman", 0))})

    st = RunState(hedef=goal, mod=mode, calisma_klasoru=str(ws), asamalar=phases,
                  notlar=[{"metin": n["metin"], "hedef": n.get("hedef")} for n in notes],
                  toplam_cagri=_nonneg_int(_req(raw, "toplam_cagri", int)),
                  toplam_dogrulama=_nonneg_int(raw.get("toplam_dogrulama", 0)),
                  durum=raw.get("durum") if raw.get("durum") in RUN_STATUSES else "calisiyor",
                  kontrol=kontrol)
    return st, plans


# ====================================================================== 2. Proje hafızası

MEMORY_DIR = ".orkestra"
MEMORY_MD = f"{MEMORY_DIR}/proje_hafizasi.md"
# Ek karar: Markdown'ı geri ayrıştırmak kırılgan olacağından hafızanın yapılandırılmış hali
# aynı korumalı klasörde JSON olarak da tutulur. Ajanlar ikisini de değiştiremez (kural 16).
MEMORY_JSON = f"{MEMORY_DIR}/proje_hafizasi.json"
MEMORY_LIMIT = 16000
MAX_ISSUES = 50


class ProjectMemory:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.goals: list[str] = []
        self.decisions: list[str] = []
        self.files: dict[str, str] = {}
        self.completed: list[str] = []
        self.issues: list[str] = []

    @classmethod
    def load(cls, workspace: Path) -> "ProjectMemory":
        m = cls(workspace)
        try:
            data = json.loads((m.workspace / MEMORY_JSON).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return m
        if not isinstance(data, dict):
            return m

        def strs(key):
            v = data.get(key, [])
            return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

        m.goals, m.decisions = strs("hedefler"), strs("kararlar")
        m.completed, m.issues = strs("tamamlanan"), strs("sorunlar")
        f = data.get("dosyalar", {})
        if isinstance(f, dict):
            m.files = {k: v for k, v in f.items() if isinstance(k, str) and isinstance(v, str)}
        return m

    @property
    def empty(self) -> bool:
        return not (self.goals or self.decisions or self.files or self.completed or self.issues)

    def summary(self) -> str:
        return (f"{len(self.goals)} hedef, {len(self.files)} dosya, {len(self.completed)} tamamlanan görev, "
                f"{len(self.issues)} açık sorun")

    # -- güncelleme
    def add_goal(self, goal: str) -> None:
        self.goals.append(f"{datetime.now():%Y-%m-%d %H:%M} — {goal.strip()[:500]}")

    def add_decision(self, phase: int, summary: str) -> None:
        self.decisions.append(f"Aşama {phase}: {summary.strip()[:1000]}")

    def record_file(self, path: str, task: str, agent: str, phase: int) -> None:
        self.files[path] = f"{task} · {agent} · aşama {phase}"

    def add_completed(self, phase: int, task_id: str, title: str, output: str) -> None:
        one = " ".join(output.split())[:300]
        self.completed.append(f"[A{phase} {task_id}] {title}: {one}")

    def add_issue(self, text: str) -> None:
        self.issues.append(text[:500])
        self.issues = self.issues[-MAX_ISSUES:]

    # -- çıktı
    def render(self) -> str:
        completed = list(self.completed)
        while True:
            text = self._render(completed)
            if len(text) <= MEMORY_LIMIT:
                return text
            # Sınır aşılırsa önce en eski görev özetleri kısaltılır, sonra çıkarılır.
            idx = next((i for i, c in enumerate(completed) if len(c) > 80), None)
            if idx is not None:
                completed[idx] = completed[idx][:77] + "…"
            elif completed:
                completed.pop(0)
            else:
                return text[:MEMORY_LIMIT]

    def _render(self, completed: list[str]) -> str:
        def section(title, items):
            body = "\n".join(f"- {x}" for x in items) if items else "- (yok)"
            return f"## {title}\n\n{body}\n"
        files = [f"{k} → {v}" for k, v in sorted(self.files.items())]
        return "\n".join([
            "# Proje hafızası\n",
            "> Bu dosyayı yalnızca Orkestra yazar.\n",
            section("Hedefler", self.goals[-20:]),
            section("Mimari kararlar", self.decisions[-30:]),
            section("Dosya haritası", files[-300:]),
            section("Tamamlanan görevler", completed),
            section("Açık sorunlar", self.issues),
        ])

    def save(self) -> list[str]:
        """Dosyaları yazar ve yazılan göreli yolları döndürür."""
        make_private_dir(self.workspace / MEMORY_DIR)
        data = {"hedefler": self.goals, "kararlar": self.decisions, "dosyalar": self.files,
                "tamamlanan": self.completed, "sorunlar": self.issues}
        for rel, text in ((MEMORY_JSON, json.dumps(redact_value(data), ensure_ascii=False, indent=2)),
                          (MEMORY_MD, redact_value(self.render()))):
            p = self.workspace / rel
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, p)
        return [MEMORY_JSON, MEMORY_MD]
