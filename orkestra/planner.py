"""Plan/Task veri yapıları; plan ve denetim ayrıştırma (bölüm 6.4, 7.1, 7.2)."""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import asdict, dataclass, field

from .config import normalize_rel_path
from .security import is_safe_id


class PlanError(Exception):
    """Plan okunamadı veya geçersiz."""


@dataclass
class Task:
    id: str
    title: str
    assignee: str
    instructions: str
    expected_output: str
    depends_on: list[str] = field(default_factory=list)
    needs_write: bool = False
    files: list[str] = field(default_factory=list)
    verify: list[str] = field(default_factory=list)


@dataclass
class Plan:
    summary: str
    tasks: list[Task]
    phase_title: str = ""
    done: bool = False

    def to_dict(self) -> dict:
        d = {"summary": self.summary, "tasks": [asdict(t) for t in self.tasks]}
        if self.phase_title or self.done:
            d["phase_title"] = self.phase_title
            d["done"] = self.done
        return d

    @property
    def verify_names(self) -> list[str]:
        return list(dict.fromkeys(v for t in self.tasks for v in t.verify))

    @property
    def has_write(self) -> bool:
        return any(t.needs_write for t in self.tasks)


LIMITS = {"title": 200, "instructions": 6000, "expected_output": 1500, "summary": 2000}
MAX_FILES_PER_TASK = 100
MAX_FILE_CHARS = 300


def _key(path: str) -> str:
    # Karşılaştırmalar büyük/küçük harfe duyarsızdır (Windows ve macOS dosya sistemleri için güvenli taraf).
    return path.rstrip("/").lower()


def paths_overlap(a: str, b: str) -> bool:
    """Eşitlik veya önek ilişkisi (src/ ile src/app.py) çakışma sayılır."""
    ka, kb = _key(a), _key(b)
    return ka == kb or kb.startswith(ka + "/") or ka.startswith(kb + "/")


def _parse_files(raw, where: str, needs_write: bool, protected) -> list[str]:
    if raw is None:
        raw = []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise PlanError(f"{where}: 'files' bir metin listesi olmalı.")
    if len(raw) > MAX_FILES_PER_TASK:
        raise PlanError(f"{where}: 'files' en fazla {MAX_FILES_PER_TASK} giriş içerebilir.")
    files: list[str] = []
    for item in raw:
        norm = normalize_rel_path(item) if len(item) <= MAX_FILE_CHARS else None
        if norm is None:
            raise PlanError(f"{where}: geçersiz dosya yolu: {item[:80]!r} "
                            f"(mutlak yol, sürücü harfi, '..' ve boş bileşen yasaktır).")
        for p in protected:
            if paths_overlap(norm, p):
                raise PlanError(f"{where}: '{norm}' korumalı bir yola giriyor: {p}")
        if norm not in files:
            files.append(norm)
    if needs_write and not files:
        raise PlanError(f"{where}: needs_write=true ise 'files' en az bir yol içermeli.")
    if not needs_write and files:
        raise PlanError(f"{where}: needs_write=false ise 'files' boş olmalı.")
    return files

_FENCE_RE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.S)


def extract_json(text: str):
    """Markdown kod çitini temizler ve JSON'u çözer; olmazsa ilk { ile son } arasını dener."""
    if not isinstance(text, str):
        raise PlanError("Yanıt metin değil.")
    s = text.strip()
    m = _FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise PlanError("Yanıtta geçerli JSON bulunamadı.")


def _text(obj: dict, key: str, where: str) -> str:
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        raise PlanError(f"{where}: '{key}' alanı boş olmayan bir metin olmalı.")
    return v.strip()[: LIMITS[key]]


AUTO_TEST = "python-test"


def _is_py_test(path: str) -> bool:
    """Python test dosyası (test_*.py, *_test.py) veya tests/ klasörü mü?"""
    parts = path.strip("/").split("/")
    name = parts[-1]
    if name == "tests":                       # klasörün tamamı (ör. "tests/")
        return True
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py") or "tests" in parts[:-1])


