"""Güvenlik yardımcıları: maskeleme, argüman denetimi ve klasör güvenliği (bölüm 6.1, 8)."""

from __future__ import annotations

import os
import re
from pathlib import Path


class SecurityError(Exception):
    """Güvenlik kuralı ihlali."""


MASK = "[GİZLENDİ]"

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),            # Anthropic
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),               # Google
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),          # GitHub
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),          # GitHub (ince taneli)
    re.compile(r"\bsk-[A-Za-z0-9]{32,}"),                 # genel
]

_KEY_VALUE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|parola|sifre|şifre)\b"
    r"(\s*[:=]\s*)(\S+)"
)

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_CMD_SPECIAL = re.compile(r'["%^&|<>!\r\n]')


def redact(text: str) -> str:
    """Gizli bilgi desenlerini maskeler."""
    if not text:
        return text
    for pat in _SECRET_PATTERNS:
        text = pat.sub(MASK, text)
    return _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)


def is_safe_id(value: str) -> bool:
    """Görev ve ajan kimlikleri için güvenli kimlik kontrolü."""
    return isinstance(value, str) and bool(_SAFE_ID.match(value))


def check_argv(argv: list[str]) -> None:
    """Alt süreç argümanlarını denetler; güvensizse SecurityError fırlatır."""
    for arg in argv:
        if "\x00" in arg:
            raise SecurityError("Argümanda NUL karakteri bulundu; çağrı reddedildi.")
    if os.name == "nt" and argv and argv[0].lower().endswith((".cmd", ".bat")):
        for arg in argv[1:]:
            if _CMD_SPECIAL.search(arg):
                raise SecurityError(
                    f"Windows .cmd/.bat programına giden argümanda izin verilmeyen karakter var "
                    f"(\" % ^ & | < > ! veya satır sonu): {arg[:80]!r}. "
                    f"config.toml içindeki sabit argümanları düzelt."
                )


def validate_workspace(path: Path) -> Path:
    """Çalışma klasörünü doğrular, oluşturur ve çözülmüş yolunu döndürür."""
    p = Path(path).expanduser().resolve()
    home = Path.home().resolve()
    if p == Path(p.anchor):
        raise SecurityError(f"Disk kökü çalışma klasörü olamaz: {p}")
    if p == home or p in home.parents:
        raise SecurityError(f"Ev dizini veya üst klasörleri çalışma klasörü olamaz: {p}")
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SecurityError(f"Çalışma klasörü oluşturulamadı: {p} ({e})") from e
    if not p.is_dir():
        raise SecurityError(f"Çalışma klasörü bir dizin değil: {p}")
    return p


def make_private_dir(path: Path) -> Path:
    """Klasörü oluşturur; Windows dışında yalnızca sahibine erişim izni verir."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(p, 0o700)
    return p
