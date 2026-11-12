"""config.toml'u güvenle düzenleme: katalogdan ajan ekleme/çıkarma, ajan açma/kapatma (sürüm 0.4).

Güvenlik ve sağlamlık:
- Her değişiklik önce geçici bir kopyada load_config ile doğrulanır; geçersizse asıl dosyaya dokunulmaz.
- Değiştirmeden önce config.toml.yedek alınır; yazma atomiktir (os.replace).
- Katalog blokları işaretlerle eklenir; /cikar yalnızca işaretli bloğu siler.
- Dosyanın satır sonu (CRLF/LF) korunur; BOM varsa okunurken atılır.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from pathlib import Path

from .config import ConfigError, load_config
from .security import is_safe_id

MARK_START = "# >>> orkestra-katalog: {ad}"
MARK_END = "# <<< orkestra-katalog: {ad}"


class ConfigEditError(Exception):
    """Config düzenlenemedi."""


def _read(path: Path) -> tuple[str, str]:
    text = Path(path).read_bytes().removeprefix(b"\xef\xbb\xbf").decode("utf-8")
    return text, ("\r\n" if "\r\n" in text else "\n")


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)       # JSON kaçışları TOML temel dizgeleriyle uyumludur
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise ConfigEditError(f"Desteklenmeyen değer türü: {type(v).__name__}")


def _write_validated(path: Path, text: str, nl: str) -> None:
    path = Path(path)
    data = text.replace("\r\n", "\n").replace("\n", nl).encode("utf-8")
    trial = path.with_name(path.name + ".deneme")
    trial.write_bytes(data)
    try:
        load_config(trial)            # göreli yollar aynı klasöre göre çözülür
    except ConfigError as e:
        raise ConfigEditError(str(e)) from e
    finally:
        trial.unlink(missing_ok=True)
    shutil.copy2(path, path.with_name(path.name + ".yedek"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _header_re(ad: str) -> re.Pattern:
    return re.compile(rf"^\[agents\.{re.escape(ad)}\][ \t]*\r?$", re.M)


def agent_states(path: Path) -> dict[str, bool]:
    """Config'teki tüm ajanlar (kapalılar dahil): {ad: etkin_mi}."""
    text, _ = _read(path)
    data = tomllib.loads(text)
    agents = data.get("agents", {}) if isinstance(data.get("agents"), dict) else {}
    return {name: bool(a.get("enabled", True)) for name, a in agents.items() if isinstance(a, dict)}


def from_catalog(path: Path, ad: str) -> bool:
    text, _ = _read(path)
    return MARK_START.format(ad=ad) in text


def add_agent(path: Path, ad: str, ayar: dict) -> None:
    if not is_safe_id(ad):
        raise ConfigEditError("Geçersiz ajan adı.")
    text, nl = _read(path)
    if _header_re(ad).search(text):
        raise ConfigEditError(f"'{ad}' zaten config'te var. Açmak için: /ajan ac {ad}")
    lines = [MARK_START.format(ad=ad), f"[agents.{ad}]"]
    lines += [f"{k} = {_toml_value(v)}" for k, v in ayar.items()]
    lines.append(MARK_END.format(ad=ad))
    new = text.rstrip("\r\n") + "\n\n" + "\n".join(lines) + "\n"
    _write_validated(path, new, nl)


def remove_agent(path: Path, ad: str) -> None:
    text, nl = _read(path)
    pat = re.compile(rf"\r?\n?{re.escape(MARK_START.format(ad=ad))}.*?{re.escape(MARK_END.format(ad=ad))}[ \t]*\r?\n?",
                     re.S)
    if not pat.search(text):
        raise ConfigEditError(f"'{ad}' katalogdan eklenmemiş; silmek yerine kapatabilirsin: /ajan kapat {ad}")
    _write_validated(path, pat.sub("\n", text, count=1), nl)


def set_enabled(path: Path, ad: str, enabled: bool) -> None:
    text, nl = _read(path)
    m = _header_re(ad).search(text)
    if not m:
        raise ConfigEditError(f"'{ad}' adında bir ajan yok. Katalogdan eklemek için: /ekle {ad}")
    start = m.end()
    nxt = re.compile(r"^\[", re.M).search(text, start)
    end = nxt.start() if nxt else len(text)
    section = text[start:end]
    value = "true" if enabled else "false"
    en_re = re.compile(r"^enabled[ \t]*=.*$", re.M)
    if en_re.search(section):
        section = en_re.sub(f"enabled = {value}", section, count=1)
    else:
        section = f"\nenabled = {value}" + section
    _write_validated(path, text[:start] + section + text[end:], nl)
