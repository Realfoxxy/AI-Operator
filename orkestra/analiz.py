"""Çalıştırma analizi: kayit.jsonl'den maliyet, süre ve sorun özeti (sürüm 0.4).

İstem ve yanıt METİNLERİNİ göstermez (yalnızca uzunluklar, sayılar, durumlar); kullanıcı çıktıyı
güvenle paylaşabilir. Eski kayıtlarda (önbellek ayrımı yokken) token sayıları önbelleği de içerir.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def _t(z: str) -> float:
    try:
        return datetime.fromisoformat(z).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def analyze_run(run_dir: Path) -> list[str]:
    path = Path(run_dir) / "kayit.jsonl"
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    calls: dict[int, dict] = {}
    last_no_by_agent: dict[str, int] = {}
    old_format = False
    mode, plans, verifs, results, changes, handovers, autorev, reviews, consults = "tek", [], [], {}, defaultdict(int), [], 0, 0, 0
    cancelled = ""
    write_tasks: dict[str, list[str]] = {}
    for ev in events:
        tur = ev.get("tur")
        if tur == "baslangic":
            mode = ev.get("mod") or mode
        elif tur == "cagri":
            calls[ev["no"]] = {"ajan": ev["ajan"], "rol": ev["rol"], "gorev": ev["gorev"], "yazma": ev.get("yazma"),
                               "bas": _t(ev["zaman"]), "bit": None, "istem": len(ev.get("istem", "")), "usage": {}}
            last_no_by_agent[ev["ajan"]] = ev["no"]
        elif tur == "yanit" and ev.get("no") in calls:
            calls[ev["no"]]["bit"] = _t(ev["zaman"])
            calls[ev["no"]]["devam_oturumu"] = ev.get("devam_oturumu")
        elif tur == "kullanim":
            no = ev.get("no") or last_no_by_agent.get(ev.get("ajan"))
            if "onbellek" not in (ev.get("son") or {}) and ev.get("son"):
                old_format = True
            if no in calls:
                calls[no]["usage"] = ev.get("son") or {}
        elif tur == "plan":
            plans.append(ev.get("plan") or {})
            for t in (ev.get("plan") or {}).get("tasks", []):
                if t.get("needs_write"):
                    write_tasks[t["id"]] = t.get("files") or []
        elif tur == "dogrulama":
            verifs.append(ev)
        elif tur == "gorev_sonucu":
            results[ev["gorev"]] = ev
        elif tur == "dosya_degisikligi":
            n = len(ev.get("eklenen", [])) + len(ev.get("degisen", [])) + len(ev.get("silinen", []))
            changes[ev.get("gorev", "-")] += n
        elif tur == "devretme":
            handovers.append(ev)
        elif tur == "denetim":
            if ev.get("otomatik"):
                autorev += 1
            else:
                reviews += 1
        elif tur == "danisma":
            consults += 1
        elif tur == "iptal":
            cancelled = str(ev.get("neden", ""))
        elif tur == "asama":
            mode = "asamali"

    tok = lambda u: (u.get("girdi") or 0) + (u.get("cikti") or 0)  # noqa: E731
    total_tok = sum(tok(c["usage"]) for c in calls.values())
    total_cache = sum(c["usage"].get("onbellek") or 0 for c in calls.values())
    starts = [c["bas"] for c in calls.values() if c["bas"]]
    ends = [c["bit"] for c in calls.values() if c["bit"]]
    span = (max(ends) - min(starts)) if starts and ends else 0
    n_tasks = sum(len(p.get("tasks", [])) for p in plans)

    out = [f"Çalıştırma: {Path(run_dir).name}",
           f"  Mod: {'aşamalı' if mode == 'asamali' else 'tek plan'} · plan sayısı {len(plans)} · görev {n_tasks} · "
           f"model çağrısı {len(calls)} · süre ≈ {int(span // 60)} dk {int(span % 60)} sn",
           f"  Token: {_fmt(total_tok)} (girdi+çıktı)" + (f" + {_fmt(total_cache)} önbellekten okunan" if total_cache else "")
           + ("  [eski kayıt: önbellek ayrılmamış, sayı önbelleği de içerir]" if old_format else ""),
           "", "  Rol bazında:"]
    by_role: dict[str, list] = defaultdict(lambda: [0, 0, 0, 0.0])
    for c in calls.values():
        r = by_role[c["rol"]]
        r[0] += 1
        r[1] += tok(c["usage"])
        r[2] += c["usage"].get("onbellek") or 0
        r[3] += (c["bit"] - c["bas"]) if c["bit"] and c["bas"] else 0
    for rol, (n, t, cache, sec) in sorted(by_role.items(), key=lambda x: -x[1][1]):
        out.append(f"    {rol:<11} {n:>3} çağrı · {_fmt(t):>9} token" + (f" (+{_fmt(cache)} önbellek)" if cache else "")
                   + f" · {int(sec)} sn")
    top = sorted(calls.items(), key=lambda kv: -(tok(kv[1]["usage"]) + (kv[1]["usage"].get("onbellek") or 0)))[:5]
    if top:
        out += ["", "  En pahalı çağrılar:"]
        for no, c in top:
            sec = int(c["bit"] - c["bas"]) if c["bit"] and c["bas"] else 0
            out.append(f"    [{no}] {c['ajan']} {c['rol']} {c['gorev']}: {_fmt(tok(c['usage']))} token"
                       + (f" (+{_fmt(c['usage'].get('onbellek') or 0)} önbellek)" if c["usage"].get("onbellek") else "")
                       + f", {sec} sn, istem {_fmt(c['istem'])} karakter" + (" · oturum sürdürüldü" if c.get("devam_oturumu") else ""))
    if verifs:
        out += ["", "  Doğrulamalar:"]
        for v in verifs:
            tail = [ln for ln in (v.get("cikti") or "").strip().splitlines() if ln.strip()][-2:]
            out.append(f"    {v['gorev']} {v['komut']}: {'✓' if v.get('basarili') else '✗'} çıkış {v.get('cikis_kodu')}"
                       + (f" — {' | '.join(tail)[:160]}" if tail and not v.get("basarili") else ""))
    if write_tasks:
        out += ["", "  Dosya yazması beklenen görevler:"]
        for tid, files in write_tasks.items():
            n = changes.get(tid, 0)
            if tid not in results:
                state = "– çalışmadı"
            else:
                state = f"✓ {n} dosya değişti" if n else "✗ HİÇ DOSYA DEĞİŞMEDİ"
            out.append(f"    {tid}: {state} (beklenen: {', '.join(files)})")

    # --------------------------------------------------------- bulgular ve öneriler
    tips = []
    if mode == "asamali" and n_tasks <= 4:
        tips.append("Küçük bir iş aşamalı modda çalışmış: her aşama ayrı planlama ve denetim demek. "
                    "Basit işler için /mod tek veya /hizli ac.")
    if n_tasks > 2 and mode == "tek" and len(plans) == 1 and n_tasks >= 3 and total_tok and len(calls) > 8:
        tips.append(f"Plan {n_tasks} göreve bölünmüş; her görev ayrı çalışma + denetim çağrısıdır. Basit işlerde /hizli ac.")
    if consults:
        tips.append(f"{consults} danışma yapılmış (her biri ek çağrı + işçinin tekrar çalışması). Kapatmak için /hizli ac.")
    if autorev:
        tips.append(f"{autorev} otomatik revizyon (başarısız doğrulama veya dosya yazılmaması) ek çağrıya yol açmış.")
    if any(v.get("cikis_kodu") is None for v in verifs) or any("bulunamadı" in (v.get("cikti") or "") for v in verifs):
        tips.append("Bir doğrulama komutu hiç çalışamamış (program bulunamadı/zaman aşımı): bu kodla değil ortamla ilgili. "
                    "Orkestra'nın son sürümüne güncelle (git pull); 'python' komutları artık doğru yorumlayıcıyla çalışır.")
    if any("NO TESTS RAN" in (v.get("cikti") or "") or "Ran 0 tests" in (v.get("cikti") or "") for v in verifs):
        tips.append("Doğrulama hiç test bulamamış: testler unittest biçiminde değil (ör. pytest tarzı). "
                    "Son sürümde ajanlara bu açıkça söyleniyor.")
    if cancelled:
        tips.append(f"Çalıştırma iptal edildi: {cancelled[:120]}")
    if any(changes.get(t, 0) == 0 and results.get(t, {}).get("durum") == "tamam" for t in write_tasks):
        tips.append("Dosya yazması beklenen bir görev hiç dosya değiştirmemiş: kod yalnızca raporda kalmış olabilir. "
                    "Son sürüm bunu yakalayıp ajandan dosyaya yazmasını ister.")
    if any(c.get("devam_oturumu") for c in calls.values()):
        tips.append("Oturum sürdürme (--resume) açık: her turda önceki konuşma yeniden yüklenir ve token artar.")
    if total_cache and total_cache > 3 * max(total_tok, 1):
        tips.append("Önbellekten okunan tokenlar toplamın büyük kısmı: Claude Code her turda bağlamını önbellekten "
                    "yeniden okur. Bunlar normal girdiden çok daha ucuzdur; yine de çağrı sayısını azaltmak hepsini düşürür.")
    if handovers:
        tips.append(f"{len(handovers)} görev başka ajana devredilmiş (kota veya hata): {', '.join(h['eski'] + '→' + h['yeni'] for h in handovers[:5])}.")
    bad = [r for r in results.values() if r.get("durum") != "tamam"]
    if bad:
        tips.append("Tamamlanamayan görevler: " + "; ".join(f"{r['gorev']} {r['durum']}: {str(r.get('hata', ''))[:100]}" for r in bad[:5]))
    out += ["", "  Bulgular ve öneriler:"] + ([f"    • {t}" for t in tips] or ["    • Belirgin bir sorun görülmedi."])
    return out


def latest_run(runs_dir: Path) -> Path | None:
    import re
    root = Path(runs_dir)
    if not root.is_dir():
        return None
    runs = sorted((p for p in root.iterdir() if p.is_dir() and re.match(r"^\d{8}-\d{6}", p.name)
                   and (p / "kayit.jsonl").is_file()), key=lambda p: p.name)
    return runs[-1] if runs else None
