"""CLI giriş noktası: python -m orkestra run | doctor (bölüm 6.8).

Çıkış kodları: 0 başarı · 1 çalışma hatası/iptal · 2 config, güvenlik veya girdi hatası · 130 Ctrl+C.
"""

from __future__ import annotations

import argparse
import json
import asyncio
import sys
from pathlib import Path

from . import __version__
from .agents import AgentError, CliAgent, MockAgent
from .config import ConfigError, default_config_path, load_config
from .engine import BudgetExceeded, Orchestrator
from .planner import Plan, PlanError
from .security import SecurityError, redact, validate_workspace
from .transcript import Transcript

MAX_GOAL_CHARS = 20000
YES_ANSWERS = {"e", "evet", "y", "yes"}


def _setup_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def _err(msg: str) -> None:
    print(f"Hata: {redact(msg)}", file=sys.stderr)


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


def _load(args):
    settings, cfgs = load_config(Path(args.config) if args.config else default_config_path())
    if args.workspace:
        settings.workspace = Path(args.workspace)
    settings.workspace = validate_workspace(settings.workspace)
    return settings, cfgs


def _print_plan(plan: Plan, cfgs) -> None:
    print("\n=== PLAN ===")
    print(plan.summary)
    for t in plan.tasks:
        agent = cfgs[t.assignee].display if t.assignee in cfgs else t.assignee
        deps = ", ".join(t.depends_on) or "-"
        write = "EVET ✎" if t.needs_write else "hayır"
        print(f"\n[{t.id}] {t.title}")
        print(f"  Ajan: {agent} · Bağımlılıklar: {deps} · Yazma: {write}")
        instr = t.instructions[:300] + ("…" if len(t.instructions) > 300 else "")
        print(f"  Talimat: {instr}")
    print()


def _approve(plan: Plan, auto_yes: bool) -> tuple[bool, str]:
    """Bölüm 6.8 onay kuralları. (onaylandı_mı, iptal_nedeni) döndürür."""
    if auto_yes and not plan.has_write:
        print("Plan salt-okunur; --yes ile otomatik onaylandı.")
        return True, ""
    if plan.has_write:
        print("⚠ Bu plan çalışma klasöründe dosya YAZAN görevler içeriyor; onayın gerekiyor.")
    if not sys.stdin.isatty():
        return False, "Etkileşimsiz ortamda plan onayı alınamadı."
    try:
        answer = input("Planı onaylıyor musun? [e/H]: ").strip().lower()
    except EOFError:
        return False, "Onay alınamadı."
    if answer in YES_ANSWERS:
        return True, ""
    return False, "Kullanıcı planı onaylamadı."


def _task_md(task, res) -> str:
    lines = [f"# [{task.id}] {task.title}", "", f"- Ajan: {task.assignee}",
             f"- Durum: {res.status if res else 'çalışmadı'}", ""]
    if res and res.status == "tamam":
        lines.append(res.output)
    else:
        lines.append(f"Hata: {res.error if res else '-'}")
    return "\n".join(lines) + "\n"


# ====================================================================== run

