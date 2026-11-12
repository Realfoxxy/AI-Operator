"""CLI giriş noktası: python -m orkestra run | devam | doctor (bölüm 6.8, 6.9).

Çıkış kodları: 0 başarı · 1 çalışma hatası/iptal · 2 config, güvenlik veya girdi hatası · 130 Ctrl+C.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from pathlib import Path

from . import __version__
from .agents import AgentError, CliAgent, build_agents
from .approvals import CliApprover
from .config import Config, ConfigError, default_config_path, load_config
from .engine import BudgetExceeded, Orchestrator, RunCancelled, RunStopped
from .events import ConsolePrinter, EventBus
from .planner import PlanError
from .security import SecurityError, clean_terminal, redact, validate_layout, validate_workspace
from .state import ProjectMemory, StateError, load_state
from .transcript import Transcript

MAX_GOAL_CHARS = 20000
INSTALL_DIR = Path(__file__).resolve().parent.parent


def _setup_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def _err(msg: str) -> None:
    print(f"Hata: {clean_terminal(redact(msg))}", file=sys.stderr)


# ====================================================================== yardımcılar

def _read_goal(args) -> str:
    if args.hedef_dosyasi:
        try:
            return Path(args.hedef_dosyasi).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise ValueError(f"Hedef dosyası okunamadı: {e}") from e
    if args.hedef:
        return args.hedef
    if sys.stdin.isatty():
        print("Hedefi yaz; bitirince Ctrl+D (Windows'ta Ctrl+Z ve Enter) bas:")
    return sys.stdin.read()


def _load_config(args) -> Config:
    return load_config(Path(args.config) if args.config else default_config_path())


def _check_workspace(cfg: Config, workspace: Path) -> Path:
    """Çalışma klasörünü doğrular (kural 4) ve yerleşim kuralını uygular (kural 15)."""
    ws = validate_workspace(workspace)
    validate_layout(ws, cfg.settings.runs_dir, INSTALL_DIR, cfg.dir)
    return ws


def _make_agents(cfg: Config, mock: bool):
    return build_agents(cfg.agents, mock=mock)


async def _drive(orch: Orchestrator, goal: str) -> str:
    return await orch.run(goal)


def _execute(orch: Orchestrator, goal: str, bus: EventBus, settings_max_calls: int) -> int:
    """Motoru çalıştırır ve hataları çıkış kodlarına çevirir."""
    try:
        report = asyncio.run(_drive(orch, goal))
    except RunCancelled as e:
        print(f"İptal edildi: {e}")
        return 1
    except RunStopped as e:
        _err(str(e))
        if orch.run_dir:
            print(f"Durum kaydedildi. Sürdürmek için: python -m orkestra devam {orch.run_dir}")
        return 1
    except (PlanError, AgentError, SecurityError, BudgetExceeded) as e:
        msg = redact(str(e))
        bus.publish("hata", mesaj=msg)
        _err(msg)
        return 1
    except KeyboardInterrupt:
        bus.publish("iptal", neden="Kullanıcı Ctrl+C ile durdurdu.")
        print("\nKullanıcı tarafından durduruldu.", file=sys.stderr)
        if orch.run_dir:
            print(f"Durum kaydedildi. Sürdürmek için: python -m orkestra devam {orch.run_dir}", file=sys.stderr)
        return 130
    print("\n=== SONUÇ RAPORU ===\n")
    print(clean_terminal(redact(report)))
    print(f"\nToplam model çağrısı: {orch.calls}/{settings_max_calls}")
    if orch.run_dir:
        print(f"Rapor: {orch.run_dir / 'sonuc.md'}")
    return 0


# ====================================================================== ui (bölüm 10.8)

def _serve(cfg: Config, args, *, goal: str | None = None, resume: str | None = None) -> int:
    """Yerel sunucuyu başlatır; isteğe bağlı olarak bir çalıştırmayı hemen başlatır. Ctrl+C ile kapanır."""
    from .server import ApiError, OrkestraServer
    port = getattr(args, "port", None)
    app = OrkestraServer(cfg, mock=args.mock, port=port,
                         open_browser=False if getattr(args, "no_browser", False) else None)
    try:
        app.start()
    except OSError as e:
        _err(str(e))
        return 2
    print(f"Orkestra {__version__} görsel çalışma alanı" + (" (sahte ajanlar)" if args.mock else ""))
    # Token konsola yalnızca açılış adresinin parçası olarak bir kez yazılır (kural 21).
    print(f"Adres: {app.url}")
    print("Kapatmak için Ctrl+C.")
    try:
        if goal is not None:
            app.start_run(goal, str(cfg.settings.workspace), cfg.settings.mode)
        elif resume is not None:
            app.resume_run(resume)
    except ApiError as e:
        _err(e.message)
        app.shutdown()
        return 2
    app.open()
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        print("\nKapatılıyor: aktif çalıştırma durduruluyor ve durum kaydediliyor…", file=sys.stderr)
        app.shutdown()
        return 130


def cmd_ui(args) -> int:
    try:
        cfg = _load_config(args)
        if args.port is not None and not (1024 <= args.port <= 65535):
            raise ConfigError("--port 1024 ile 65535 arasında olmalı.")
    except ConfigError as e:
        _err(str(e))
        return 2
    return _serve(cfg, args)


# ====================================================================== run

def cmd_run(args) -> int:
    try:
        goal = _read_goal(args).strip()
    except ValueError as e:
        _err(str(e))
        return 2
    if not goal:
        _err("Hedef boş olamaz.")
        return 2
    if len(goal) > MAX_GOAL_CHARS:
        _err(f"Hedef en fazla {MAX_GOAL_CHARS} karakter olabilir ({len(goal)} verildi).")
        return 2
    try:
        cfg = _load_config(args)
        if args.asamali:
            cfg.settings.mode = "asamali"
        ws = _check_workspace(cfg, Path(args.workspace) if args.workspace else cfg.settings.workspace)
    except (ConfigError, SecurityError) as e:
        _err(str(e))
        return 2
    cfg.settings.workspace = ws
    if args.ui:
        args.port, args.no_browser = None, False
        return _serve(cfg, args, goal=goal)

    try:
        transcript = Transcript(cfg.settings.runs_dir)
    except OSError as e:
        _err(f"Kayıt klasörü oluşturulamadı: {e}")
        return 2
    bus = EventBus()
    bus.add_listener(transcript.on_event)
    bus.add_listener(ConsolePrinter({n: c.display for n, c in cfg.agents.items()}))
    try:
        bus.publish("baslangic", hedef=goal, calisma_klasoru=str(ws), mock=bool(args.mock),
                    mod=cfg.settings.mode)
        print(f"Orkestra {__version__}" + (" (sahte ajanlar)" if args.mock else ""))
        print(f"Çalışma klasörü: {ws}")
        print(f"Kayıt: {transcript.dir}")
        if cfg.settings.mode == "asamali":
            print(f"Mod: aşamalı (en fazla {cfg.settings.max_phases} aşama)")
        memory = ProjectMemory.load(ws)
        if not memory.empty:
            print(f"Proje hafızası yüklendi: {memory.summary()}")
        orch = Orchestrator(cfg, _make_agents(cfg, args.mock), bus, ws, CliApprover(auto_yes=args.yes),
                            run_dir=transcript.dir, memory=memory)
        return _execute(orch, goal, bus, cfg.settings.max_calls)
    finally:
        transcript.close()


# ====================================================================== devam

def cmd_devam(args) -> int:
    run_dir = Path(args.run_klasoru).expanduser()
    try:
        cfg = _load_config(args)
    except ConfigError as e:
        _err(str(e))
        return 2
    try:
        run_dir = run_dir.resolve(strict=True)
        state, plans = load_state(run_dir, cfg)
        ws = _check_workspace(cfg, Path(state.calisma_klasoru))
    except (OSError, StateError, SecurityError) as e:
        _err(f"Çalıştırma sürdürülemiyor: {e}")
        return 2
    if state.durum == "bitti":
        print("Bu çalıştırma zaten tamamlanmış; sürdürülecek bir şey yok.")
        return 0
    cfg.settings.workspace = ws
    cfg.settings.mode = state.mod
    if args.ui:
        args.port, args.no_browser = None, False
        return _serve(cfg, args, resume=run_dir.name)

    transcript = Transcript(cfg.settings.runs_dir, existing=run_dir)
    bus = EventBus()
    bus.add_listener(transcript.on_event)
    bus.add_listener(ConsolePrinter({n: c.display for n, c in cfg.agents.items()}))
    try:
        print(f"Orkestra {__version__}" + (" (sahte ajanlar)" if args.mock else ""))
        print(f"Sürdürülen çalıştırma: {run_dir}")
        print(f"Çalışma klasörü: {ws}")
        memory = ProjectMemory.load(ws)
        # Yeni oturuma max_calls kadar taze bütçe verilir; toplam çağrı sayısı durumda ayrıca tutulur.
        orch = Orchestrator(cfg, _make_agents(cfg, args.mock), bus, ws, CliApprover(auto_yes=args.yes),
                            run_dir=run_dir, state=state, resume_plans=plans, memory=memory)
        return _execute(orch, state.hedef, bus, cfg.settings.max_calls)
    finally:
        transcript.close()


# ====================================================================== doctor

async def _doctor(cfg: Config) -> bool:
    ok = True
    for name, acfg in cfg.agents.items():
        try:
            ver = await CliAgent(acfg).version()
            print(f"  ✔ {name} ({acfg.display}): {ver}")
        except (AgentError, SecurityError, OSError) as e:
            ok = False
            print(f"  ✖ {name} ({acfg.display}): {redact(str(e))}")
    return ok


def cmd_doctor(args) -> int:
    try:
        cfg = _load_config(args)
    except ConfigError as e:
        _err(str(e))
        return 2
    print(f"Orkestra {__version__}")
    print(f"Planlayıcı: {cfg.settings.planner}")
    print(f"Denetçi: {cfg.settings.reviewer or '(denetim kapalı)'}")
    print("Ajanlar:")
    ok = asyncio.run(_doctor(cfg))
    try:
        ws = _check_workspace(cfg, Path(args.workspace) if args.workspace else cfg.settings.workspace)
        print(f"  ✔ Çalışma klasörü: {ws}")
    except SecurityError as e:
        ok = False
        print(f"  ✖ Çalışma klasörü: {e}")
    print("Her şey hazır." if ok else "Sorunlar var; yukarıdaki ✖ satırlarına bak.")
    return 0 if ok else 1


# ====================================================================== terminal ve hesaplar (sürüm 0.3)

def cmd_terminal(args) -> int:
    try:
        cfg = _load_config(args)
    except ConfigError as e:
        _err(str(e))
        return 2
    if not sys.stdin.isatty():
        _err("Terminal etkileşimli bir konsol gerektirir. Betiklerden 'run' komutunu kullan.")
        return 2
    from .terminal import main_terminal
    return main_terminal(cfg, mock=args.mock)


def cmd_hesap(args) -> int:
    """Hesap bağlama sihirbazı (terminal açmadan)."""
    try:
        cfg = _load_config(args)
    except ConfigError as e:
        _err(str(e))
        return 2
    from .terminal import TerminalApp
    app = TerminalApp(cfg)
    app.cmd_hesaplar([])
    if not sys.stdin.isatty():
        return 0
    from . import accounts
    if accounts.missing_packages(cfg):
        app.cmd_kur([])
    app.cmd_baglan(["hepsi"])
    app.cmd_hesaplar([])
    return 0


# ====================================================================== ekip yönetimi (sürüm 0.4)

def cmd_ekip(args) -> int:
    """katalog | ekle <ad> | cikar <ad> | ajanlar | ajan ac|kapat <ad> — terminal komutlarının CLI karşılığı."""
    try:
        cfg = _load_config(args)
    except ConfigError as e:
        _err(str(e))
        return 2
    from .terminal import TerminalApp
    app = TerminalApp(cfg)
    fn = {"katalog": app.cmd_katalog, "ekle": app.cmd_ekle, "cikar": app.cmd_cikar,
          "ajanlar": app.cmd_ajanlar, "ajan": app.cmd_ajan}[args.komut]
    fn(args.args)
    return 0


# ====================================================================== argparse

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m orkestra",
                                description="Claude, Gemini ve Copilot'u tek ekip gibi çalıştıran orkestratör.")
    p.add_argument("--mock", action="store_true", help="terminali sahte ajanlarla aç (abonelik harcamaz)")
    p.add_argument("--config", help="config dosyası")
    sub = p.add_subparsers(dest="komut", required=False, metavar="KOMUT")

    t = sub.add_parser("terminal", help="etkileşimli proje geliştirme terminali (varsayılan)")
    t.add_argument("--mock", action="store_true", help="sahte ajanlarla dene (abonelik harcamaz)")
    t.add_argument("--config", help="config dosyası")
    t.set_defaults(func=cmd_terminal)

    for name, help_text in (("katalog", "eklenebilir yapay zekâları listele"),
                            ("ekle", "katalogdan ekibe ajan ekle: ekle <ad>"),
                            ("cikar", "katalogdan eklenen ajanı çıkar: cikar <ad>"),
                            ("ajanlar", "ekipteki ajanları listele"),
                            ("ajan", "ajanı aç/kapat: ajan ac|kapat <ad>")):
        e = sub.add_parser(name, help=help_text)
        e.add_argument("args", nargs="*")
        e.add_argument("--config", help="config dosyası")
        e.set_defaults(func=cmd_ekip)

    h = sub.add_parser("hesap", help="Claude, Gemini ve Copilot hesaplarını bağla")
    h.add_argument("--config", help="config dosyası")
    h.set_defaults(func=cmd_hesap)

    r = sub.add_parser("run", help="bir hedefi planla ve çalıştır")
    r.add_argument("hedef", nargs="?", help="hedef metni")
    r.add_argument("--hedef-dosyasi", help="hedefi UTF-8 dosyadan oku")
    r.add_argument("--config", help="config dosyası (varsayılan: depodaki config.toml)")
    r.add_argument("--workspace", help="config'teki çalışma klasörünü ez")
    r.add_argument("--mock", action="store_true", help="gerçek CLI yerine sahte ajanlar kullan")
    r.add_argument("--yes", action="store_true", help="salt-okunur planları otomatik onayla")
    r.add_argument("--asamali", action="store_true", help="aşamalı mod (büyük uygulamalar için)")
    r.add_argument("--ui", action="store_true", help="çalıştırmayı görsel çalışma alanında başlat")
    r.set_defaults(func=cmd_run)

    dv = sub.add_parser("devam", help="yarım kalan bir çalıştırmayı kaldığı yerden sürdür")
    dv.add_argument("run_klasoru", help="runs/<YYYYMMDD-HHMMSS> klasörü")
    dv.add_argument("--config", help="config dosyası")
    dv.add_argument("--mock", action="store_true", help="gerçek CLI yerine sahte ajanlar kullan")
    dv.add_argument("--yes", action="store_true", help="salt-okunur planları otomatik onayla")
    dv.add_argument("--ui", action="store_true", help="görsel çalışma alanında sürdür")
    dv.set_defaults(func=cmd_devam)

    u = sub.add_parser("ui", help="görsel çalışma alanını başlat (yalnızca 127.0.0.1)")
    u.add_argument("--port", type=int, help="başlangıç portu (doluysa sonraki 10 port denenir)")
    u.add_argument("--no-browser", action="store_true", help="tarayıcıyı otomatik açma")
    u.add_argument("--mock", action="store_true", help="sahte ajanlarla dene (abonelik harcamaz)")
    u.add_argument("--config", help="config dosyası")
    u.set_defaults(func=cmd_ui)

    d = sub.add_parser("doctor", help="CLI araçlarını ve çalışma klasörünü kontrol et")
    d.add_argument("--config", help="config dosyası")
    d.add_argument("--workspace", help="denetlenecek çalışma klasörü")
    d.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    _setup_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        if not sys.stdin.isatty():
            parser.print_help()
            return 0
        args.func = cmd_terminal          # argümansız: etkileşimli terminal
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nKullanıcı tarafından durduruldu.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
