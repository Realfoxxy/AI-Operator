"""Olay veri yolu (bölüm 10.2) ve CLI konsol yazıcısı.

Motor yalnızca EventBus.publish çağırır. Transcript (kayit.jsonl), konsol çıktısı ve web
arayüzü (SSE) bu yolun aboneleridir.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from datetime import datetime
from typing import Callable

from .security import clean_terminal, redact

BUFFER_SIZE = 5000
QUEUE_SIZE = 1000

Listener = Callable[[dict, bool], None]   # (olay, persist)


def redact_value(v):
    """Metin değerlerine (iç içe yapılar dahil) maskeleme uygular."""
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, dict):
        return {k: redact_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [redact_value(x) for x in v]
    return v


class Subscription:
    """Tek bir SSE bağlantısının kuyruğu. Kuyruğu dolan yavaş abone düşürülür."""

    def __init__(self, bus: "EventBus"):
        self.bus = bus
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self.closed = False

    def get(self, timeout: float | None = None) -> dict | None:
        """Sıradaki olayı döndürür; zaman aşımında None."""
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self.bus.unsubscribe(self)


class EventBus:
    def __init__(self, buffer_size: int = BUFFER_SIZE):
        self._lock = threading.Lock()
        self._buffer: deque[dict] = deque(maxlen=buffer_size)
        self._subs: list[Subscription] = []
        self._listeners: list[Listener] = []
        self._next_id = 0

    @property
    def last_id(self) -> int:
        return self._next_id

    def add_listener(self, fn: Listener) -> None:
        """Eşzamanlı abone (Transcript, konsol). Yayın sırasında, sırayla çağrılır."""
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def publish(self, tur: str, *, persist: bool = True, **veri) -> dict:
        with self._lock:
            self._next_id += 1
            event = {"id": self._next_id, "zaman": datetime.now().isoformat(timespec="seconds"), "tur": tur}
            event.update({k: redact_value(v) for k, v in veri.items()})
            self._buffer.append(event)
            for sub in list(self._subs):
                try:
                    sub.queue.put_nowait(event)
                except queue.Full:
                    # Yavaş abone düşürülür; tarayıcı Last-Event-ID ile yeniden bağlanır.
                    sub.closed = True
                    self._subs.remove(sub)
            listeners = list(self._listeners)
            # Abonelere kilit içinde dağıtılır: kayıt ve konsol sırası olay sırasıyla aynı kalır.
            for fn in listeners:
                try:
                    fn(event, persist)
                except Exception:  # noqa: BLE001 - bir abonenin hatası yayını durdurmamalı
                    pass
        return event

    def subscribe(self, son_id: int | None = None) -> Subscription:
        sub = Subscription(self)
        with self._lock:
            if son_id is not None:
                for ev in self._buffer:
                    if ev["id"] > son_id:
                        try:
                            sub.queue.put_nowait(ev)
                        except queue.Full:
                            break
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            sub.closed = True
            if sub in self._subs:
                self._subs.remove(sub)

    def history(self, son_id: int = 0) -> list[dict]:
        with self._lock:
            return [ev for ev in self._buffer if ev["id"] > son_id]


# ====================================================================== konsol yazıcısı

class ConsolePrinter:
    """CLI modunda olayları okunur satırlara çevirir (sürüm 0.1 çıktı biçimi korunur)."""

    def __init__(self, agent_displays: dict[str, str], out: Callable[[str], None] = print):
        self.displays = agent_displays
        self._raw_out = out
        # Model kaynaklı metinler terminale basılmadan önce kontrol karakterlerinden arındırılır.
        self.out = lambda text: self._raw_out(clean_terminal(text))

    def __call__(self, ev: dict, persist: bool) -> None:
        fn = getattr(self, "_" + ev["tur"], None)
        if fn:
            fn(ev)

    def _cagri(self, ev):
        self.out(f"→ [{ev['no']}] {ev['ajan']} {ev['rol']} {ev['gorev']}" + (" (✎ yazma)" if ev.get("yazma") else ""))

    def _yanit(self, ev):
        self.out(f"← [{ev['no']}] {ev['ajan']} {len(ev.get('metin', ''))} karakter")

    def _plan(self, ev):
        plan = ev["plan"]
        title = f" — Aşama {ev['asama']}: {plan.get('phase_title') or ''}" if ev.get("asama") else ""
        self.out(f"\n=== PLAN{title} ===")
        self.out(plan.get("summary", ""))
        for t in plan.get("tasks", []):
            agent = self.displays.get(t["assignee"], t["assignee"])
            deps = ", ".join(t.get("depends_on") or []) or "-"
            write = "EVET ✎" if t.get("needs_write") else "hayır"
            self.out(f"\n[{t['id']}] {t['title']}")
            self.out(f"  Ajan: {agent} · Bağımlılıklar: {deps} · Yazma: {write}")
            if t.get("files"):
                self.out(f"  Dosyalar: {', '.join(t['files'])}")
            if t.get("verify"):
                self.out(f"  Doğrulama: {', '.join(t['verify'])}")
            instr = t["instructions"]
            self.out(f"  Talimat: {instr[:300]}{'…' if len(instr) > 300 else ''}")
        self.out("")

    def _danisma_basladi(self, ev):
        self.out(f"💬 {ev['gorev']}: {ev['soran']} → {ev['hedef']} danışıyor")

    def _denetim(self, ev):
        if ev.get("otomatik"):
            self.out(f"🔍 {ev['gorev']} doğrulama başarısız: REVİZYON ↺ (otomatik)")
        else:
            self.out(f"🔍 {ev['gorev']} denetim: {'ONAY ✓' if ev['karar'] == 'approve' else 'REVİZYON ↺'}")

    def _gorev_sonucu(self, ev):
        icon = {"tamam": "✔", "hata": "✖", "atlandı": "⤼"}.get(ev["durum"], "•")
        self.out(f"{icon} {ev['gorev']} {ev['durum']}" + (f": {ev['hata']}" if ev.get("hata") else ""))

    def _dogrulama(self, ev):
        mark = "✓" if ev["basarili"] else "✗"
        self.out(f"🧪 {ev['gorev']} {ev['komut']}: {mark} (çıkış kodu {ev['cikis_kodu']}, {ev['sure_sn']} sn)")

    def _dosya_degisikligi(self, ev):
        self.out(f"📝 {ev['gorev']} ({ev['ajan']}): +{len(ev['eklenen'])} ~{len(ev['degisen'])} -{len(ev['silinen'])}")
        if ev.get("bildirilmemis"):
            self.out(f"⚠ Bildirilmemiş değişiklik: {', '.join(ev['bildirilmemis'][:10])}")

    def _korumali_ihlal(self, ev):
        self.out(f"⛔ İHLAL ({ev['ajan']}, {ev['gorev']}): {ev['neden']}")
        for y in ev.get("yollar", [])[:20]:
            self.out(f"   - {y}")

    def _asama(self, ev):
        durum = {"planlaniyor": "planlanıyor", "calisiyor": "çalışıyor", "bitti": "bitti"}.get(ev["durum"], ev["durum"])
        baslik = f": {ev['baslik']}" if ev.get("baslik") else ""
        self.out(f"\n=== AŞAMA {ev['no']}{baslik} ({durum}) ===")

    def _kullanici_notu(self, ev):
        self.out(f"📌 Kullanıcı notu ({ev.get('hedef') or 'tüm ekip'}): {ev['metin']}")

    def _devam(self, ev):
        self.out(f"↻ Devam ediliyor: önceki çağrı sayısı {ev['onceki_calls']}, kalan görev {ev['kalan_gorevler']}")

    def _hata(self, ev):
        if ev.get("uyari"):        # ölümcül hatalar __main__ tarafından ayrıca yazdırılır
            self.out(f"⚠ {ev['mesaj']}")

    def _durum(self, ev):
        text = {"duraklatildi": "⏸ Duraklatıldı.", "devam_ediyor": "▶ Devam ediyor.",
                "durduruldu": "⏹ Durduruldu."}.get(ev["durum"])
        if text:
            self.out(text)
