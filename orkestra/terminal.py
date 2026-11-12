"""Orkestra Terminali: büyük projeler geliştirmek için etkileşimli kabuk (sürüm 0.3).

`orkestra` (veya `python -m orkestra`) argümansız çalıştırıldığında açılır. Kullanıcı hedefini yazar;
ekip (Claude planlar ve denetler, Gemini ve Copilot çalışır) projeyi aşama aşama geliştirir. Çalışma
sürerken kullanıcı yazmaya devam edebilir: düz metin ekibe not olarak gider, /komutlar kontrol sağlar,
onay soruları istem satırına yönlendirilir.

Bölümler:
  1. Renk ve çıktı (olaylar yazılırken kullanıcının satırı korunur)
  2. TerminalApprover: onay sorularını istem satırına taşır
  3. EngineRunner: motoru ayrı iş parçacığında, kendi asyncio döngüsüyle çalıştırır
  4. TerminalPrinter: olayları kısa ve okunur satırlara çevirir
  5. TerminalApp: proje, hesaplar, komutlar ve ana döngü
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shlex
import shutil
import sys
import threading
import time
from pathlib import Path

from . import __version__, accounts, configedit, katalog
from .agents import AgentError, build_agents
from .approvals import Approver, YES_ANSWERS, edit_plan_interactive, plan_needs_manual_approval, VERIFY_WARNING
from .checkpoints import CheckpointError, CheckpointStore
from .config import Config, VerifyCommand, load_config, normalize_rel_path, user_config_dir
from .engine import BudgetExceeded, Orchestrator, RunCancelled, RunStopped
from .events import EventBus
from .planner import PlanError
from .security import SecurityError, clean_terminal, is_safe_id, redact, validate_layout, validate_workspace
from .state import RUN_NAME_RE, STATE_FILE, ProjectMemory, StateError, load_state
from .analiz import analyze_run
from .transcript import Transcript

try:                                    # POSIX'te satır düzenleme, geçmiş ve sekme tamamlama
    import readline  # noqa: F401
except ImportError:                     # Windows: düz input()
    readline = None

INSTALL_DIR = Path(__file__).resolve().parent.parent
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
COMMANDS = ["/yardim", "/hesaplar", "/baglan", "/kur", "/test", "/proje", "/projeler", "/mod", "/butce", "/asama",
            "/sikimod", "/devretme", "/otomatik", "/dogrulama", "/durum", "/gorevler", "/dosyalar", "/fark",
            "/kontrol", "/geri-al", "/not", "/duraklat", "/surdur", "/durdur", "/devam", "/gecmis", "/rapor",
            "/kullanim", "/ayrinti", "/hizli", "/model", "/katalog", "/ekle", "/cikar", "/ajanlar", "/ajan", "/analiz",
            "/temizle", "/cikis"]
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

HELP = """
KOMUTLAR  (düz metin yazarsan: boştayken yeni hedef olur, çalışma sürerken ekibe not olarak gider)

 Hesaplar      /hesaplar                 Claude, Gemini ve Copilot bağlantı durumunu göster
               /kur                      Eksik CLI araçlarını npm ile kur
               /baglan [ajan|hepsi]      Hesabını bağla (resmi giriş ekranı açılır)
               /test [ajan|hepsi]        Bağlantıyı küçük bir istemle dene
 Ekip          /katalog                  Eklenebilir yapay zekâlar (hiçbiri zorunlu değil)
               /ekle <ad>  /cikar <ad>   Katalogdan ekip üyesi ekle / çıkar
               /ajanlar                  Ekipteki tüm ajanlar (açık/kapalı)
               /ajan ac|kapat <ad>       Bir ajanı aç veya kapat (ör. /ajan kapat gemini)
 Proje         /proje [ad|yol]           Proje seç veya oluştur (ad: proje kökünde alt klasör)
               /projeler                 Projeleri listele
               /gecmis   /rapor [no]     Bu projedeki çalıştırmalar ve raporları
               /analiz [no]              Çalıştırma neden pahalı/yavaştı? Token, süre ve sorun özeti
               /devam [no]               Yarım kalan çalıştırmayı kaldığı yerden sürdür
 Ayarlar       /mod tek|asamali          Tek plan veya aşamalı (büyük projeler) mod
               /butce <n>   /asama <n>   Çağrı bütçesi, en fazla aşama
               /sikimod ac|kapat         Yazma sırasında okumaları da beklet (kesin değişiklik takibi)
               /devretme ac|kapat        Kota dolunca görevi başka ajana devret
               /otomatik ac|kapat        Salt-okunur planları sormadan onayla
               /dogrulama [ekle <ad> <komut…> | sil <ad>]   Test/derleme komutları (Orkestra çalıştırır)
 Çalışma       /durum  /gorevler  /dosyalar  /kullanim
               /not [@ajan] <metin>      Ekibe veya tek ajana talimat notu
               /duraklat  /surdur  /durdur
               /ayrinti ac|kapat         Her model çağrısını da göster
 Hız ve model  /hizli ac|kapat           Basit işler: tek plan, danışma ve denetim turu yok (çok daha hızlı)
               /model [ajan] [model]     Ajanın modelini seç, ör. /model claude sonnet · /model claude haiku
                                         /model copilot <model> · /model claude varsayilan
 Güvenlik ağı  /kontrol                  Kontrol noktaları (her aşama sonunda otomatik alınır)
               /fark <dosya>             Dosyanın çalıştırma başından bu yana farkı
               /geri-al <no>             Projeyi bir kontrol noktasına döndür (geri alma da geri alınabilir)
 Diğer         /temizle  /yardim  /cikis
