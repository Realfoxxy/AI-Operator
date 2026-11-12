"""Onay soyutlaması (bölüm 10.2): plan, başlangıç uyarısı ve ihlal kararları.

kind:  "plan" | "baslangic_uyarisi" | "ihlal"
dönüş: "onay" | "ret"   (ihlal için: "devam" | "durdur")

WebApprover Aşama C'de (server.py ile birlikte) eklenecek.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Callable

from .planner import PlanError
from .security import SecurityError, check_argv

YES_ANSWERS = {"e", "evet", "y", "yes"}
VERIFY_WARNING = ("Bu komutlar ajanların yazdığı kodu bilgisayarında çalıştırır. Güvenmediğin işlerde "
                  "Orkestra'yı sanal makine, WSL veya Windows Sandbox içinde çalıştır.")


def plan_needs_manual_approval(payload: dict) -> bool:
    """Bölüm 6.8: yazma içeren plan --yes ile bile otomatik onaylanmaz.

    Ek karar (en güvenli seçenek): doğrulama komutu çalıştıracak planlar da kod
    çalıştırdığı için otomatik onaylanmaz.
    """
    return bool(payload.get("has_write") or payload.get("verify_commands"))


class Approver:
    """validate: plan onayında kullanıcının düzenlediği planı (dict) doğrulayıp Plan döndüren fonksiyon;
    geçersizse PlanError fırlatır. Düzenlenmiş ve doğrulanmış plan pop_edit ile alınır."""

    def __init__(self):
        self._edits: dict = {}

    async def request(self, kind: str, payload: dict, istek_id: str = "", validate=None) -> str:
        raise NotImplementedError

    def pop_edit(self, istek_id: str):
        return getattr(self, "_edits", {}).pop(istek_id, None)


class CliApprover(Approver):
    def __init__(self, auto_yes: bool = False, out: Callable[[str], None] = print,
                 isatty: Callable[[], bool] | None = None):
        super().__init__()
        self.auto_yes = auto_yes
        self.out = out
        self.isatty = isatty or (lambda: sys.stdin.isatty())

    async def _ask(self, question: str) -> str | None:
        if not self.isatty():
            return None
        loop = asyncio.get_running_loop()
        try:
            return (await loop.run_in_executor(None, input, question)).strip().lower()
        except EOFError:
            return None

    async def request(self, kind: str, payload: dict, istek_id: str = "", validate=None) -> str:
        if kind == "plan":
            manual = plan_needs_manual_approval(payload)
            if self.auto_yes and not manual:
                self.out("Plan salt-okunur; --yes ile otomatik onaylandı.")
                return "onay"
            if payload.get("has_write"):
                self.out("⚠ Bu plan çalışma klasöründe dosya YAZAN görevler içeriyor; onayın gerekiyor.")
            cmds = payload.get("verify_commands") or []
            if cmds:
                self.out("Bu plan şu doğrulama komutlarını çalıştıracak:")
                for c in cmds:
                    self.out(f"  - {c['name']}: {' '.join(c['argv'])}")
                self.out(f"⚠ {VERIFY_WARNING}")
            while True:
                answer = await self._ask("Planı onaylıyor musun? [e = evet / d = düzenle / H = hayır]: ")
                if answer is None:
                    self.out("Etkileşimsiz ortamda plan onayı alınamadı.")
                    return "ret"
                if answer in ("d", "düzenle", "duzenle") and validate is not None:
                    loop = asyncio.get_running_loop()
                    edited = await loop.run_in_executor(None, edit_plan_interactive, payload.get("plan", {}),
                                                        validate, self.out)
                    if edited is not None:
                        self._edits[istek_id] = edited
                        self.out("Düzenlenen plan doğrulandı ve kabul edildi.")
                        return "onay"
                    continue
                return "onay" if answer in YES_ANSWERS else "ret"

        if kind == "baslangic_uyarisi":
            self.out("⚠ Ajanları yönlendirebilecek talimat/konfig dosyaları bulundu:")
            for f in payload.get("dosyalar", []):
                self.out(f"  - {f}")
            self.out("Bu dosyalar CLI araçları tarafından otomatik okunur. İçeriklerini kontrol etmeden devam etme.")
            answer = await self._ask("Yine de devam edilsin mi? [e/H]: ")
            if answer is None:
                self.out("Etkileşimsiz ortamda onay alınamadı.")
                return "ret"
            return "onay" if answer in YES_ANSWERS else "ret"

        if kind == "ihlal":
            self.out("⛔ Korumalı yol veya izinsiz değişiklik algılandı. Orkestra değişiklikleri geri ALMAZ; "
                     "gerekirse git ile geri al.")
            answer = await self._ask("Devam edilsin mi? [e = devam et / H = durdur]: ")
            if answer is None:
                return "durdur"
            return "devam" if answer in YES_ANSWERS else "durdur"

        return "ret"


class StaticApprover(Approver):
    """Testler için: her isteğe sabit karar verir ve istekleri kaydeder. edit: plan onayında
    uygulanacak düzenleme fonksiyonu (plan dict → yeni plan dict)."""

    def __init__(self, plan: str = "onay", startup: str = "onay", violation: str = "devam", edit=None):
        super().__init__()
        self.answers = {"plan": plan, "baslangic_uyarisi": startup, "ihlal": violation}
        self.requests: list[tuple[str, dict]] = []
        self.edit = edit

    async def request(self, kind: str, payload: dict, istek_id: str = "", validate=None) -> str:
        self.requests.append((kind, payload))
        if kind == "plan" and self.edit is not None and validate is not None:
            self._edits[istek_id] = validate(self.edit(payload["plan"]))
        return self.answers.get(kind, "ret")


def edit_plan_interactive(plan: dict, validate, out: Callable[[str], None] = print):
    """Planı geçici bir JSON dosyasında kullanıcının editöründe açar; doğrulanmış Plan veya None döndürür.

    Editör $VISUAL/$EDITOR'dan (yoksa Windows'ta notepad, diğerlerinde nano/vi) alınır; kabuk olmadan
    liste argümanlarla çalıştırılır.
    """
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        argv = shlex.split(editor, posix=os.name != "nt")
    elif os.name == "nt":
        argv = ["notepad"]
    else:
        argv = [next((e for e in ("nano", "vim", "vi") if shutil.which(e)), "vi")]
    exe = shutil.which(argv[0]) if argv else None
    if not exe:
        out(f"Editör bulunamadı ({argv[0] if argv else '-'}). EDITOR ortam değişkenini ayarla.")
        return None
    fd, path = tempfile.mkstemp(prefix="orkestra-plan-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
        while True:
            argv_full = [exe, *argv[1:], path]
            try:
                check_argv(argv_full)
                subprocess.run(argv_full, check=False)
            except (OSError, SecurityError) as e:
                out(f"Editör açılamadı: {e}")
                return None
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                return validate(data)
            except (ValueError, PlanError) as e:
                out(f"Plan geçersiz: {e}")
                again = input("Yeniden düzenlemek ister misin? [e/H]: ").strip().lower() if sys.stdin.isatty() else ""
                if again not in YES_ANSWERS:
                    return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
