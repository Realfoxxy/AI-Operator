"""Hesap bağlama: resmi CLI'ların kurulumu, girişi ve bağlantı testi (sürüm 0.3).

Kimlik doğrulama tamamen resmi CLI'ların kendi giriş akışlarına bırakılır (kural 14): Orkestra hiçbir
token veya parola okumaz, saklamaz. Bu modül yalnızca:
  - CLI'ın kurulu olup olmadığını denetler ve npm ile kurmayı önerir,
  - giriş için CLI'ı kullanıcının terminalinde etkileşimli olarak açar (kabuk yok, liste argümanlar),
  - isteğe bağlı küçük bir istemle bağlantıyı dener ve sonucu (yalnızca durum ve tarih) kaydeder.
Her kullanıcı kendi bilgisayarında kendi hesabıyla giriş yapar; CLI'lar kimlik bilgilerini kullanıcının
ev dizininde kendileri tutar.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .agents import AgentError, CliAgent
from .config import AgentConfig, Config, user_config_dir
from .security import SecurityError, check_argv, redact

TOOLS = {
    "claude": {
        "paket": "@anthropic-ai/claude-code",
        "abonelik": "Claude Pro / Max",
        "giris": ["Açılan Claude Code içinde /login yaz ve Enter'a bas.",
                  "Tarayıcıda Claude hesabınla (Pro/Max) giriş yap ve izin ver.",
                  "Terminale dönünce /exit yazarak çık."],
    },
    "gemini": {
        "paket": "@google/gemini-cli",
        "abonelik": "Google AI Pro",
        "giris": ["İlk açılışta kimlik doğrulama seçeneklerinden 'Login with Google'ı seç.",
                  "Tarayıcıda Google AI Pro aboneliğinin olduğu Google hesabıyla giriş yap.",
                  "Terminale dönünce /quit yazarak çık."],
    },
    "copilot": {
        "paket": "@github/copilot",
        "abonelik": "GitHub Copilot",
        "giris": ["Copilot klasöre güvenmeni isterse onayla (bu, Orkestra'nın çalışacağı proje klasörüdür).",
                  "/login yaz; ekrandaki kodu tarayıcıda github.com/login/device adresine gir.",
                  "Giriş bitince /exit yazarak çık."],
    },
}
PROBE_PROMPT = "Bu bir bağlantı testidir. Başka hiçbir şey yapma; yalnızca TAMAM yaz."
STATUS_FILE = "hesaplar.json"


def tool_of(acfg: AgentConfig) -> str | None:
    """Ajanın kullandığı resmi aracı bulur (komut adına göre: claude, gemini, copilot)."""
    name = Path(acfg.command[0]).name.lower()
    for ext in (".cmd", ".exe", ".bat", ".ps1"):
        name = name.removesuffix(ext)
    return name if name in TOOLS else None


def _status_path() -> Path:
    return user_config_dir() / STATUS_FILE


def load_status() -> dict:
    try:
        data = json.loads(_status_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_status(agent: str, ok: bool, message: str) -> None:
    data = load_status()
    data[agent] = {"basarili": ok, "zaman": int(time.time()), "mesaj": redact(message)[:300]}
    p = _status_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def rows(cfg: Config) -> list[dict]:
    """Her ajan için hesap durumu satırı."""
    status = load_status()
    out = []
    for name, a in cfg.agents.items():
        tool = tool_of(a)
        exe = shutil.which(a.command[0])
        st = status.get(name) or {}
        out.append({
            "ajan": name, "gorunen": a.display, "arac": tool, "kurulu": bool(exe), "yol": exe or "",
            "paket": TOOLS.get(tool, {}).get("paket", ""), "abonelik": TOOLS.get(tool, {}).get("abonelik", ""),
            "planlayici": name == cfg.settings.planner, "denetci": name == cfg.settings.reviewer,
            "test": st.get("basarili"), "test_zamani": st.get("zaman"), "test_mesaji": st.get("mesaj", ""),
        })
    return out


def missing_packages(cfg: Config) -> list[str]:
    return sorted({r["paket"] for r in rows(cfg) if not r["kurulu"] and r["paket"]})


def install(packages: list[str]) -> int:
    """Eksik CLI'ları npm ile genel olarak kurar (çıktı doğrudan kullanıcının terminaline akar)."""
    npm = shutil.which("npm")
    if not npm:
        raise FileNotFoundError("npm bulunamadı. Önce Node.js LTS kur: https://nodejs.org/")
    for p in packages:
        if p not in {t["paket"] for t in TOOLS.values()}:
            raise SecurityError(f"Bilinmeyen paket: {p}")
    argv = [npm, "install", "-g", *packages]
    check_argv(argv)
    return subprocess.run(argv, check=False).returncode


def login(acfg: AgentConfig, workspace: Path) -> int:
    """CLI'ı giriş için etkileşimli açar; kullanıcı çıkınca döner. Copilot klasör güvenini de burada sorar."""
    exe = shutil.which(acfg.command[0])
    if not exe:
        raise FileNotFoundError(f"'{acfg.command[0]}' kurulu değil.")
    argv = [exe]
    check_argv(argv)
    Path(workspace).mkdir(parents=True, exist_ok=True)
    return subprocess.run(argv, cwd=str(workspace), check=False).returncode


async def probe(acfg: AgentConfig, workspace: Path, timeout_s: int = 180) -> tuple[bool, str]:
    """Küçük bir istemle bağlantıyı dener (aboneliğinden çok küçük bir kullanım harcar)."""
    test_cfg = AgentConfig(**{**acfg.__dict__, "timeout_s": min(acfg.timeout_s, timeout_s), "stream_format": "none",
                              "resume_args": []})
    try:
        out = await CliAgent(test_cfg).run(PROBE_PROMPT, write=False, workdir=Path(workspace), tag="baglanti-testi")
    except (AgentError, SecurityError, OSError) as e:
        msg = redact(str(e))
        save_status(acfg.name, False, msg)
        return False, msg
    ok = bool(out.strip())
    msg = f"Yanıt alındı: {out.strip()[:60]}" if ok else "Boş yanıt."
    save_status(acfg.name, ok, msg)
    return ok, msg


def probe_sync(acfg: AgentConfig, workspace: Path) -> tuple[bool, str]:
    return asyncio.run(probe(acfg, workspace))
