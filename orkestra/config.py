"""config.toml okuma ve doğrulama (bölüm 5, 6.2)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .security import is_safe_id


class ConfigError(Exception):
    """Config dosyası okunamadı veya geçersiz."""


_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
STREAM_FORMATS = ("none", "text", "claude-stream-json", "gemini-stream-json")


@dataclass
class AgentConfig:
    name: str
    display: str
    command: list[str]
    prompt_via: str = "stdin"
    file_args: list[str] = field(default_factory=list)
    output: str = "text"
    json_field: str = ""
    read_only_args: list[str] = field(default_factory=list)
    write_args: list[str] = field(default_factory=list)
    strengths: str = ""
    timeout_s: int = 600
    max_parallel: int = 1
    max_output_chars: int = 60000
    enabled: bool = True
    stream_format: str = "none"
    stream_command: list[str] = field(default_factory=list)
    color: str = ""


@dataclass
class Settings:
    planner: str = "claude"
    reviewer: str = "claude"
    workspace: Path = Path("../orkestra-calisma")
    runs_dir: Path = Path("./runs")
    language: str = "Türkçe"
    max_tasks: int = 8
    max_revisions: int = 1
    max_consult_rounds: int = 2
    max_calls: int = 60
    mode: str = "tek"
    max_phases: int = 6
    dep_output_chars: int = 12000


# Aşama A: [guard], [verify] ve [ui] tabloları sürüm 0.2'nin Aşama B/C kapsamındadır
# (bölüm 6.9). Şimdilik yalnızca bu iki tablo tanınır; diğer her şey hata verir.
_TOP_LEVEL = {"settings", "agents"}


# ---------------------------------------------------------------- tür denetimleri

def _where(section: str, key: str) -> str:
    return f"[{section}] {key}"


def _str(v, where: str, *, allow_empty: bool = True) -> str:
    if not isinstance(v, str):
        raise ConfigError(f"{where}: metin olmalı.")
    if not allow_empty and not v.strip():
        raise ConfigError(f"{where}: boş olamaz.")
    return v


def _int(v, where: str, *, minimum: int | None = None) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ConfigError(f"{where}: tam sayı olmalı.")
    if minimum is not None and v < minimum:
        raise ConfigError(f"{where}: en az {minimum} olmalı.")
    return v


def _bool(v, where: str) -> bool:
    if not isinstance(v, bool):
        raise ConfigError(f"{where}: true veya false olmalı.")
    return v


def _str_list(v, where: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(f"{where}: metin listesi olmalı.")
    if not allow_empty and not v:
        raise ConfigError(f"{where}: boş olamaz.")
    return list(v)


def _choice(v, where: str, options: tuple[str, ...]) -> str:
    if v not in options:
        raise ConfigError(f"{where}: şunlardan biri olmalı: {', '.join(options)}.")
    return v


def _check_unknown(data: dict, known: set[str], section: str) -> None:
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"[{section}] içinde bilinmeyen alan(lar): {', '.join(unknown)}")


# ---------------------------------------------------------------- bölüm ayrıştırıcıları

def _parse_settings(raw: dict, base: Path) -> Settings:
    if not isinstance(raw, dict):
        raise ConfigError("[settings] bir tablo olmalı.")
    known = {f.name for f in fields(Settings)}
    _check_unknown(raw, known, "settings")
    s = Settings()
    w = lambda k: _where("settings", k)  # noqa: E731
    if "planner" in raw:
        s.planner = _str(raw["planner"], w("planner"), allow_empty=False)
    if "reviewer" in raw:
        s.reviewer = _str(raw["reviewer"], w("reviewer"))
    if "workspace" in raw:
        s.workspace = Path(_str(raw["workspace"], w("workspace"), allow_empty=False))
    if "runs_dir" in raw:
        s.runs_dir = Path(_str(raw["runs_dir"], w("runs_dir"), allow_empty=False))
    if "language" in raw:
        s.language = _str(raw["language"], w("language"), allow_empty=False)
    if "max_tasks" in raw:
        s.max_tasks = _int(raw["max_tasks"], w("max_tasks"), minimum=1)
    if "max_revisions" in raw:
        s.max_revisions = _int(raw["max_revisions"], w("max_revisions"), minimum=0)
    if "max_consult_rounds" in raw:
        s.max_consult_rounds = _int(raw["max_consult_rounds"], w("max_consult_rounds"), minimum=0)
    if "max_calls" in raw:
        s.max_calls = _int(raw["max_calls"], w("max_calls"), minimum=3)
    if "mode" in raw:
        s.mode = _choice(raw["mode"], w("mode"), ("tek", "asamali"))
    if "max_phases" in raw:
        s.max_phases = _int(raw["max_phases"], w("max_phases"), minimum=1)
    if "dep_output_chars" in raw:
        s.dep_output_chars = _int(raw["dep_output_chars"], w("dep_output_chars"), minimum=1000)
    # Göreli yollar config dosyasının klasörüne göre çözülür.
    if not s.workspace.is_absolute():
        s.workspace = (base / s.workspace).resolve()
    if not s.runs_dir.is_absolute():
        s.runs_dir = (base / s.runs_dir).resolve()
    return s


def _parse_agent(name: str, raw: dict) -> AgentConfig:
    section = f"agents.{name}"
    if not is_safe_id(name):
        raise ConfigError(f"[{section}]: ajan adı ^[a-z0-9][a-z0-9_-]{{0,39}}$ desenine uymalı.")
    if not isinstance(raw, dict):
        raise ConfigError(f"[{section}] bir tablo olmalı.")
    known = {f.name for f in fields(AgentConfig)} - {"name"}
    _check_unknown(raw, known, section)
    w = lambda k: _where(section, k)  # noqa: E731
    if "display" not in raw:
        raise ConfigError(f"{w('display')}: zorunlu alan eksik.")
    if "command" not in raw:
        raise ConfigError(f"{w('command')}: zorunlu alan eksik.")
    a = AgentConfig(
        name=name,
        display=_str(raw["display"], w("display"), allow_empty=False),
        command=_str_list(raw["command"], w("command"), allow_empty=False),
    )
    if not a.command[0].strip():
        raise ConfigError(f"{w('command')}: program adı boş olamaz.")
    if "prompt_via" in raw:
        a.prompt_via = _choice(raw["prompt_via"], w("prompt_via"), ("stdin", "file"))
    if "file_args" in raw:
        a.file_args = _str_list(raw["file_args"], w("file_args"))
    if "output" in raw:
        a.output = _choice(raw["output"], w("output"), ("text", "json"))
    if "json_field" in raw:
        a.json_field = _str(raw["json_field"], w("json_field"))
    if "read_only_args" in raw:
        a.read_only_args = _str_list(raw["read_only_args"], w("read_only_args"))
    if "write_args" in raw:
        a.write_args = _str_list(raw["write_args"], w("write_args"))
    if "strengths" in raw:
        a.strengths = _str(raw["strengths"], w("strengths"))
    if "timeout_s" in raw:
        a.timeout_s = _int(raw["timeout_s"], w("timeout_s"), minimum=10)
    if "max_parallel" in raw:
        a.max_parallel = _int(raw["max_parallel"], w("max_parallel"), minimum=1)
    if "max_output_chars" in raw:
        a.max_output_chars = _int(raw["max_output_chars"], w("max_output_chars"), minimum=1)
    if "enabled" in raw:
        a.enabled = _bool(raw["enabled"], w("enabled"))
    if "stream_format" in raw:
        a.stream_format = _choice(raw["stream_format"], w("stream_format"), STREAM_FORMATS)
    if "stream_command" in raw:
        a.stream_command = _str_list(raw["stream_command"], w("stream_command"))
    if "color" in raw:
        a.color = _str(raw["color"], w("color"))

    # Alanlar arası kurallar
    if a.prompt_via == "file" and not any("{path}" in x for x in a.file_args):
        raise ConfigError(f"{w('file_args')}: prompt_via=\"file\" ise en az bir argümanda {{path}} olmalı.")
    if a.output == "json" and not a.json_field.strip():
        raise ConfigError(f"{w('json_field')}: output=\"json\" ise zorunludur.")
    if a.stream_format.endswith("-stream-json") and not a.stream_command:
        raise ConfigError(f"{w('stream_command')}: stream_format=\"{a.stream_format}\" ise zorunludur.")
    if a.color and not _COLOR_RE.match(a.color):
        raise ConfigError(f"{w('color')}: #rrggbb biçiminde olmalı.")
    return a


# ---------------------------------------------------------------- giriş noktası

def load_config(path: Path) -> tuple[Settings, dict[str, AgentConfig]]:
    """Config dosyasını okur, doğrular ve (ayarlar, etkin ajanlar) döndürür."""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"Config dosyası bulunamadı: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Config dosyası geçerli TOML değil: {path} ({e})") from e
    except OSError as e:
        raise ConfigError(f"Config dosyası okunamadı: {path} ({e})") from e

    unknown = sorted(set(data) - _TOP_LEVEL)
    if unknown:
        raise ConfigError(f"Config içinde bilinmeyen tablo/alan(lar): {', '.join(unknown)}")

    base = path.resolve().parent
    settings = _parse_settings(data.get("settings", {}), base)

    raw_agents = data.get("agents", {})
    if not isinstance(raw_agents, dict):
        raise ConfigError("[agents] bir tablo olmalı.")
    agents: dict[str, AgentConfig] = {}
    for name, raw in raw_agents.items():
        cfg = _parse_agent(name, raw)
        if cfg.enabled:
            agents[name] = cfg

    if not agents:
        raise ConfigError("Etkin hiçbir ajan yok; en az bir ajan enabled=true olmalı.")
    if settings.planner not in agents:
        raise ConfigError(f"Planlayıcı ajan '{settings.planner}' tanımlı veya etkin değil.")
    if settings.reviewer and settings.reviewer not in agents:
        raise ConfigError(f"Denetçi ajan '{settings.reviewer}' tanımlı veya etkin değil.")
    return settings, agents


def default_config_path() -> Path:
    """Paket klasörünün bir üstündeki config.toml."""
    return Path(__file__).resolve().parent.parent / "config.toml"