"""


# ====================================================================== 1. Renk ve çıktı

class Out:
    """Thread güvenli çıktı. Arka plandan satır yazılırken kullanıcının yazdığı satır yeniden çizilir."""

    CODES = {"dim": "2", "bold": "1", "red": "31", "green": "32", "yellow": "33", "blue": "34", "magenta": "35",
             "cyan": "36"}

    def __init__(self, prompt_fn):
        self.tty = sys.stdout.isatty()
        self.color = self.tty and not os.environ.get("NO_COLOR") and (os.name != "nt" or os.environ.get("WT_SESSION")
                                                                        or os.environ.get("TERM"))
        self.prompt_fn = prompt_fn
        self.lock = threading.Lock()
        self.in_input = False
        self.last_submitted = ""    # readline arabelleği yeni tuşa basılana kadar önceki satırı tutar

    def c(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return "\x1b[" + ";".join(self.CODES[s] for s in styles) + "m" + text + "\x1b[0m"

    def line(self, text: str = "", *styles: str, model: bool = False) -> None:
        """model=True: metin model kaynaklıdır; kontrol karakterleri temizlenir."""
        text = clean_terminal(text) if model else text
        text = self.c(text, *styles)
        with self.lock:
            if self.in_input and self.tty and readline is not None:
                buf = readline.get_line_buffer()
                try:    # UTF-8 olmayan yerel ayarlarda readline vekil karakterler döndürebilir
                    buf = buf.encode("utf-8", "surrogateescape").decode("utf-8")
                except UnicodeError:
                    buf = ""
                # Gönderilmiş eski satır arabellekte sonunda '\n' ile kalır; yazılmakta olan satırda '\n' olmaz.
                if buf.endswith("\n") or buf == self.last_submitted:
                    buf = ""
                sys.stdout.write("\r\x1b[2K" + text + "\n" + self.prompt_fn() + buf)
            elif self.in_input and self.tty:
                sys.stdout.write("\n" + text + "\n" + self.prompt_fn())
            else:
                sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def bell(self) -> None:
        if self.tty:
            with self.lock:
                sys.stdout.write("\a")
                sys.stdout.flush()


# ====================================================================== 2. TerminalApprover

class TerminalApprover(Approver):
    """Onay isteklerini terminalin istem satırına yönlendirir; yanıtı ana iş parçacığı verir."""

    def __init__(self, app: "TerminalApp"):
        super().__init__()
        self.app = app
        self.pending: dict | None = None
        self._lock = threading.Lock()

    async def request(self, kind: str, payload: dict, istek_id: str = "", validate=None) -> str:
        if kind == "plan" and self.app.auto_yes and not plan_needs_manual_approval(payload):
            self.app.out.line("✔ Plan salt-okunur; otomatik onaylandı (/otomatik kapat ile kapatılır).", "dim")
            return "onay"
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        with self._lock:
            self.pending = {"kind": kind, "payload": payload, "id": istek_id, "validate": validate,
                            "fut": fut, "loop": loop}
        self.app.show_approval(kind, payload)
        try:
            return await fut
        finally:
            with self._lock:
                if self.pending and self.pending["fut"] is fut:
                    self.pending = None

    def answer(self, line: str) -> None:
        """Ana iş parçacığından: kullanıcının yanıtını işler."""
        with self._lock:
            p = self.pending
        if p is None:
            return
        a = line.strip().lower()
        kind = p["kind"]
        if kind == "plan" and a in ("d", "düzenle", "duzenle"):
            if p["validate"] is None:
                self.app.out.line("Bu plan düzenlenemiyor.", "yellow")
                return
            self.app.out.in_input = False
            edited = edit_plan_interactive(p["payload"].get("plan", {}), p["validate"],
                                           lambda t: self.app.out.line(t, "yellow"))
            if edited is None:
                self.app.out.line("Plan değişmedi. Yanıtla: e = onayla · d = düzenle · h = reddet", "yellow")
                return
            self._edits[p["id"]] = edited
            self.app.out.line("✔ Düzenlenen plan doğrulandı.", "green")
            karar = "onay"
        elif kind == "ihlal":
            karar = "devam" if a in YES_ANSWERS or a == "devam" else "durdur"
        else:
            karar = "onay" if a in YES_ANSWERS else "ret"

        def _set(fut=p["fut"]):
            if not fut.done():
                fut.set_result(karar)
        p["loop"].call_soon_threadsafe(_set)
        with self._lock:
            self.pending = None

    def cancel(self) -> None:
        with self._lock:
            p, self.pending = self.pending, None
        if p:
            p["loop"].call_soon_threadsafe(p["fut"].cancel)


# ====================================================================== 3. EngineRunner

class EngineRunner:
    """Bir çalıştırmayı arka plan iş parçacığında yürütür."""

    def __init__(self, app: "TerminalApp", cfg: Config, goal: str, ws: Path, *, run_dir: Path | None = None,
                 state=None, plans=None):
        self.app, self.cfg, self.goal, self.ws = app, cfg, goal, ws
        self.bus = EventBus()
        self.transcript = Transcript(cfg.settings.runs_dir, existing=run_dir)
        self.bus.add_listener(self.transcript.on_event)
        self.printer = TerminalPrinter(app)
        self.bus.add_listener(self.printer)
        self.approver = TerminalApprover(app)
        agents = build_agents(cfg.agents, mock=app.mock, hiz=app.mock_speed)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.orch: Orchestrator | None = None
        self._agents, self._state, self._plans = agents, state, plans
        self.report: str | None = None
        self.error: str | None = None
        self.done = threading.Event()
        self.ready = threading.Event()
        self.started = time.time()
        if state is None:
            self.bus.publish("baslangic", hedef=goal, calisma_klasoru=str(ws), mock=app.mock, mod=cfg.settings.mode)
        self.thread = threading.Thread(target=self._main, name="orkestra-motor", daemon=True)

    @property
    def run_dir(self) -> Path:
        return self.transcript.dir

    def start(self) -> None:
        self.thread.start()
        self.ready.wait(10)

    def _main(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)

        async def main():
            self.orch = Orchestrator(self.cfg, self._agents, self.bus, self.ws, self.approver,
                                     run_dir=self.transcript.dir, state=self._state, resume_plans=self._plans,
                                     memory=ProjectMemory.load(self.ws))
            self.ready.set()
            return await self.orch.run(self.goal)

        try:
            self.report = loop.run_until_complete(main())
        except RunCancelled as e:
            self.error = f"İptal edildi: {e}"
        except RunStopped as e:
            self.error = str(e)
        except (PlanError, AgentError, SecurityError, BudgetExceeded, StateError) as e:
            self.error = redact(str(e))
            self.bus.publish("hata", mesaj=self.error)
        except BaseException as e:  # noqa: BLE001
            self.error = f"Beklenmeyen hata: {redact(repr(e))[:400]}"
            self.bus.publish("hata", mesaj=self.error)
        finally:
            self.ready.set()
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
            self.transcript.close()
            self.done.set()
            self.app.on_run_finished(self)

    @property
    def active(self) -> bool:
        return not self.done.is_set()

    def call(self, fn, *args) -> None:
        if self.loop and self.orch and not self.done.is_set():
            try:
                self.loop.call_soon_threadsafe(fn, *args)
            except RuntimeError:
                pass

    def stop(self) -> None:
        if self.orch:
            self.call(self.orch.stop)
        self.approver.cancel()

    def add_note(self, metin: str, hedef: str | None) -> None:
        if not (self.loop and self.orch):
            raise ValueError("Çalıştırma henüz başlamadı.")

        async def _add():
            return self.orch.add_note(metin, hedef)
        asyncio.run_coroutine_threadsafe(_add(), self.loop).result(5)


# ====================================================================== 4. TerminalPrinter

class TerminalPrinter:
    """Olayları kısa satırlara çevirir. Model kaynaklı metinler temizlenerek basılır."""

    def __init__(self, app: "TerminalApp"):
        self.app = app
        self.out = app.out
        self.tasks: dict[str, dict] = {}
        self.files: dict[str, str] = {}
        self.phase = 0

    def __call__(self, ev: dict, persist: bool) -> None:
        fn = getattr(self, "_" + ev["tur"], None)
        if fn:
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 - yazdırma hatası çalıştırmayı etkilemesin
                pass

    def L(self, text, *styles, model=True):  # noqa: N802
        self.out.line(text, *styles, model=model)

    def _cagri(self, ev):
        if self.app.verbose:
            self.L(f"  → [{ev['no']}] {ev['ajan']} {ev['rol']} {ev['gorev']}" + (" ✎" if ev.get("yazma") else ""), "dim")

    def _yanit(self, ev):
        if self.app.verbose:
            self.L(f"  ← [{ev['no']}] {ev['ajan']} {len(ev.get('metin', ''))} karakter", "dim")

    def _asama(self, ev):
        self.phase = ev["no"]
        if ev["durum"] == "planlaniyor":
            self.L(f"\n━━ Aşama {ev['no']} planlanıyor…", "bold", "cyan")
        elif ev["durum"] == "bitti":
            self.L(f"━━ Aşama {ev['no']}{': ' + ev['baslik'] if ev.get('baslik') else ''} bitti", "cyan")

    def _plan(self, ev):
        plan = ev["plan"]
        for t in plan.get("tasks", []):
            self.tasks[t["id"]] = {"title": t["title"], "agent": t["assignee"], "status": "bekliyor"}
        head = "Plan (düzenlendi)" if ev.get("duzenlendi") else "Plan"
        if plan.get("phase_title"):
            head += f" — {plan['phase_title']}"
        self.L(f"\n{head}: {plan.get('summary', '')}", "bold")
        for t in plan.get("tasks", []):
            deps = f" ← {', '.join(t['depends_on'])}" if t.get("depends_on") else ""
            write = f"  ✎ {', '.join(t.get('files') or [])}" if t.get("needs_write") else ""
            ver = f"  🧪 {', '.join(t['verify'])}" if t.get("verify") else ""
            self.L(f"  {t['id']:>4}  {t['assignee']:<8} {t['title']}{deps}{write}{ver}")
        if plan.get("done") and not plan.get("tasks"):
            self.L("  (Planlayıcı hedefin tamamlandığını bildirdi.)", "green")

    def _gorev_durumu(self, ev):
        t = self.tasks.setdefault(ev["gorev"], {"title": "", "agent": ev.get("ajan"), "status": ""})
        t["status"] = ev["durum"]
        if ev["durum"] == "calisiyor":
            t["agent"] = ev.get("ajan") or t["agent"]
            self.L(f"▶ {ev['gorev']} {t['title']} — {t['agent']}", "blue")

    def _danisma_basladi(self, ev):
        self.L(f"💬 {ev['soran']} → {ev['hedef']}: {ev['soru'][:160]}", "magenta")

    def _danisma(self, ev):
        self.L(f"   ↩ {ev['hedef']}: {ev['cevap'][:200]}", "dim")

    def _denetim(self, ev):
        if ev.get("otomatik"):
            self.L(f"↺ {ev['gorev']} doğrulama başarısız → otomatik revizyon", "yellow")
        elif ev["karar"] == "approve":
            self.L(f"✓ {ev['gorev']} ONAY", "green")
        else:
            self.L(f"↺ {ev['gorev']} REVİZYON: {ev.get('geri_bildirim', '')[:200]}", "yellow")

    def _dogrulama(self, ev):
        mark, st = ("✓", "green") if ev["basarili"] else ("✗", "red")
        self.L(f"🧪 {ev['gorev']} {ev['komut']} {mark}  ({ev['sure_sn']} sn, çıkış {ev['cikis_kodu']})", st)
        if not ev["basarili"]:
            tail = "\n".join((ev.get("cikti") or "").strip().splitlines()[-8:])
            if tail:
                self.L("   " + tail.replace("\n", "\n   "), "dim")

    def _dosya_degisikligi(self, ev):
        for k, lst in (("+", ev["eklenen"]), ("~", ev["degisen"]), ("-", ev["silinen"])):
            for p in lst:
                self.files[p] = k
        n = len(ev["eklenen"]) + len(ev["degisen"]) + len(ev["silinen"])
        names = ", ".join((ev["eklenen"] + ev["degisen"] + ev["silinen"])[:6])
        self.L(f"📝 {ev['ajan']}: {n} dosya ({names}{'…' if n > 6 else ''})", "dim")
        if ev.get("bildirilmemis"):
            self.L(f"⚠ Bildirilmemiş değişiklik: {', '.join(ev['bildirilmemis'][:8])}", "yellow")

    def _korumali_ihlal(self, ev):
        self.L(f"⛔ İHLAL — {ev['ajan']} ({ev['gorev']}): {ev['neden']}", "bold", "red")
        for y in ev.get("yollar", [])[:10]:
            self.L(f"   {y}", "red")

    def _gorev_sonucu(self, ev):
        t = self.tasks.setdefault(ev["gorev"], {"title": "", "agent": "", "status": ""})
        t["status"] = ev["durum"]
        if ev["durum"] == "tamam":
            warn = f"  ⚠ {'; '.join(ev['uyarilar'])}" if ev.get("uyarilar") else ""
            self.L(f"✔ {ev['gorev']} tamam{warn}", "green")
        else:
            self.L(f"✖ {ev['gorev']} {ev['durum']}: {ev.get('hata', '')[:240]}", "red")

    def _devretme(self, ev):
        self.L(f"⇄ {ev['gorev']}: {ev['eski']} → {ev['yeni']} ({ev['neden'][:120]})", "yellow")

    def _kontrol_noktasi(self, ev):
        self.L(f"⛳ Kontrol noktası: {ev['etiket']}", "dim")

    def _kullanici_notu(self, ev):
        self.L(f"📌 Not → {ev.get('hedef') or 'tüm ekip'}: {ev['metin']}", "cyan")

    def _hata(self, ev):
        self.L(("⚠ " if ev.get("uyari") else "✖ ") + ev["mesaj"], "yellow" if ev.get("uyari") else "red")

    def _iptal(self, ev):
        self.L(f"İptal: {ev['neden']}", "yellow")

    def _devam(self, ev):
        self.L(f"↻ Kaldığı yerden: önceki çağrı {ev['onceki_calls']}, kalan görev {ev['kalan_gorevler']}", "cyan")

    def _durum(self, ev):
        text = {"duraklatildi": ("⏸ Duraklatıldı. Devam için /surdur", "yellow"),
                "devam_ediyor": ("▶ Devam ediyor.", "green"),
                "durduruldu": ("⏹ Durduruldu. Sürdürmek için /devam", "yellow")}.get(ev["durum"])
        if text:
            self.L(*text, model=False)


# ====================================================================== 5. TerminalApp

class TerminalApp:
    def __init__(self, cfg: Config, *, mock: bool = False, mock_speed: float = 1.0):
        self.cfg = cfg
        self.mock = mock
        self.mock_speed = mock_speed
        self.out = Out(self.prompt)
        self.runner: EngineRunner | None = None
        self.last_runner: EngineRunner | None = None
        self.verbose = False
        self.auto_yes = False
        self.state_path = user_config_dir() / "terminal.json"
        self.projects_root = cfg.settings.workspace.expanduser().resolve()
        self.project = self._load_project()
        # Mod config'ten gelir (varsayılan "tek"): basit işler aşama planlaması ödemez. Büyük projeler: /mod asamali
        self._quit = False

    # -------------------------------------------------------------- durum dosyası ve proje

    def _load_project(self) -> Path:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            p = Path(data.get("proje", ""))
            if p.is_absolute():
                return p
        except (OSError, ValueError, TypeError):
            pass
        return self.projects_root

    def _save_project(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"proje": str(self.project)}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _check_ws(self, p: Path) -> Path:
        ws = validate_workspace(p)
        validate_layout(ws, self.cfg.settings.runs_dir, INSTALL_DIR, self.cfg.dir)
        return ws

    @property
    def project_name(self) -> str:
        return self.project.name or str(self.project)

    def prompt(self) -> str:
        r = self.runner
        if r and r.approver.pending:
            kind = r.approver.pending["kind"]
            hint = {"plan": "e onayla · d düzenle · h reddet", "ihlal": "e devam et · h durdur"}.get(kind, "e evet · h hayır")
            return self.out.c(f"onay ({hint})> ", "bold", "yellow")
        busy = " ⏳" if r and r.active else ""
        return self.out.c(f"orkestra[{self.project_name}]{busy}> ", "bold", "cyan")

    # -------------------------------------------------------------- açılış

    def banner(self) -> None:
        o = self.out
        o.line("")
        o.line(f"  ◆ Orkestra {__version__} — çok-ajanlı proje geliştirme terminali" + (" (sahte ajanlar)" if self.mock else ""),
               "bold")
        o.line("    Claude planlar ve denetler · Gemini ve Copilot ekipte · her şey senin hesaplarınla", "dim")
        o.line("")
        self.cmd_hesaplar([], short=True)
        o.line("")
        o.line(f"  Proje : {self.project}", "bold")
        mem = ProjectMemory.load(self.project) if self.project.is_dir() else None
        if mem and not mem.empty:
            o.line(f"          proje hafızası: {mem.summary()}", "dim")
        s = self.cfg.settings
        o.line(f"  Mod   : {'aşamalı' if s.mode == 'asamali' else 'tek plan'} · bütçe {s.max_calls} çağrı · "
               f"en fazla {s.max_phases} aşama · devretme {'açık' if s.fallback else 'kapalı'}", "dim")
        o.line("")
        o.line("  Ne geliştirmek istediğini yaz. Komutlar için /yardim.", "cyan")
        o.line("")

    # -------------------------------------------------------------- ana döngü

    def run(self) -> int:
        self._setup_readline()
        try:
            self.project = self._check_ws(self.project)
        except SecurityError as e:
            self.out.line(f"⚠ Kayıtlı proje klasörü kullanılamıyor ({e}); varsayılana dönülüyor.", "yellow")
            self.project = self._check_ws(self.projects_root)
        self.banner()
        interrupts = 0
        while not self._quit:
            try:
                self.out.in_input = True
                line = input(self.prompt())
                self.out.in_input = False
                self.out.last_submitted = line
                interrupts = 0
            except EOFError:
                self.out.in_input = False
                self.out.line("")
                break
            except KeyboardInterrupt:
                self.out.in_input = False
                self.out.line("")
                if self.runner and self.runner.active:
                    self.out.line("Çalıştırma durduruluyor… (çıkmak için tekrar Ctrl+C)", "yellow")
                    self.runner.stop()
                    interrupts += 1
                    if interrupts >= 2:
                        break
                    continue
                break
            try:
                self.handle(line)
            except KeyboardInterrupt:
                self.out.line("\nİşlem kesildi.", "yellow")
            except Exception as e:  # noqa: BLE001 - kabuk tek bir hatayla kapanmamalı
                self.out.line(f"✖ {redact(str(e))}", "red")
        if self.runner and self.runner.active:
            self.out.line("Aktif çalıştırma durduruluyor ve durum kaydediliyor…", "yellow")
            self.runner.stop()
            self.runner.done.wait(30)
        self._save_history()
        self.out.line("Görüşmek üzere.", "dim")
        return 0

    def handle(self, line: str) -> None:
        r = self.runner
        if r and r.approver.pending:
            r.approver.answer(line)
            return
        text = line.strip()
        if not text:
            return
        if text.startswith("/"):
            parts = text.split()
            cmd, args = parts[0].lower(), parts[1:]
            fn = getattr(self, "cmd_" + cmd[1:].replace("-", "_").replace("ı", "i"), None)
            aliases = {"/help": self.cmd_yardim, "/?": self.cmd_yardim, "/quit": self.cmd_cikis, "/exit": self.cmd_cikis,
                       "/q": self.cmd_cikis, "/geri": self.cmd_geri_al}
            fn = fn or aliases.get(cmd)
            if fn is None:
                self.out.line(f"Bilinmeyen komut: {cmd}. /yardim yaz.", "yellow")
                return
            fn(args)
            return
        if r and r.active:
            self._note(text, None)
        else:
            self.start(text)

    # -------------------------------------------------------------- çalıştırma

    def _run_cfg(self) -> Config:
        cfg = copy.deepcopy(self.cfg)
        cfg.settings.workspace = self.project
        return cfg

    def start(self, goal: str) -> None:
        if len(goal) > 20000:
            self.out.line("Hedef en fazla 20000 karakter olabilir.", "yellow")
            return
        if not self.mock and not self._planner_ready():
            return
        self.out.line(f"\nHedef: {goal}", "bold")
        self.runner = EngineRunner(self, self._run_cfg(), goal, self.project)
        self.runner.start()
        self.out.line(f"Kayıt: {self.runner.run_dir}  ·  çalışırken yazdıkların ekibe not olarak gider; /durdur ile durur.", "dim")

    def _planner_ready(self) -> bool:
        planner = self.cfg.agents[self.cfg.settings.planner]
        if not shutil.which(planner.command[0]):
            self.out.line(f"✖ Planlayıcı ({planner.name}) kurulu değil. Önce /kur ve /baglan {planner.name}.", "red")
            return False
        return True

    def on_run_finished(self, runner: EngineRunner) -> None:
        o = self.out
        self.last_runner = runner
        if runner.report:
            o.line("\n━━━━━━━━━━━━━━━━━━━━━━ SONUÇ RAPORU ━━━━━━━━━━━━━━━━━━━━━━", "bold", "green")
            o.line(runner.report, model=True)
            o.line("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "bold", "green")
        elif runner.error:
            o.line(f"\n✖ {runner.error}", "red")
        orch = runner.orch
        if orch:
            self._usage_lines(orch)
            if runner.printer.files:
                o.line(f"Değişen dosyalar: {len(runner.printer.files)} (/dosyalar · /fark <dosya>)", "dim")
            if orch.start_checkpoint:
                o.line("Beğenmezsen projeyi başlangıç haline döndürebilirsin: /kontrol ve /geri-al <no>", "dim")
            if runner.error and orch.state and orch.state.durum != "bitti":
                o.line("Kaldığı yerden sürdürmek için: /devam", "cyan")
        o.line(f"Rapor ve kayıtlar: {runner.run_dir}", "dim")
        o.bell()

    def _note(self, text: str, hedef: str | None) -> None:
        if not (self.runner and self.runner.active):
            self.out.line("Aktif çalıştırma yok; notlar çalışma sırasında eklenir.", "yellow")
            return
        try:
            self.runner.add_note(text, hedef)
        except ValueError as e:
            self.out.line(f"Not eklenemedi: {e}", "yellow")

    def show_approval(self, kind: str, payload: dict) -> None:
        o = self.out
        o.bell()
        if kind == "plan":
            o.line("")
            if payload.get("has_write"):
                o.line("✎ Bu plan proje klasöründe dosya YAZACAK.", "yellow")
            cmds = payload.get("verify_commands") or []
            if cmds:
                o.line("🧪 Çalıştırılacak doğrulama komutları: " + ", ".join(f"{c['name']} ({' '.join(c['argv'])})" for c in cmds), "yellow")
                o.line("   " + VERIFY_WARNING, "dim")
            o.line("Planı onaylıyor musun?  e = onayla · d = düzenle (editörde) · h = reddet", "bold", "yellow")
        elif kind == "baslangic_uyarisi":
            o.line("⚠ Ajanları yönlendirebilecek talimat/konfig dosyaları bulundu (CLI'lar bunları otomatik okur):", "yellow")
            for f in payload.get("dosyalar", []):
                o.line(f"   {f}", "yellow")
            o.line("İçeriklerini kontrol ettiysen devam et.  e = devam · h = iptal", "bold", "yellow")
        elif kind == "ihlal":
            o.line("⛔ Korumalı yol veya izinsiz değişiklik! Yeni çağrılar beklemede.", "bold", "red")
            o.line("   Orkestra değişiklikleri kendiliğinden geri almaz; istersen sonra /geri-al kullan.", "red")
            o.line("e = devam et · h = durdur", "bold", "red")

    # -------------------------------------------------------------- hesaplar

    def cmd_hesaplar(self, args=None, short: bool = False) -> None:
        o = self.out
        rows = accounts.rows(self.cfg)
        o.line("  Hesaplar" + ("" if short else " (her kullanıcı kendi aboneliğiyle giriş yapar; Orkestra şifre saklamaz)"),
               "bold")
        for r in rows:
            role = "planlayıcı" if r["planlayici"] else ("denetçi" if r["denetci"] else "ekip")
            if not r["kurulu"]:
                st = o.c("✖ kurulu değil", "red") + o.c("  → /kur", "dim")
            elif r["test"] is True:
                st = o.c("✔ bağlı", "green") + o.c(f"  (son test {time.strftime('%d.%m %H:%M', time.localtime(r['test_zamani']))})", "dim")
            elif r["test"] is False:
                st = o.c("⚠ son test başarısız", "yellow") + o.c(f"  → /baglan {r['ajan']}", "dim")
            else:
                st = o.c("● kurulu", "cyan") + o.c(f"  → /baglan {r['ajan']} · /test {r['ajan']}", "dim")
            o.line(f"    {r['ajan']:<8} {role:<11} {st}")
            if not short and r["test"] is False and r["test_mesaji"]:
                o.line(f"             {r['test_mesaji'][:160]}", "dim")
        if self.mock and not short:
            o.line("    (Sahte ajan modu: hesaplar kullanılmaz.)", "dim")

    def cmd_kur(self, args=None) -> None:
        pkgs = accounts.missing_packages(self.cfg)
        if not pkgs:
            self.out.line("Tüm CLI araçları kurulu. ✔", "green")
            return
        self.out.line(f"Kurulacak: npm install -g {' '.join(pkgs)}", "bold")
        if not self._confirm("Kurulsun mu?"):
            return
        try:
            code = accounts.install(pkgs)
        except (FileNotFoundError, SecurityError) as e:
            self.out.line(f"✖ {e}", "red")
            return
        self.out.line("Kurulum tamam. ✔ Şimdi /baglan ile hesaplarına bağlan." if code == 0
                      else f"npm çıkış kodu {code}. Yetki hatası aldıysan npm belgelerindeki 'global install' adımlarına bak.",
                      "green" if code == 0 else "yellow")

    def _pick_agents(self, args) -> list[str]:
        if not args or args[0] in ("hepsi", "tum", "tümü"):
            return list(self.cfg.agents)
        name = args[0].lower()
        if name not in self.cfg.agents:
            self.out.line(f"Bilinmeyen ajan: {name}. Seçenekler: {', '.join(self.cfg.agents)}", "yellow")
            return []
        return [name]

    def cmd_baglan(self, args=None) -> None:
        if self.runner and self.runner.active:
            self.out.line("Çalıştırma sürerken hesap bağlanamaz.", "yellow")
            return
        for name in self._pick_agents(args or []):
            a = self.cfg.agents[name]
            tool = accounts.tool_of(a)
            info = accounts.TOOLS.get(tool or "", {})
            if not shutil.which(a.command[0]):
                self.out.line(f"✖ {name} kurulu değil. Önce /kur.", "red")
                continue
            self.out.line(f"\n── {name} hesabını bağla ({info.get('abonelik', a.display)}) ──", "bold", "cyan")
            for i, step in enumerate(info.get("giris", ["CLI açılınca giriş yap ve çık."]), 1):
                self.out.line(f"  {i}. {step}")
            if info.get("etkilesimli") is False:
                # Yerel API ajanı (ör. Ollama): açılacak bir giriş ekranı yok.
                if self._confirm("Bağlantıyı test edelim mi?", default=True):
                    self._probe(name)
                continue
            if not self._confirm(f"{a.command[0]} şimdi açılsın mı?", default=True):
                continue
            try:
                accounts.login(a, self.project)
            except (FileNotFoundError, SecurityError, OSError) as e:
                self.out.line(f"✖ {e}", "red")
                continue
            self.out.line(f"{name} kapandı.", "dim")
            if self._confirm("Bağlantıyı küçük bir istemle test edelim mi? (çok küçük bir kullanım harcar)", default=True):
                self._probe(name)

    def cmd_test(self, args=None) -> None:
        names = self._pick_agents(args or [])
        if names and not self._confirm(f"{', '.join(names)} için bağlantı testi yapılsın mı? (her biri çok küçük bir kullanım harcar)",
                                       default=True):
            return
        for name in names:
            self._probe(name)

    def _probe(self, name: str) -> None:
        if self.mock:
            self.out.line(f"  {name}: sahte modda test edilmez.", "dim")
            return
        self.out.line(f"  {name} deneniyor…", "dim")
        ok, msg = accounts.probe_sync(self.cfg.agents[name], self.project)
        self.out.line(f"  {'✔' if ok else '✖'} {name}: {msg[:400]}", "green" if ok else "red", model=True)

    # -------------------------------------------------------------- proje

    def cmd_proje(self, args=None) -> None:
        if not args:
            self.out.line(f"Proje: {self.project}", "bold")
            mem = ProjectMemory.load(self.project)
            self.out.line(f"Proje hafızası: {mem.summary() if not mem.empty else 'henüz yok'}", "dim")
            return
        if self.runner and self.runner.active:
            self.out.line("Çalıştırma sürerken proje değiştirilemez.", "yellow")
            return
        arg = " ".join(args)
        if PROJECT_NAME_RE.match(arg) and not arg.startswith("."):
            target = self.projects_root / arg
        else:
            target = Path(arg).expanduser()
            if not target.is_absolute():
                target = Path.cwd() / target
        try:
            self.project = self._check_ws(target)
        except SecurityError as e:
            self.out.line(f"✖ {e}", "red")
            return
        self._save_project()
        self.out.line(f"✔ Proje: {self.project}", "green")
        mem = ProjectMemory.load(self.project)
        if not mem.empty:
            self.out.line(f"  Proje hafızası: {mem.summary()} — ekip kaldığı yerden devam eder.", "dim")
        if any(r["arac"] == "copilot" and r["kurulu"] for r in accounts.rows(self.cfg)):
            self.out.line("  Not: Copilot yeni bir klasörde güven onayı ister: /baglan copilot (bir kez).", "dim")

    def cmd_projeler(self, args=None) -> None:
        root = self.projects_root
        items = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")) if root.is_dir() else []
        self.out.line(f"Proje kökü: {root}", "bold")
        if not items:
            self.out.line("  Henüz proje yok. Oluşturmak için: /proje <ad>", "dim")
        for p in items:
            mem = ProjectMemory.load(p)
            mark = "▸" if p == self.project else " "
            self.out.line(f"  {mark} {p.name:<24} {mem.summary() if not mem.empty else ''}", "dim" if p != self.project else "bold")

    # -------------------------------------------------------------- ayarlar

    def _toggle(self, args, attr: str, label: str, obj=None) -> None:
        obj = obj or self.cfg.settings
        if not args or args[0] not in ("ac", "aç", "kapat"):
            self.out.line(f"{label}: {'açık' if getattr(obj, attr) else 'kapalı'}  (kullanım: ac | kapat)")
            return
        setattr(obj, attr, args[0] != "kapat")
        self.out.line(f"✔ {label} {'açıldı' if getattr(obj, attr) else 'kapatıldı'} (sonraki çalıştırmalarda geçerli).", "green")

    def cmd_sikimod(self, args=None):
        self._toggle(args, "strict_guard", "Sıkı mod")

    def cmd_devretme(self, args=None):
        self._toggle(args, "fallback", "Devretme")

    def cmd_otomatik(self, args=None):
        if args and args[0] in ("ac", "aç", "kapat"):
            self.auto_yes = args[0] != "kapat"
        self.out.line(f"Salt-okunur planları otomatik onay: {'açık' if self.auto_yes else 'kapalı'}"
                      " (dosya yazan veya komut çalıştıran planlar her zaman sorulur)")

    def cmd_hizli(self, args=None):
        """Hızlı profil: basit işler için tek plan, danışma ve denetim turları kapalı (bu oturum için)."""
        s = self.cfg.settings
        if not hasattr(self, "_normal_profile"):
            self._normal_profile = (s.mode, s.max_consult_rounds, s.max_revisions)
        if args and args[0] in ("ac", "aç"):
            s.mode, s.max_consult_rounds, s.max_revisions = "tek", 0, 0
        elif args and args[0] == "kapat":
            s.mode, s.max_consult_rounds, s.max_revisions = self._normal_profile
        fast = s.mode == "tek" and s.max_consult_rounds == 0 and s.max_revisions == 0
        self.out.line(f"Hızlı mod: {'açık' if fast else 'kapalı'}  (mod {s.mode}, danışma turu {s.max_consult_rounds}, "
                      f"denetim turu {s.max_revisions})")
        if fast:
            self.out.line("  Görev başına tek çağrı; Claude yine plan yapar ve rapor yazar. Büyük işlerde /hizli kapat.", "dim")

    def cmd_model(self, args=None):
        """Ajanın modelini bu oturum için seçer (CLI'ın --model bayrağıyla). Kalıcı yapmak için config.toml."""
        if not args:
            for name, a in self.cfg.agents.items():
                cur = next((a.command[i + 1] for i, x in enumerate(a.command[:-1]) if x == "--model"), "varsayılan")
                self.out.line(f"  {name:<14} model: {cur}")
            self.out.line("  Değiştir: /model <ajan> <model>   ör. /model claude sonnet · /model claude haiku · "
                          "/model claude varsayilan", "dim")
            self.out.line("  Copilot'un modelleri için PowerShell'de: copilot --help (--model satırı)", "dim")
            return
        name = args[0].lower()
        if name not in self.cfg.agents or len(args) != 2:
            self.out.line(f"Kullanım: /model <ajan> <model>. Ajanlar: {', '.join(self.cfg.agents)}", "yellow")
            return
        model = args[1]
        if model not in ("varsayilan", "varsayılan") and not MODEL_RE.match(model):
            self.out.line("Geçersiz model adı.", "yellow")
            return
        a = self.cfg.agents[name]

        def strip(argv):
            out, skip = [], False
            for x in argv:
                if skip:
                    skip = False
                    continue
                if x == "--model":
                    skip = True
                    continue
                out.append(x)
            return out
        a.command = strip(a.command)
        a.stream_command = strip(a.stream_command)
        if model not in ("varsayilan", "varsayılan"):
            a.command = a.command + ["--model", model]
            if a.stream_command:
                a.stream_command = a.stream_command + ["--model", model]
        self.out.line(f"✔ {name} modeli: {model} (bu oturum için; sonraki işlerde geçerli).", "green")
        self.out.line(f"  Kalıcı yapmak için config.toml'da [agents.{name}] command listesine \"--model\", \"{model}\" ekle."
                      if model not in ("varsayilan", "varsayılan") else "", "dim")

    # -------------------------------------------------------------- ekip: katalog, ekle, çıkar, aç/kapat (0.4)

    def _reload_agents(self) -> None:
        new = load_config(self.cfg.path)
        self.cfg.agents = new.agents

    def _team_busy(self) -> bool:
        if self.runner and self.runner.active:
            self.out.line("Çalıştırma sürerken ekip değiştirilemez. Önce /durdur.", "yellow")
            return True
        return False

    def cmd_katalog(self, args=None) -> None:
        states = configedit.agent_states(self.cfg.path)
        self.out.line("Eklenebilir yapay zekâlar (isteğe bağlı; eklemek için /ekle <ad>):", "bold")
        for e in katalog.CATALOG:
            if e.ad in states:
                st = self.out.c("✔ ekipte" if states[e.ad] else "○ ekli ama kapalı", "green" if states[e.ad] else "yellow")
            else:
                st = self.out.c("eklenmedi", "dim")
            tag = self.out.c(" [deneysel]", "yellow") if e.durum == "deneysel" else ""
            self.out.line(f"  {e.ad:<14} {e.baslik}{tag}  —  {st}")
            self.out.line(f"  {'':<14} {e.aciklama}", "dim")
            self.out.line(f"  {'':<14} Ücret: {e.ucret}", "dim")
        self.out.line("  Ayrıntı: /ekle <ad> önce bilgileri gösterir ve onay ister. Gemini gibi hazır ajanlar: /ajan ac|kapat <ad>",
                      "dim")

    def cmd_ekle(self, args=None) -> None:
        if not args:
            self.out.line("Kullanım: /ekle <ad>  (liste için /katalog)", "yellow")
            return
        if self._team_busy():
            return
        e = katalog.get(args[0].lower())
        if e is None:
            self.out.line(f"Katalogda '{args[0]}' yok. Liste için /katalog", "yellow")
            return
        o = self.out
        o.line(f"\n── {e.baslik} ({e.saglayici}) ──", "bold", "cyan")
        o.line(f"  {e.aciklama}")
        o.line(f"  Ücret/abonelik: {e.ucret}")
        if e.durum == "deneysel":
            o.line("  Durum: DENEYSEL — komutlar aracın belgelerinden alındı ama gerçek bir hesapla burada denenmedi. "
                   "Ekledikten sonra /test ile doğrula.", "yellow")
        for n in e.notlar:
            o.line(f"  • {n}", "dim")
        if e.kaynak:
            o.line(f"  Kaynak: {e.kaynak}", "dim")
        o.line("  Bu aracın kullanım koşullarında başka uygulamalardan çağırmaya izin verildiğinden emin ol.", "dim")
        if sys.stdin.isatty() and not self._confirm("Ekibe eklensin mi?", default=True):
            return
        try:
            configedit.add_agent(self.cfg.path, e.ad, e.ayar)
            self._reload_agents()
        except (configedit.ConfigEditError, OSError) as ex:
            o.line(f"✖ Eklenemedi: {ex}", "red")
            return
        o.line(f"✔ {e.ad} ekibe eklendi. (Yedek: {self.cfg.path.name}.yedek)", "green")
        if not shutil.which(e.ayar["command"][0]):
            if e.npm:
                o.line(f"  Kurulu değil. Kurmak için: /kur   (npm install -g {e.npm})", "yellow")
            elif e.kurulum:
                o.line(f"  Kurulu değil. {e.kurulum}", "yellow")
        o.line(f"  Sonra: /baglan {e.ad} ve /test {e.ad}", "cyan")

    def cmd_cikar(self, args=None) -> None:
        if not args:
            self.out.line("Kullanım: /cikar <ad>", "yellow")
            return
        if self._team_busy():
            return
        ad = args[0].lower()
        try:
            configedit.remove_agent(self.cfg.path, ad)
            self._reload_agents()
        except (configedit.ConfigEditError, OSError) as ex:
            self.out.line(f"✖ {ex}", "red")
            return
        self.out.line(f"✔ {ad} ekipten çıkarıldı.", "green")

    def cmd_ajanlar(self, args=None) -> None:
        states = configedit.agent_states(self.cfg.path)
        s = self.cfg.settings
        self.out.line("Ekip:", "bold")
        for name, on in states.items():
            role = "planlayıcı" if name == s.planner else ("denetçi" if name == s.reviewer else "ekip")
            src = " (katalog)" if configedit.from_catalog(self.cfg.path, name) else ""
            mark = self.out.c("● açık", "green") if on else self.out.c("○ kapalı", "dim")
            self.out.line(f"  {name:<14} {role:<11} {mark}{src}")
        self.out.line("  Aç/kapat: /ajan ac <ad> · /ajan kapat <ad>   Yeni ekle: /katalog", "dim")

    def cmd_ajan(self, args=None) -> None:
        if not args or len(args) != 2 or args[0] not in ("ac", "aç", "kapat"):
            self.out.line("Kullanım: /ajan ac <ad>  veya  /ajan kapat <ad>", "yellow")
            return
        if self._team_busy():
            return
        ad, on = args[1].lower(), args[0] != "kapat"
        try:
            configedit.set_enabled(self.cfg.path, ad, on)
            self._reload_agents()
        except (configedit.ConfigEditError, OSError) as ex:
            self.out.line(f"✖ {ex}", "red")
            return
        self.out.line(f"✔ {ad} {'açıldı' if on else 'kapatıldı'}.", "green")

    def cmd_ayrinti(self, args=None):
        if args and args[0] in ("ac", "aç", "kapat"):
            self.verbose = args[0] != "kapat"
        self.out.line(f"Ayrıntılı çıktı: {'açık' if self.verbose else 'kapalı'}")

    def cmd_mod(self, args=None):
        if args and args[0] in ("tek", "asamali", "aşamalı"):
            self.cfg.settings.mode = "tek" if args[0] == "tek" else "asamali"
        self.out.line(f"Mod: {'aşamalı (büyük projeler)' if self.cfg.settings.mode == 'asamali' else 'tek plan'}")

    def _int_setting(self, args, attr, label, minimum):
        if args:
            try:
                v = int(args[0])
                if v < minimum:
                    raise ValueError
                setattr(self.cfg.settings, attr, v)
            except ValueError:
                self.out.line(f"{label} en az {minimum} olan bir tam sayı olmalı.", "yellow")
                return
        self.out.line(f"{label}: {getattr(self.cfg.settings, attr)}")

    def cmd_butce(self, args=None):
        self._int_setting(args, "max_calls", "Çağrı bütçesi", 3)

    def cmd_asama(self, args=None):
        self._int_setting(args, "max_phases", "En fazla aşama", 1)

    def cmd_dogrulama(self, args=None) -> None:
        v = self.cfg.verify
        if args and args[0] == "ekle" and len(args) >= 3:
            name = args[1].lower()
            if not is_safe_id(name):
                self.out.line("Ad küçük harf, rakam, - ve _ içermeli.", "yellow")
                return
            argv = shlex.split(" ".join(args[2:]), posix=os.name != "nt")
            v.commands = [c for c in v.commands if c.name != name] + [VerifyCommand(name, argv, 300)]
            self.out.line(f"✔ '{name}' eklendi (bu oturum için). Planlayıcı görevlerine bu adla bağlayabilir.", "green")
            return
        if args and args[0] == "sil" and len(args) == 2:
            v.commands = [c for c in v.commands if c.name != args[1]]
            self.out.line(f"✔ '{args[1]}' kaldırıldı.", "green")
            return
        self.out.line(f"Doğrulama komutları (yalıtım: {v.sandbox}):", "bold")
        for c in v.commands:
            self.out.line(f"  {c.name:<14} {' '.join(c.argv)}")
        self.out.line("  Ekle: /dogrulama ekle <ad> <komut…>   ör. /dogrulama ekle pytest python -m pytest -q", "dim")
        self.out.line("  Kalıcı yapmak için config.toml içindeki [[verify.commands]] bölümünü düzenle: " + str(self.cfg.path), "dim")

    # -------------------------------------------------------------- çalışma durumu

    def _current(self) -> EngineRunner | None:
        return self.runner or self.last_runner

    def cmd_durum(self, args=None) -> None:
        r = self._current()
        if not r:
            self.out.line("Bu oturumda henüz çalıştırma yok.")
            return
        orch = r.orch
        state = "çalışıyor" if r.active else ("bitti" if r.report else "durdu")
        if orch and orch.paused:
            state = "duraklatıldı"
        self.out.line(f"Durum: {state} · {int(time.time() - r.started)} sn · çağrı {orch.calls if orch else 0}/"
                      f"{r.cfg.settings.max_calls}" + (f" · aşama {r.printer.phase}" if r.printer.phase else ""), "bold")
        self.cmd_gorevler()

    def cmd_gorevler(self, args=None) -> None:
        r = self._current()
        if not r or not r.printer.tasks:
            self.out.line("Görev yok.")
            return
        icons = {"tamam": "✔", "hata": "✖", "atlandi": "⤼", "calisiyor": "▶", "denetimde": "🔍", "dogrulamada": "🧪"}
        for tid, t in r.printer.tasks.items():
            self.out.line(f"  {icons.get(t['status'], '·')} {tid:>4} {t['agent'] or '':<8} {t['title']}  [{t['status']}]", model=True)

    def cmd_dosyalar(self, args=None) -> None:
        r = self._current()
        files = r.printer.files if r else {}
        if not files:
            self.out.line("Bu çalıştırmada değişen dosya yok.")
            return
        for p, k in sorted(files.items()):
            self.out.line(f"  {k} {p}", "green" if k == "+" else ("red" if k == "-" else ""), model=True)

    def _usage_lines(self, orch: Orchestrator) -> None:
        if not orch.usage:
            return
        parts = []
        for name, u in orch.usage.items():
            cost = f", ≈${u['maliyet_usd']:.2f}" if u.get("maliyet_usd") else ""
            cache = f" (+{u.get('onbellek', 0):,} önbellek)" if u.get("onbellek") else ""
            parts.append(f"{name} {u['cagri']} çağrı / {u['girdi'] + u['cikti']:,} token{cache}{cost}".replace(",", "."))
        self.out.line("Kullanım: " + " · ".join(parts) + "   (ayrıntı: /analiz)", "dim")

    def cmd_kullanim(self, args=None) -> None:
        r = self._current()
        if not r or not r.orch:
            self.out.line("Henüz kullanım yok.")
            return
        self._usage_lines(r.orch)
        self.out.line("(Maliyet, CLI'ın bildirdiği API eşdeğeri tahminidir; aboneliğinden ayrıca ücret alınmaz,"
                      " ama aboneliğin kullanım sınırına sayılır.)", "dim")

    def cmd_not(self, args=None) -> None:
        if not args:
            self.out.line("Kullanım: /not [@ajan] <metin>", "yellow")
            return
        hedef = None
        if args[0].startswith("@"):
            hedef, args = args[0][1:].lower(), args[1:]
        self._note(" ".join(args), hedef)

    def cmd_duraklat(self, args=None):
        if self.runner and self.runner.active and self.runner.orch:
            self.runner.call(self.runner.orch.pause)
        else:
            self.out.line("Aktif çalıştırma yok.")

    def cmd_surdur(self, args=None):
        if self.runner and self.runner.active and self.runner.orch:
            self.runner.call(self.runner.orch.resume)
        else:
            self.out.line("Aktif çalıştırma yok. Yarım kalanı sürdürmek için /devam")

    def cmd_durdur(self, args=None):
        if self.runner and self.runner.active:
            self.out.line("Durduruluyor… (durum kaydedilir, /devam ile sürdürülür)", "yellow")
            self.runner.stop()
        else:
            self.out.line("Aktif çalıştırma yok.")

    # -------------------------------------------------------------- geçmiş, devam, rapor

    def _project_runs(self) -> list[tuple[Path, dict]]:
        root = self.cfg.settings.runs_dir
        out = []
        if not root.is_dir():
            return out
        for d in sorted((p for p in root.iterdir() if p.is_dir() and RUN_NAME_RE.match(p.name)), reverse=True):
            try:
                st = json.loads((d / STATE_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(st, dict) and st.get("calisma_klasoru") == str(self.project):
                out.append((d, st))
        return out[:30]

    def cmd_gecmis(self, args=None) -> None:
        runs = self._project_runs()
        if not runs:
            self.out.line("Bu projede henüz çalıştırma yok.")
            return
        for i, (d, st) in enumerate(runs, 1):
            goal = str(st.get("hedef", "")).splitlines()[0][:70] if st.get("hedef") else ""
            self.out.line(f"  {i:>2}. {d.name}  {st.get('durum', '?'):<11} {goal}", model=True)
        self.out.line("  Raporu görmek: /rapor <no> · yarım kalanı sürdürmek: /devam <no>", "dim")

    def _pick_run(self, args) -> tuple[Path, dict] | None:
        runs = self._project_runs()
        if not runs:
            self.out.line("Bu projede çalıştırma yok.")
            return None
        if not args:
            return runs[0]
        try:
            return runs[int(args[0]) - 1]
        except (ValueError, IndexError):
            self.out.line("Geçersiz numara. /gecmis ile listele.", "yellow")
            return None

    def cmd_analiz(self, args=None) -> None:
        picked = self._pick_run(args or [])
        if not picked:
            return
        try:
            lines = analyze_run(picked[0])
        except OSError as e:
            self.out.line(f"✖ Kayıt okunamadı: {e}", "red")
            return
        for ln in lines:
            self.out.line(ln, model=True)
        self.out.line("  (İstem ve yanıt metinleri gösterilmez; bu çıktıyı güvenle paylaşabilirsin.)", "dim")

    def cmd_rapor(self, args=None) -> None:
        picked = self._pick_run(args or [])
        if not picked:
            return
        p = picked[0] / "sonuc.md"
        if not p.is_file():
            self.out.line("Bu çalıştırmanın raporu yok (yarım kalmış olabilir).")
            return
        self.out.line(p.read_text(encoding="utf-8"), model=True)

    def cmd_devam(self, args=None) -> None:
        if self.runner and self.runner.active:
            self.out.line("Zaten bir çalıştırma sürüyor.", "yellow")
            return
        if not args:
            runs = [r for r in self._project_runs() if r[1].get("durum") != "bitti"]
            if not runs:
                self.out.line("Yarım kalan çalıştırma yok.")
                return
            picked = runs[0]
        else:
            picked = self._pick_run(args)
            if not picked:
                return
        d, _ = picked
        cfg = self._run_cfg()
        try:
            state, plans = load_state(d, cfg)
        except StateError as e:
            self.out.line(f"✖ Sürdürülemiyor: {e}", "red")
            return
        if state.durum == "bitti":
            self.out.line("Bu çalıştırma zaten tamamlanmış.")
            return
        cfg.settings.mode = state.mod
        self.out.line(f"↻ Sürdürülüyor: {d.name}", "cyan")
        self.runner = EngineRunner(self, cfg, state.hedef, self.project, run_dir=d, state=state, plans=plans)
        self.runner.start()

    # -------------------------------------------------------------- kontrol noktaları

    def _store(self) -> CheckpointStore | None:
        store = CheckpointStore(self.project, self.cfg.settings.runs_dir)
        if not store.available:
            self.out.line("git kurulu değil; kontrol noktaları kullanılamıyor. https://git-scm.com/", "yellow")
            return None
        return store

    def cmd_kontrol(self, args=None) -> None:
        store = self._store()
        if not store:
            return
        cps = store.list(30)
        if not cps:
            self.out.line("Bu projede henüz kontrol noktası yok (ilk çalıştırmada otomatik alınır).")
            return
        for i, cp in enumerate(cps, 1):
            self.out.line(f"  {i:>2}. {time.strftime('%d.%m %H:%M', time.localtime(cp.zaman))}  {cp.sha[:8]}  {cp.etiket}",
                          model=True)
        self.out.line("  Geri dönmek: /geri-al <no>  ·  bir dosyanın farkı: /fark <dosya>", "dim")

    def cmd_geri_al(self, args=None) -> None:
        if self.runner and self.runner.active:
            self.out.line("Çalıştırma sürerken geri alınamaz. Önce /durdur.", "yellow")
            return
        store = self._store()
        if not store or not args:
            if store:
                self.out.line("Kullanım: /geri-al <no>  (numaralar için /kontrol)", "yellow")
            return
        cps = store.list(30)
        try:
            cp = cps[int(args[0]) - 1]
        except (ValueError, IndexError):
            self.out.line("Geçersiz numara. /kontrol ile listele.", "yellow")
            return
        try:
            changes = store.changes_since(cp.sha)
        except CheckpointError as e:
            self.out.line(f"✖ {e}", "red")
            return
        if not changes:
            self.out.line("Proje zaten bu kontrol noktasıyla aynı.")
            return
        self.out.line(f"'{cp.etiket}' noktasına dönülecek. Etkilenecek dosyalar:", "bold", model=True)
        labels = {"A": "silinecek (sonradan eklenmiş)", "M": "eski haline dönecek", "D": "geri gelecek"}
        for st, p in changes[:40]:
            self.out.line(f"   {labels.get(st, st)}: {p}", model=True)
        if len(changes) > 40:
            self.out.line(f"   … ve {len(changes) - 40} dosya daha", "dim")
        self.out.line("Şu anki hal önce kaydedilir; bu işlem de /geri-al ile geri alınabilir.", "dim")
        if not self._confirm("Geri alınsın mı?"):
            return
        try:
            store.restore(cp.sha)
        except CheckpointError as e:
            self.out.line(f"✖ {e}", "red")
            return
        self.out.line("✔ Proje geri alındı.", "green")

    def cmd_fark(self, args=None) -> None:
        if not args:
            self.out.line("Kullanım: /fark <dosya>", "yellow")
            return
        rel = normalize_rel_path(" ".join(args))
        if rel is None or rel.endswith("/"):
            self.out.line("Geçersiz dosya yolu.", "yellow")
            return
        store = self._store()
        if not store:
            return
        r = self._current()
        base = r.orch.start_checkpoint if r and r.orch and r.orch.start_checkpoint else None
        if base is None:
            cps = store.list(1)
            base = cps[0].sha if cps else None
        if base is None:
            self.out.line("Karşılaştırılacak kontrol noktası yok.")
            return
        try:
            diff = store.diff(base, rel)
        except CheckpointError as e:
            self.out.line(f"✖ {e}", "red")
            return
        for line in diff.splitlines()[:400]:
            style = "green" if line.startswith("+") and not line.startswith("+++") else (
                "red" if line.startswith("-") and not line.startswith("---") else ("cyan" if line.startswith("@@") else ""))
            self.out.line(line, *([style] if style else []), model=True)

    # -------------------------------------------------------------- diğer

    def cmd_yardim(self, args=None) -> None:
        self.out.line(HELP)

    def cmd_temizle(self, args=None) -> None:
        if self.out.tty:
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()

    def cmd_cikis(self, args=None) -> None:
        self._quit = True

    def _confirm(self, question: str, default: bool = False) -> bool:
        if not sys.stdin.isatty():
            return default
        try:
            a = input(self.out.c(f"{question} [{'E/h' if default else 'e/H'}] ", "bold")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return default if not a else a in YES_ANSWERS

    # -------------------------------------------------------------- readline

    def _setup_readline(self) -> None:
        if readline is None:
            return
        hist = user_config_dir() / "terminal_gecmisi.txt"
        try:
            hist.parent.mkdir(parents=True, exist_ok=True)
            if hist.exists():
                readline.read_history_file(str(hist))
            readline.set_history_length(1000)
        except OSError:
            pass

        def complete(text, state):
            opts = [c for c in COMMANDS if c.startswith(text)] if text.startswith("/") else []
            return opts[state] if state < len(opts) else None
        readline.set_completer(complete)
        readline.set_completer_delims(" ")
        readline.parse_and_bind("tab: complete")

    def _save_history(self) -> None:
        if readline is None:
            return
        try:
            readline.write_history_file(str(user_config_dir() / "terminal_gecmisi.txt"))
        except OSError:
            pass


def main_terminal(cfg: Config, mock: bool = False) -> int:
    return TerminalApp(cfg, mock=mock).run()
