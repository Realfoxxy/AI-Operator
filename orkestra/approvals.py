"""Onay soyutlaması (bölüm 10.2): plan, başlangıç uyarısı ve ihlal kararları.

kind:  "plan" | "baslangic_uyarisi" | "ihlal"
dönüş: "onay" | "ret"   (ihlal için: "devam" | "durdur")

WebApprover Aşama C'de (server.py ile birlikte) eklenecek.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Callable

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
    async def request(self, kind: str, payload: dict, istek_id: str = "") -> str:
        raise NotImplementedError


class CliApprover(Approver):
    def __init__(self, auto_yes: bool = False, out: Callable[[str], None] = print,
                 isatty: Callable[[], bool] | None = None):
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

    async def request(self, kind: str, payload: dict, istek_id: str = "") -> str:
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
            answer = await self._ask("Planı onaylıyor musun? [e/H]: ")
            if answer is None:
                self.out("Etkileşimsiz ortamda plan onayı alınamadı.")
                return "ret"
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
    """Testler için: her isteğe sabit karar verir ve istekleri kaydeder."""

    def __init__(self, plan: str = "onay", startup: str = "onay", violation: str = "devam"):
        self.answers = {"plan": plan, "baslangic_uyarisi": startup, "ihlal": violation}
        self.requests: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict, istek_id: str = "") -> str:
        self.requests.append((kind, payload))
        return self.answers.get(kind, "ret")
