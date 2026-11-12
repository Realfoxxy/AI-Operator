"""Yerel web sunucusu ve SSE (bölüm 10.2, 10.4; kural 20–25).

Bölümler:
  1. Sabitler ve yardımcılar
  2. WebApprover: onay isteklerini tarayıcıya taşır
  3. RunView: /api/durum için çalıştırma görünümü (olay aboneliği)
  4. OrkestraServer: çalıştırma yönetimi (motor ayrı iş parçacığında, kendi asyncio döngüsüyle)
  5. HTTP işleyici: güvenlik denetimleri, statik dosyalar, API uç noktaları, SSE
"""

from __future__ import annotations

import asyncio
import copy
import hmac
import json
import os
import re
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .agents import AgentError, build_agents
from .approvals import Approver
from .checkpoints import CheckpointError, CheckpointStore
from .config import Config, normalize_rel_path
from .engine import BudgetExceeded, Orchestrator, RunCancelled, RunStopped
from .events import EventBus
from .guard import is_protected
from .planner import PlanError
from .security import SecurityError, redact, validate_layout, validate_workspace
from .state import RUN_NAME_RE, STATE_FILE, ProjectMemory, StateError, load_state
from .transcript import Transcript

# ====================================================================== 1. Sabitler

HOST = "127.0.0.1"          # yapılandırılamaz (kural 20)
WEB_DIR = Path(__file__).resolve().parent / "web"
INSTALL_DIR = Path(__file__).resolve().parent.parent
STATIC_FILES = {             # sabit beyaz liste; istekteki yoldan dosya yolu türetilmez (kural 23)
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}
MAX_BODY = 64 * 1024
MAX_FILE_VIEW = 200 * 1024
HEARTBEAT_S = 15
MAX_SSE_CLIENTS = 16
SOCKET_TIMEOUT_S = 60
MAX_GOAL_CHARS = 20000
VIEW_LIMIT = 20000
LIVE_CHARS = 400
PALETTE = ["#d97757", "#4c8bf5", "#8b5cf6", "#16a34a", "#db2777", "#0891b2", "#ca8a04"]


class ApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def web_event(ev: dict) -> dict:
    """Tarayıcıya giden olay: büyük istem metinleri kısaltılır (tam hali kayit.jsonl'dedir)."""
    if ev.get("tur") == "cagri" and isinstance(ev.get("istem"), str) and len(ev["istem"]) > 2000:
        ev = dict(ev)
        ev["istem"] = ev["istem"][:2000] + "\n… (kısaltıldı)"
    if ev.get("tur") == "yanit" and isinstance(ev.get("metin"), str) and len(ev["metin"]) > 8000:
        ev = dict(ev)
        ev["metin"] = ev["metin"][:8000] + "\n… (kısaltıldı)"
    return ev


# ====================================================================== 2. WebApprover

ALLOWED_DECISIONS = {"plan": {"onay", "ret"}, "baslangic_uyarisi": {"onay", "ret"}, "ihlal": {"devam", "durdur"}}