async def _run_flow(goal: str, args, settings, cfgs, transcript: Transcript) -> int:
    names = list(cfgs)
    if args.mock:
        agents = {n: MockAgent(c, names) for n, c in cfgs.items()}
    else:
        agents = {n: CliAgent(c) for n, c in cfgs.items()}
    orch = Orchestrator(settings, agents, transcript, settings.workspace, log=print)

    plan = await orch.make_plan(goal)
    transcript.save("plan.json", json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    _print_plan(plan, cfgs)

    ok, reason = _approve(plan, args.yes)
    if not ok:
        transcript.event("iptal", neden=reason)
        print(f"İptal edildi: {reason}")
        return 1

    results = await orch.execute(plan, goal)
    for t in plan.tasks:
        transcript.save(f"gorev-{t.id}.md", _task_md(t, results.get(t.id)))
    report = await orch.synthesize(goal, plan, results)
    path = transcript.save("sonuc.md", report)
    print("\n=== SONUÇ RAPORU ===\n")
    print(redact(report))
    print(f"\nToplam model çağrısı: {orch.calls}/{settings.max_calls}")
    print(f"Rapor: {path}")
    return 0


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
        settings, cfgs = _load(args)
    except (ConfigError, SecurityError) as e:
        _err(str(e))
        return 2

    try:
        transcript = Transcript(settings.runs_dir)
    except OSError as e:
        _err(f"Kayıt klasörü oluşturulamadı: {e}")
        return 2
    try:
        transcript.event("baslangic", hedef=goal, calisma_klasoru=str(settings.workspace), mock=bool(args.mock))
        print(f"Orkestra {__version__}" + (" (sahte ajanlar)" if args.mock else ""))
        print(f"Çalışma klasörü: {settings.workspace}")
        print(f"Kayıt: {transcript.dir}")
        try:
            return asyncio.run(_run_flow(goal, args, settings, cfgs, transcript))
        except (PlanError, AgentError, SecurityError, BudgetExceeded) as e:
            msg = redact(str(e))
            transcript.event("hata", mesaj=msg)
            _err(msg)
            return 1
        except KeyboardInterrupt:
            transcript.event("iptal", neden="Kullanıcı Ctrl+C ile durdurdu.")
            print("\nKullanıcı tarafından durduruldu.", file=sys.stderr)
            return 130
    finally:
        transcript.close()


# ====================================================================== doctor

async def _doctor(settings, cfgs) -> bool:
    ok = True
    for name, cfg in cfgs.items():
        try:
            ver = await CliAgent(cfg).version()
            print(f"  ✔ {name} ({cfg.display}): {ver}")
        except (AgentError, SecurityError, OSError) as e:
            ok = False
            print(f"  ✖ {name} ({cfg.display}): {redact(str(e))}")
    return ok


def cmd_doctor(args) -> int:
    try:
        settings, cfgs = load_config(Path(args.config) if args.config else default_config_path())
    except ConfigError as e:
        _err(str(e))
        return 2
    print(f"Orkestra {__version__}")
    print(f"Planlayıcı: {settings.planner}")
    print(f"Denetçi: {settings.reviewer or '(denetim kapalı)'}")
    print("Ajanlar:")
    ok = asyncio.run(_doctor(settings, cfgs))
    ws = Path(args.workspace) if args.workspace else settings.workspace
    try:
        ws = validate_workspace(ws)
        print(f"  ✔ Çalışma klasörü: {ws}")
    except SecurityError as e:
        ok = False
        print(f"  ✖ Çalışma klasörü: {e}")
    print("Her şey hazır." if ok else "Sorunlar var; yukarıdaki ✖ satırlarına bak.")
    return 0 if ok else 1


# ====================================================================== argparse

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m orkestra",
                                description="Claude, Gemini ve Copilot'u tek ekip gibi çalıştıran orkestratör.")
    sub = p.add_subparsers(dest="komut", required=True, metavar="KOMUT")

    r = sub.add_parser("run", help="bir hedefi planla ve çalıştır")
    r.add_argument("hedef", nargs="?", help="hedef metni")
    r.add_argument("--hedef-dosyasi", help="hedefi UTF-8 dosyadan oku")
    r.add_argument("--config", help="config dosyası (varsayılan: depodaki config.toml)")
    r.add_argument("--workspace", help="config'teki çalışma klasörünü ez")
    r.add_argument("--mock", action="store_true", help="gerçek CLI yerine sahte ajanlar kullan")
    r.add_argument("--yes", action="store_true", help="salt-okunur planları otomatik onayla")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("doctor", help="CLI araçlarını ve çalışma klasörünü kontrol et")
    d.add_argument("--config", help="config dosyası")
    d.add_argument("--workspace", help="denetlenecek çalışma klasörü")
    d.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    _setup_stdio()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nKullanıcı tarafından durduruldu.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