def parse_plan(text: str, agent_names, max_tasks: int, *, verify_names=(), protected=(),
               phased: bool = False, writers=None) -> Plan:
    """Planlayıcı çıktısını sıkı biçimde doğrular ve Plan döndürür.

    writers: dosya yazabilen ajanlar (None = hepsi). Yazamayan ajana yazma görevi verilemez.
    """
    data = extract_json(text)
    if not isinstance(data, dict):
        raise PlanError("Plan kökü bir JSON nesnesi olmalı.")
    summary = _text(data, "summary", "Plan")
    phase_title, done = "", False
    if phased:
        pt = data.get("phase_title", "")
        if not isinstance(pt, str):
            raise PlanError("Plan: 'phase_title' metin olmalı.")
        phase_title = pt.strip()[:200]
        done = data.get("done", False)
        if not isinstance(done, bool):
            raise PlanError("Plan: 'done' true veya false olmalı.")
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list):
        raise PlanError("Plan: 'tasks' bir liste olmalı.")
    if not raw_tasks and not (phased and done):
        raise PlanError("Plan: 'tasks' boş olamaz (yalnızca aşamalı modda done=true iken boş olabilir).")
    if len(raw_tasks) > max_tasks:
        raise PlanError(f"Plan: en fazla {max_tasks} görev olabilir, {len(raw_tasks)} görev verildi.")

    names = set(agent_names)
    tasks: list[Task] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_tasks, 1):
        where = f"{i}. görev"
        if not isinstance(raw, dict):
            raise PlanError(f"{where}: bir JSON nesnesi olmalı.")
        tid = raw.get("id")
        if not isinstance(tid, str):
            raise PlanError(f"{where}: 'id' metin olmalı.")
        tid = tid.strip().lower()
        if not is_safe_id(tid):
            raise PlanError(f"{where}: güvensiz görev kimliği: {tid[:50]!r}")
        if tid in seen:
            raise PlanError(f"Görev '{tid}': kimlik birden fazla kez kullanılmış.")
        seen.add(tid)
        where = f"Görev '{tid}'"
        assignee = raw.get("assignee")
        if not isinstance(assignee, str) or assignee.strip().lower() not in names:
            raise PlanError(f"{where}: bilinmeyen ajan: {str(assignee)[:50]!r}")
        deps = raw.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise PlanError(f"{where}: 'depends_on' bir metin listesi olmalı.")
        needs_write = raw.get("needs_write", False)
        if not isinstance(needs_write, bool):
            raise PlanError(f"{where}: 'needs_write' true veya false olmalı.")
        if needs_write and writers is not None and assignee.strip().lower() not in writers:
            raise PlanError(f"{where}: '{assignee.strip().lower()}' dosya yazamaz; yazma görevi başka ajana verilmeli.")
        files = _parse_files(raw.get("files"), where, needs_write, protected)
        verify = raw.get("verify") or []
        if not isinstance(verify, list) or not all(isinstance(v, str) for v in verify):
            raise PlanError(f"{where}: 'verify' bir metin listesi olmalı.")
        verify = list(dict.fromkeys(v.strip().lower() for v in verify))
        for v in verify:
            if v not in verify_names:
                raise PlanError(f"{where}: bilinmeyen doğrulama komutu: {v[:50]!r}")
        if needs_write and not verify and AUTO_TEST in verify_names and any(_is_py_test(f) for f in files):
            # Güvenlik ağı: Python testi yazan görev doğrulamasız planlandıysa testler yine de çalıştırılır.
            verify = [AUTO_TEST]
        tasks.append(Task(
            id=tid,
            title=_text(raw, "title", where),
            assignee=assignee.strip().lower(),
            instructions=_text(raw, "instructions", where),
            expected_output=_text(raw, "expected_output", where),
            depends_on=[d.strip().lower() for d in deps],
            needs_write=needs_write,
            files=files,
            verify=verify,
        ))

    for i, a in enumerate(tasks):
        for b in tasks[i + 1:]:
            for fa in a.files:
                for fb in b.files:
                    if paths_overlap(fa, fb):
                        raise PlanError(f"Görev '{a.id}' ve '{b.id}' aynı dosyalara yazamaz: {fa} / {fb}")

    for t in tasks:
        for d in t.depends_on:
            if d == t.id:
                raise PlanError(f"Görev '{t.id}': kendine bağımlı olamaz.")
            if d not in seen:
                raise PlanError(f"Görev '{t.id}': bilinmeyen bağımlılık: {d[:50]!r}")
        t.depends_on = list(dict.fromkeys(t.depends_on))  # tekrarları kaldır, sırayı koru

    # Kahn algoritmasıyla döngü kontrolü
    indeg = {t.id: len(t.depends_on) for t in tasks}
    children: dict[str, list[str]] = {t.id: [] for t in tasks}
    for t in tasks:
        for d in t.depends_on:
            children[d].append(t.id)
    queue = deque(tid for tid, n in indeg.items() if n == 0)
    visited = 0
    while queue:
        cur = queue.popleft()
        visited += 1
        for c in children[cur]:
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
    if visited != len(tasks):
        bad = sorted(tid for tid, n in indeg.items() if n > 0)
        raise PlanError(f"Planda döngüsel bağımlılık var: {', '.join(bad)}")

    return Plan(summary=summary, tasks=tasks, phase_title=phase_title, done=done)


def parse_review(text: str) -> tuple[str, str]:
    """Denetim yanıtını ('approve'|'revise', geri_bildirim) olarak döndürür."""
    try:
        data = extract_json(text)
    except PlanError:
        return "approve", "(denetçi yanıtı okunamadı)"
    if not isinstance(data, dict):
        return "approve", "(denetçi yanıtı okunamadı)"
    verdict = data.get("verdict")
    verdict = verdict.strip().lower() if isinstance(verdict, str) else ""
    if verdict != "revise":
        verdict = "approve"  # bilinmeyen değerler approve sayılır (bölüm 7.2)
    feedback = data.get("feedback", "")
    if not isinstance(feedback, str):
        feedback = json.dumps(feedback, ensure_ascii=False)
    return verdict, feedback.strip()[:4000]