class WebApprover(Approver):
    """onay_istegi olayını motor yayınlar; bu sınıf kararı bekleyen bir asyncio.Future tutar.

    Zaman aşımı yoktur; Durdur komutu future'ı iptal eder.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.loop = loop
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[asyncio.Future, str, dict]] = {}
        self._validators: dict[str, object] = {}

    async def request(self, kind: str, payload: dict, istek_id: str = "", validate=None) -> str:
        fut = self.loop.create_future()
        istek_id = istek_id or secrets.token_hex(6)
        with self._lock:
            self._pending[istek_id] = (fut, kind, payload)
            if validate is not None:
                self._validators[istek_id] = validate
        try:
            return await fut
        finally:
            with self._lock:
                self._pending.pop(istek_id, None)
                self._validators.pop(istek_id, None)

    def pop_edit(self, istek_id: str):
        with self._lock:
            return self._edits.pop(istek_id, None)

    def pending(self) -> dict | None:
        with self._lock:
            for istek_id, (_, kind, payload) in self._pending.items():
                return {"istek_id": istek_id, "istek_turu": kind, "veri": payload}
        return None

    def resolve(self, istek_id: str, karar: str, plan=None) -> None:
        """HTTP iş parçacığından çağrılır; future, motorun döngüsünde çözülür.

        plan verilirse (yalnızca plan onayında): kullanıcının düzenlediği plan motorun kurallarıyla
        doğrulanır; geçersizse 400 döner ve istek bekler durumda kalır.
        """
        with self._lock:
            item = self._pending.get(istek_id)
            validate = self._validators.get(istek_id)
        if item is None:
            raise ApiError(404, "Bekleyen böyle bir onay isteği yok.")
        fut, kind, _ = item
        if karar not in ALLOWED_DECISIONS.get(kind, set()):
            raise ApiError(400, f"Geçersiz karar: {karar!r}")
        if plan is not None:
            if kind != "plan" or karar != "onay" or validate is None or not isinstance(plan, dict):
                raise ApiError(400, "Plan yalnızca plan onayında düzenlenebilir.")
            try:
                edited = validate(plan)
            except PlanError as e:
                raise ApiError(400, f"Düzenlenen plan geçersiz: {e}") from e
            with self._lock:
                self._edits[istek_id] = edited

        def _set():
            if not fut.done():
                fut.set_result(karar)
        self.loop.call_soon_threadsafe(_set)

    def cancel_all(self) -> None:
        with self._lock:
            futs = [f for f, _, _ in self._pending.values()]
        for f in futs:
            self.loop.call_soon_threadsafe(f.cancel)


# ====================================================================== 3. RunView

class RunView:
    """Aktif çalıştırmanın olaylarını tutar; tarayıcı yeniden bağlandığında durum bunlardan kurulur."""

    def __init__(self):
        self._lock = threading.Lock()
        self.events: list[dict] = []
        self.live: dict[str, str] = {}
        self.first_id = 0

    def reset(self, first_id: int) -> None:
        with self._lock:
            self.events, self.live, self.first_id = [], {}, first_id

    def __call__(self, ev: dict, persist: bool) -> None:
        with self._lock:
            if ev["tur"] == "parca":
                self.live[ev["ajan"]] = (self.live.get(ev["ajan"], "") + ev.get("metin", ""))[-LIVE_CHARS:]
                return
            if ev["tur"] == "ajan_durumu" and ev.get("durum") in ("dusunuyor", "dosya_yaziyor", "denetliyor"):
                self.live[ev["ajan"]] = ""
            self.events.append(web_event(ev))
            if len(self.events) > VIEW_LIMIT:
                del self.events[: len(self.events) - VIEW_LIMIT]

    def snapshot(self) -> tuple[list[dict], dict[str, str]]:
        with self._lock:
            return list(self.events), dict(self.live)


# ====================================================================== 4. OrkestraServer

class RunHandle:
    def __init__(self, info: dict):
        self.info = info
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.orch: Orchestrator | None = None
        self.approver: WebApprover | None = None
        self.workspace: Path | None = None
        self.run_dir: Path | None = None
        self.ready = threading.Event()
        self.done = threading.Event()


class OrkestraServer:
    def __init__(self, cfg: Config, *, mock: bool = False, port: int | None = None,
                 open_browser: bool | None = None, mock_speed: float = 1.0, auto_yes: bool = False):
        self.cfg = cfg
        self.mock = mock
        self.mock_speed = mock_speed
        self.port_start = cfg.ui.port if port is None else port
        self.open_browser = cfg.ui.open_browser if open_browser is None else open_browser
        self.token = secrets.token_urlsafe(32)
        self.bus = EventBus()
        self.view = RunView()
        self.bus.add_listener(self.view)
        self._lock = threading.Lock()
        self.active: RunHandle | None = None
        self.last: RunHandle | None = None
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.closing = threading.Event()
        self._sse_lock = threading.Lock()
        self.sse_clients = 0

    # -------------------------------------------------------------- sunucu yaşam döngüsü

    @property
    def port(self) -> int:
        return self.httpd.server_address[1] if self.httpd else 0

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/?t={self.token}"

    def start(self) -> None:
        last_err: OSError | None = None
        candidates = [0] if self.port_start == 0 else range(self.port_start, self.port_start + 11)
        for port in candidates:
            try:
                httpd = _QuietServer((HOST, port), _Handler)
                break
            except OSError as e:
                last_err = e
        else:
            raise OSError(f"{self.port_start}–{self.port_start + 10} aralığında boş port bulunamadı: {last_err}")
        httpd.daemon_threads = True
        httpd.app = self   # type: ignore[attr-defined]
        self.httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="orkestra-http", daemon=True)
        self._thread.start()

    def open(self) -> None:
        if self.open_browser:
            try:
                webbrowser.open(self.url)
            except Exception:  # noqa: BLE001 - tarayıcı açılamazsa adres zaten konsolda
                pass

    def shutdown(self, timeout: float = 10.0) -> None:
        """Aktif çalıştırma durdurulur, durum kaydedilir, sunucu kapatılır."""
        self.closing.set()
        h = self.active
        if h is not None and not h.done.is_set():
            self._stop(h)
            h.done.wait(timeout)
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()

    # -------------------------------------------------------------- çalıştırma yönetimi

    def _workspace_for_view(self) -> Path | None:
        h = self.active or self.last
        if h is not None and h.workspace is not None:
            return h.workspace
        ws = self.cfg.settings.workspace.expanduser().resolve()
        return ws if ws.is_dir() else None

    def _check_workspace(self, cfg: Config, ws: Path) -> Path:
        try:
            ws = validate_workspace(ws)
            validate_layout(ws, cfg.settings.runs_dir, INSTALL_DIR, cfg.dir)
        except SecurityError as e:
            raise ApiError(400, str(e)) from e
        return ws

    def _make_agents(self, cfg: Config):
        return build_agents(cfg.agents, mock=self.mock, hiz=self.mock_speed)

    def start_run(self, goal: str, workspace: str | None = None, mode: str | None = None) -> dict:
        if not isinstance(goal, str) or not goal.strip():
            raise ApiError(400, "Hedef boş olamaz.")
        if len(goal) > MAX_GOAL_CHARS:
            raise ApiError(400, f"Hedef en fazla {MAX_GOAL_CHARS} karakter olabilir.")
        if mode is not None and mode not in ("tek", "asamali"):
            raise ApiError(400, "Mod 'tek' veya 'asamali' olmalı.")
        if workspace is not None and (not isinstance(workspace, str) or len(workspace) > 1000):
            raise ApiError(400, "Çalışma klasörü geçersiz.")
        cfg = copy.deepcopy(self.cfg)
        if mode:
            cfg.settings.mode = mode
        with self._lock:
            self._ensure_idle()
            ws = self._check_workspace(cfg, Path(workspace) if workspace and workspace.strip()
                                       else cfg.settings.workspace)
            cfg.settings.workspace = ws
            return self._launch(cfg, goal.strip(), ws, None, None, None)

    def resume_run(self, name: str) -> dict:
        if not isinstance(name, str) or not RUN_NAME_RE.match(name):
            raise ApiError(400, "Geçersiz çalıştırma adı.")
        cfg = copy.deepcopy(self.cfg)
        run_dir = cfg.settings.runs_dir / name
        with self._lock:
            self._ensure_idle()
            try:
                state, plans = load_state(run_dir, cfg)
            except StateError as e:
                raise ApiError(400, f"Çalıştırma sürdürülemiyor: {e}") from e
            if state.durum == "bitti":
                raise ApiError(400, "Bu çalıştırma zaten tamamlanmış.")
            ws = self._check_workspace(cfg, Path(state.calisma_klasoru))
            cfg.settings.workspace = ws
            cfg.settings.mode = state.mod
            return self._launch(cfg, state.hedef, ws, run_dir, state, plans)

    def _ensure_idle(self) -> None:
        if self.closing.is_set():
            raise ApiError(503, "Sunucu kapanıyor.")
        if self.active is not None and not self.active.done.is_set():
            raise ApiError(409, "Zaten aktif bir çalıştırma var.")

    def _launch(self, cfg: Config, goal: str, ws: Path, run_dir: Path | None, state, plans) -> dict:
        try:
            transcript = Transcript(cfg.settings.runs_dir, existing=run_dir)
        except OSError as e:
            raise ApiError(500, f"Kayıt klasörü oluşturulamadı: {e}") from e
        info = {"hedef": goal, "mod": cfg.settings.mode, "calisma_klasoru": str(ws),
                "run": transcript.dir.name, "mock": self.mock, "baslangic": time.time(),
                "devam": state is not None, "max_asama": cfg.settings.max_phases}
        h = RunHandle(info)
        h.workspace, h.run_dir = ws, transcript.dir
        self.view.reset(self.bus.last_id)
        self.bus.add_listener(transcript.on_event)
        # Sürdürmede de yayınlanır (devam=True): tüm açık arayüzler görünümlerini sıfırlar.
        self.bus.publish("baslangic", hedef=goal, calisma_klasoru=str(ws), mock=self.mock, mod=cfg.settings.mode,
                         devam=state is not None)
        h.thread = threading.Thread(target=self._engine_thread, name="orkestra-motor", daemon=True,
                                    args=(h, cfg, goal, transcript, state, plans))
        self.active = h
        h.thread.start()
        h.ready.wait(10)
        return info

    def _engine_thread(self, h: RunHandle, cfg: Config, goal: str, transcript: Transcript, state, plans) -> None:
        loop = asyncio.new_event_loop()
        h.loop = loop

        async def main():
            h.approver = WebApprover(loop)
            h.orch = Orchestrator(cfg, self._make_agents(cfg), self.bus, h.workspace, h.approver,
                                  run_dir=transcript.dir, state=state, resume_plans=plans,
                                  memory=ProjectMemory.load(h.workspace))
            h.ready.set()
            return await h.orch.run(goal)

        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(main())
        except (RunCancelled, RunStopped):
            pass  # olaylar motor tarafından yayınlandı
        except (PlanError, AgentError, SecurityError, BudgetExceeded, StateError) as e:
            self.bus.publish("hata", mesaj=redact(str(e)))
        except BaseException as e:  # noqa: BLE001 - motor iş parçacığı sessizce ölmemeli
            self.bus.publish("hata", mesaj=f"Beklenmeyen hata: {redact(repr(e))[:500]}")
        finally:
            h.ready.set()
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
            self.bus.remove_listener(transcript.on_event)
            transcript.close()
            with self._lock:
                self.last = h
                h.done.set()

    def _require_active(self) -> RunHandle:
        h = self.active
        if h is None or h.done.is_set() or h.orch is None or h.loop is None:
            raise ApiError(409, "Aktif bir çalıştırma yok.")
        return h

    def _stop(self, h: RunHandle) -> None:
        if h.loop is None or h.orch is None:
            return
        try:
            h.loop.call_soon_threadsafe(h.orch.stop)
            if h.approver:
                h.approver.cancel_all()
        except RuntimeError:
            pass  # döngü zaten kapanmış

    def control(self, action: str) -> None:
        h = self._require_active()
        if action == "duraklat":
            h.loop.call_soon_threadsafe(h.orch.pause)
        elif action == "devam":
            h.loop.call_soon_threadsafe(h.orch.resume)
        elif action == "durdur":
            self._stop(h)

    def add_note(self, metin, hedef) -> None:
        h = self._require_active()
        if hedef is not None and not isinstance(hedef, str):
            raise ApiError(400, "Not hedefi geçersiz.")

        async def _add():
            return h.orch.add_note(metin, hedef or None)
        try:
            asyncio.run_coroutine_threadsafe(_add(), h.loop).result(5)
        except ValueError as e:
            raise ApiError(400, str(e)) from e

    def approve(self, istek_id, karar, plan=None) -> None:
        h = self._require_active()
        if not isinstance(istek_id, str) or not isinstance(karar, str) or h.approver is None:
            raise ApiError(400, "istek_id ve karar metin olmalı.")
        h.approver.resolve(istek_id, karar, plan)

    def status(self) -> dict:
        # son_id anlık görüntüden ÖNCE okunur: arada gelen bir olay en kötü ihtimalle iki kez gönderilir
        # (istemci kimliğe göre tekrarı atar), hiçbir olay kaybolmaz.
        son_id = self.bus.last_id
        events, live = self.view.snapshot()
        h = self.active or self.last
        active = self.active is not None and not self.active.done.is_set()
        agents = []
        for i, (name, a) in enumerate(self.cfg.agents.items()):
            agents.append({"ad": name, "gorunen": a.display, "renk": a.color or PALETTE[i % len(PALETTE)],
                           "planlayici": name == self.cfg.settings.planner,
                           "denetci": name == self.cfg.settings.reviewer,
                           "akis": a.stream_format != "none"})
        default_ws = self.cfg.settings.workspace.expanduser().resolve()
        memory = ProjectMemory.load(default_ws) if default_ws.is_dir() else None
        return {
            "surum": 1,
            "calisiyor": active,
            "son_id": son_id,
            "calistirma": h.info if h else None,
            "ajanlar": agents,
            "bekleyen_onay": (self.active.approver.pending() if active and self.active.approver else None),
            "duraklatildi": bool(active and self.active.orch and self.active.orch.paused),
            "olaylar": events if h else [],
            "canli": live if h else {},
            "varsayilan_workspace": str(default_ws),
            "hafiza_ozeti": memory.summary() if memory and not memory.empty else "",
            "mock": self.mock,
            "max_hedef": MAX_GOAL_CHARS,
        }

    # -------------------------------------------------------------- kontrol noktaları ve fark (sürüm 0.3)

    def _store(self) -> CheckpointStore:
        ws = self._workspace_for_view()
        if ws is None:
            raise ApiError(404, "Çalışma klasörü yok.")
        store = CheckpointStore(ws, self.cfg.settings.runs_dir)
        if not store.available:
            raise ApiError(501, "git kurulu değil; kontrol noktaları kullanılamıyor.")
        return store

    def checkpoints(self) -> dict:
        store = self._store()
        h = self.active or self.last
        start = h.orch.start_checkpoint if h and h.orch else None
        return {"kontrol_noktalari": [{"sha": c.sha, "zaman": c.zaman, "etiket": c.etiket} for c in store.list(50)],
                "baslangic": start}

    def diff(self, yol) -> dict:
        rel = normalize_rel_path(yol) if isinstance(yol, str) and len(yol) <= 1000 else None
        if rel is None or rel.endswith("/"):
            raise ApiError(400, "Geçersiz dosya yolu.")
        if is_protected(rel, self.cfg.guard.protected_paths):
            raise ApiError(403, "Korumalı yollar görüntülenemez.")
        store = self._store()
        h = self.active or self.last
        base = h.orch.start_checkpoint if h and h.orch and h.orch.start_checkpoint else None
        if base is None:
            cps = store.list(1)
            base = cps[0].sha if cps else None
        if base is None:
            raise ApiError(404, "Karşılaştırılacak kontrol noktası yok.")
        try:
            return {"yol": rel, "taban": base, "fark": redact(store.diff(base, rel))}
        except CheckpointError as e:
            raise ApiError(400, str(e)) from e

    def rollback(self, sha) -> dict:
        with self._lock:
            if self.active is not None and not self.active.done.is_set():
                raise ApiError(409, "Çalıştırma sürerken geri alınamaz; önce durdur.")
            store = self._store()
            try:
                before, rows = store.restore(sha if isinstance(sha, str) else "")
            except CheckpointError as e:
                raise ApiError(400, str(e)) from e
        return {"tamam": True, "onceki": before.sha, "degisenler": [{"durum": st, "yol": p} for st, p in rows[:500]]}

    # -------------------------------------------------------------- geçmiş ve dosyalar

    def history(self) -> list[dict]:
        root = self.cfg.settings.runs_dir
        rows = []
        if not root.is_dir():
            return rows
        for d in sorted((p for p in root.iterdir() if p.is_dir() and RUN_NAME_RE.match(p.name)),
                        key=lambda p: p.name, reverse=True)[:100]:
            row = {"run": d.name, "hedef": "", "durum": "bilinmiyor", "mod": "tek",
                   "rapor": (d / "sonuc.md").is_file()}
            try:
                st = json.loads((d / STATE_FILE).read_text(encoding="utf-8"))
                if isinstance(st, dict):
                    row["hedef"] = str(st.get("hedef", ""))[:120]
                    row["durum"] = str(st.get("durum", "bilinmiyor"))[:20]
                    row["mod"] = str(st.get("mod", "tek"))[:10]
            except (OSError, ValueError):
                pass
            row["devam_edilebilir"] = row["durum"] not in ("bitti", "bilinmiyor") and (d / STATE_FILE).is_file()
            rows.append(row)
        return rows

    def history_item(self, name: str) -> dict:
        if not RUN_NAME_RE.match(name):
            raise ApiError(404, "Bulunamadı.")
        d = self.cfg.settings.runs_dir / name
        if not d.is_dir():
            raise ApiError(404, "Bulunamadı.")
        plans = []
        for p in sorted(d.glob("plan*.json")):
            if re.fullmatch(r"plan(-a\d{1,3})?\.json", p.name):
                try:
                    plans.append(json.loads(p.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    pass
        report = ""
        try:
            report = (d / "sonuc.md").read_text(encoding="utf-8")[:500_000]
        except OSError:
            pass
        return {"run": name, "planlar": plans, "rapor": report}

    def read_file(self, yol) -> dict:
        """Kural 25: salt-okunur; çalışma klasörü içinde; korumalı yol, bağlantı, büyük ve ikili dosya yok."""
        ws = self._workspace_for_view()
        if ws is None:
            raise ApiError(404, "Çalışma klasörü yok.")
        rel = normalize_rel_path(yol) if isinstance(yol, str) and len(yol) <= 1000 else None
        if rel is None or rel.endswith("/"):
            raise ApiError(400, "Geçersiz dosya yolu.")
        if is_protected(rel, self.cfg.guard.protected_paths):
            raise ApiError(403, "Korumalı yollar görüntülenemez.")
        cur = ws
        for part in rel.split("/"):
            cur = cur / part
            if os.path.islink(cur):
                raise ApiError(403, "Sembolik bağlantılar görüntülenemez.")
        try:
            resolved = cur.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ApiError(404, "Dosya bulunamadı.") from None
        if not (resolved == ws or ws in resolved.parents) or not resolved.is_file():
            raise ApiError(404, "Dosya bulunamadı.")
        size = resolved.stat().st_size
        if size > MAX_FILE_VIEW:
            raise ApiError(413, "Dosya 200 KB'tan büyük; görüntülenemez.")
        data = resolved.read_bytes()
        if b"\x00" in data[:8192]:
            raise ApiError(415, "İkili dosyalar görüntülenemez.")
        return {"yol": rel, "boyut": size, "icerik": redact(data.decode("utf-8", errors="replace"))}


# ====================================================================== 5. HTTP işleyici

class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # kopan bağlantılar vb. konsolu kirletmez; hata ayrıntıları token içerebilir


class _Handler(BaseHTTPRequestHandler):
    timeout = SOCKET_TIMEOUT_S          # yavaş istemcilere karşı (SSE kuyruğu bekler, soketi değil)
    server_version = "Orkestra"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> OrkestraServer:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format, *args):  # noqa: A002
        pass  # istek satırları token içerebilir; hiçbir şey loglanmaz (kural 21)

    # -------------------------------------------------------------- yanıt yardımcıları

    def _headers(self, code: int, ctype: str, length: int | None) -> None:
        self.send_response(code)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self._headers(code, ctype, len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"hata": message})

    def send_error(self, code, message=None, explain=None):
        """http.server'ın kendi hata sayfaları da güvenlik başlıklarıyla ve JSON olarak döner."""
        self.close_connection = True
        try:
            self._error(int(code), "Geçersiz istek." if int(code) < 500 else "Sunucu hatası.")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # -------------------------------------------------------------- güvenlik denetimleri

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        port = self.app.port
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        port = self.app.port
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _token_ok(self, query: dict) -> bool:
        given = self.headers.get("X-Orkestra-Token")
        if given is None:
            vals = query.get("t")
            given = vals[0] if vals else ""
        return hmac.compare_digest(given.encode("utf-8"), self.app.token.encode("utf-8"))

    # -------------------------------------------------------------- yönlendirme

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self._dispatch_get()
        except ApiError as e:
            self._error(e.code, e.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            self._error(500, "Sunucu hatası.")

    def do_POST(self):
        try:
            self._dispatch_post()
        except ApiError as e:
            self.close_connection = True   # gövde okunmamış olabilir; bağlantı yeniden kullanılmaz
            self._error(e.code, e.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            self._error(500, "Sunucu hatası.")

    def _not_allowed(self):
        self._error(405, "Yönteme izin verilmiyor.")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed  # noqa: N815

    def _dispatch_get(self):
        if not self._host_ok():
            raise ApiError(403, "Geçersiz Host başlığı.")
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        if path in STATIC_FILES:
            name, ctype = STATIC_FILES[path]
            try:
                body = (WEB_DIR / name).read_bytes()
            except OSError:
                raise ApiError(404, "Bulunamadı.") from None
            self._send(200, body, ctype)
            return
        if not path.startswith("/api/"):
            raise ApiError(404, "Bulunamadı.")
        if path == "/api/olaylar":
            if not self._token_ok(query):    # EventSource başlık gönderemez: token sorgu parametresiyle
                raise ApiError(401, "Yetkisiz.")
            self._sse(query)
            return
        if self.headers.get("X-Orkestra-Token") is None or not self._token_ok({}):
            raise ApiError(401, "Yetkisiz.")
        app = self.app
        if path == "/api/durum":
            self._json(200, app.status())
        elif path == "/api/dosya":
            self._json(200, app.read_file((query.get("yol") or [None])[0]))
        elif path == "/api/fark":
            self._json(200, app.diff((query.get("yol") or [None])[0]))
        elif path == "/api/kontrol":
            self._json(200, app.checkpoints())
        elif path == "/api/gecmis":
            self._json(200, {"calistirmalar": app.history()})
        elif path.startswith("/api/gecmis/"):
            self._json(200, app.history_item(path[len("/api/gecmis/"):]))
        else:
            raise ApiError(404, "Bulunamadı.")

    def _read_json(self) -> dict:
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "Content-Type: application/json zorunludur.")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ApiError(411, "Content-Length zorunludur.") from None
        if length < 0 or length > MAX_BODY:
            raise ApiError(413, "İstek gövdesi en fazla 64 KB olabilir.")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "Geçersiz JSON.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "Gövde bir JSON nesnesi olmalı.")
        return data

    def _dispatch_post(self):
        if not self._host_ok():
            raise ApiError(403, "Geçersiz Host başlığı.")
        if not self._origin_ok():
            raise ApiError(403, "Geçersiz veya eksik Origin başlığı.")
        if self.headers.get("X-Orkestra-Token") is None or not self._token_ok({}):
            raise ApiError(401, "Yetkisiz.")
        path = urlsplit(self.path).path
        data = self._read_json()
        app = self.app
        if path == "/api/calistir":
            info = app.start_run(data.get("hedef"), data.get("workspace"), data.get("mod"))
            self._json(200, {"tamam": True, "calistirma": info})
        elif path == "/api/onay":
            app.approve(data.get("istek_id"), data.get("karar"), data.get("plan"))
            self._json(200, {"tamam": True})
        elif path == "/api/not":
            app.add_note(data.get("metin"), data.get("hedef"))
            self._json(200, {"tamam": True})
        elif path in ("/api/duraklat", "/api/devam", "/api/durdur"):
            app.control(path.rsplit("/", 1)[1])
            self._json(200, {"tamam": True})
        elif path == "/api/geri-al":
            self._json(200, app.rollback(data.get("sha")))
        elif path == "/api/devam-et-run":
            info = app.resume_run(data.get("run"))
            self._json(200, {"tamam": True, "calistirma": info})
        else:
            raise ApiError(404, "Bulunamadı.")

    # -------------------------------------------------------------- SSE

    def _sse(self, query: dict) -> None:
        last = self.headers.get("Last-Event-ID") or (query.get("son") or [None])[0]
        try:
            son_id = int(last) if last is not None else None
        except ValueError:
            son_id = None
        with self.app._sse_lock:
            if self.app.sse_clients >= MAX_SSE_CLIENTS:
                raise ApiError(429, "Çok fazla açık olay bağlantısı.")
            self.app.sse_clients += 1
        sub = self.app.bus.subscribe(son_id)
        self.close_connection = True
        try:
            self._headers(200, "text/event-stream; charset=utf-8", None)
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while not self.app.closing.is_set() and not sub.closed:
                ev = sub.get(timeout=HEARTBEAT_S)
                if ev is None:
                    self.wfile.write(b": kalp\n\n")
                else:
                    data = json.dumps(web_event(ev), ensure_ascii=False)
                    self.wfile.write(f"id: {ev['id']}\nevent: orkestra\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            sub.close()
            with self.app._sse_lock:
                self.app.sse_clients -= 1
