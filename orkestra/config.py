"""config.toml okuma ve doğrulama (bölüm 5, 6.2)."""

from __future__ import annotations

import os
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
    # Sürüm 0.3: oturum sürekliliği ve kullanım bilgisi (yalnızca JSON çıktılı CLI'lar)
    resume_args: list[str] = field(default_factory=list)   # en az birinde {session} olmalı
    session_field: str = ""                                 # JSON çıktısındaki oturum kimliği alanı


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
    # Sürüm 0.3
    fallback: bool = True          # kota/hata durumunda görevi başka ajana devret
    strict_guard: bool = False     # sıkı mod: yazma ve doğrulama sürerken okuma çağrıları da bekler
    checkpoints: bool = True       # git ile kontrol noktaları (git kuruluysa)


DEFAULT_PROTECTED = [".git", ".github", ".claude", ".gemini", ".copilot", ".vscode", ".idea",
                     ".mcp.json", "CLAUDE.md", "GEMINI.md", "AGENTS.md", ".orkestra"]
DEFAULT_ENV_PASSTHROUGH = ["PATH", "PATHEXT", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "HOME",
                           "USERPROFILE", "APPDATA", "LOCALAPPDATA", "LANG", "PYTHONIOENCODING"]


@dataclass
class GuardConfig:
    protected_paths: list[str] = field(default_factory=lambda: list(DEFAULT_PROTECTED))
    max_snapshot_files: int = 20000


@dataclass
class VerifyCommand:
    name: str
    argv: list[str]
    timeout_s: int = 300


@dataclass
class VerifyConfig:
    max_verify_runs: int = 30
    env_passthrough: list[str] = field(default_factory=lambda: list(DEFAULT_ENV_PASSTHROUGH))
    commands: list[VerifyCommand] = field(default_factory=list)
    sandbox: str = "none"          # "none" | "docker"
    docker_image: str = "python:3.12-slim"

    def get(self, name: str) -> VerifyCommand | None:
        return next((c for c in self.commands if c.name == name), None)


@dataclass
class UiConfig:
    port: int = 8765
    open_browser: bool = True


@dataclass
class Config:
    settings: Settings
    agents: dict[str, AgentConfig]
    guard: GuardConfig
    verify: VerifyConfig
    ui: UiConfig
    path: Path

    @property
    def dir(self) -> Path:
        return self.path.resolve().parent


_TOP_LEVEL = {"settings", "agents", "guard", "verify", "ui"}


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
    for key in ("fallback", "strict_guard", "checkpoints"):
        if key in raw:
            setattr(s, key, _bool(raw[key], w(key)))
    # '~' ev dizinine açılır; göreli yollar config dosyasının klasörüne göre çözülür.
    s.workspace, s.runs_dir = s.workspace.expanduser(), s.runs_dir.expanduser()
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
    if "resume_args" in raw:
        a.resume_args = _str_list(raw["resume_args"], w("resume_args"))
    if "session_field" in raw:
        a.session_field = _str(raw["session_field"], w("session_field"))

    # Alanlar arası kurallar
    if a.prompt_via == "file" and not any("{path}" in x for x in a.file_args):
        raise ConfigError(f"{w('file_args')}: prompt_via=\"file\" ise en az bir argümanda {{path}} olmalı.")
    if a.output == "json" and not a.json_field.strip():
        raise ConfigError(f"{w('json_field')}: output=\"json\" ise zorunludur.")
    if a.stream_format.endswith("-stream-json") and not a.stream_command:
        raise ConfigError(f"{w('stream_command')}: stream_format=\"{a.stream_format}\" ise zorunludur.")
    if a.resume_args and not any("{session}" in x for x in a.resume_args):
        raise ConfigError(f"{w('resume_args')}: en az bir argümanda {{session}} olmalı.")
    if a.resume_args and not a.session_field:
        raise ConfigError(f"{w('session_field')}: resume_args kullanılıyorsa zorunludur.")
    if a.color and not _COLOR_RE.match(a.color):
        raise ConfigError(f"{w('color')}: #rrggbb biçiminde olmalı.")
    return a


def normalize_rel_path(value: str) -> str | None:
    """Göreli yolu '/' ayraçlı biçime getirir; güvensizse None döndürür.

    Mutlak yol, sürücü harfi, '..' veya '.' bileşeni ve boş bileşen reddedilir.
    Ayrıca ':' (Windows alternatif veri akışları, ör. CLAUDE.md::$DATA), kontrol karakterleri ve
    sonu nokta/boşlukla biten bileşenler (Windows bunları siler: 'CLAUDE.md.' == 'CLAUDE.md')
    reddedilir; aksi halde korumalı yol denetimi atlatılabilirdi.
    Sondaki '/' korunur (klasörün altındaki her şeyi kapsar).
    """
    if not isinstance(value, str):
        return None
    v = value.strip().replace("\\", "/")
    if not v or v.startswith("/") or re.match(r"^[A-Za-z]:", v) or "\x00" in v:
        return None
    is_dir = v.endswith("/")
    body = v[:-1] if is_dir else v
    parts = body.split("/")
    if not body or any(p in ("", ".", "..") for p in parts):
        return None
    if ":" in v or any(ord(c) < 32 or ord(c) == 127 for c in v):
        return None
    if any(p != p.rstrip(". ") for p in parts):
        return None
    return body + ("/" if is_dir else "")


def _parse_guard(raw) -> GuardConfig:
    if not isinstance(raw, dict):
        raise ConfigError("[guard] bir tablo olmalı.")
    _check_unknown(raw, {"protected_paths", "max_snapshot_files"}, "guard")
    g = GuardConfig()
    if "protected_paths" in raw:
        items = _str_list(raw["protected_paths"], _where("guard", "protected_paths"))
        paths = []
        for item in items:
            norm = normalize_rel_path(item)
            if norm is None:
                raise ConfigError(f"[guard] protected_paths: geçersiz yol: {item!r} (göreli ve '..' içermeyen olmalı)")
            paths.append(norm.rstrip("/"))
        g.protected_paths = paths
    if ".orkestra" not in [p.lower() for p in g.protected_paths]:
        g.protected_paths.append(".orkestra")  # her zaman korumalı (bölüm 5.4)
    if "max_snapshot_files" in raw:
        g.max_snapshot_files = _int(raw["max_snapshot_files"], _where("guard", "max_snapshot_files"), minimum=100)
    return g


def _parse_verify(raw) -> VerifyConfig:
    if not isinstance(raw, dict):
        raise ConfigError("[verify] bir tablo olmalı.")
    _check_unknown(raw, {"max_verify_runs", "env_passthrough", "commands", "sandbox", "docker_image"}, "verify")
    v = VerifyConfig()
    if "sandbox" in raw:
        v.sandbox = _choice(raw["sandbox"], _where("verify", "sandbox"), ("none", "docker"))
    if "docker_image" in raw:
        img = _str(raw["docker_image"], _where("verify", "docker_image"), allow_empty=False)
        # İmaj adı argv'ye girer: yalnızca güvenli karakterler (ör. python:3.12-slim, ghcr.io/x/y@sha256:...)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}", img):
            raise ConfigError("[verify] docker_image: geçersiz imaj adı.")
        v.docker_image = img
    if "max_verify_runs" in raw:
        v.max_verify_runs = _int(raw["max_verify_runs"], _where("verify", "max_verify_runs"), minimum=0)
    if "env_passthrough" in raw:
        v.env_passthrough = _str_list(raw["env_passthrough"], _where("verify", "env_passthrough"))
    cmds = raw.get("commands", [])
    if not isinstance(cmds, list):
        raise ConfigError("[[verify.commands]] bir tablo listesi olmalı.")
    seen = set()
    for i, c in enumerate(cmds, 1):
        where = f"verify.commands[{i}]"
        if not isinstance(c, dict):
            raise ConfigError(f"[{where}] bir tablo olmalı.")
        _check_unknown(c, {"name", "argv", "timeout_s"}, where)
        name = c.get("name")
        if not is_safe_id(name if isinstance(name, str) else ""):
            raise ConfigError(f"[{where}] name: ^[a-z0-9][a-z0-9_-]{{0,39}}$ desenine uyan bir ad olmalı.")
        if name in seen:
            raise ConfigError(f"[{where}] name: '{name}' birden fazla kez tanımlanmış.")
        seen.add(name)
        if "argv" not in c:
            raise ConfigError(f"[{where}] argv: zorunlu alan eksik.")
        argv = _str_list(c["argv"], f"[{where}] argv", allow_empty=False)
        timeout = _int(c.get("timeout_s", 300), f"[{where}] timeout_s", minimum=10)
        v.commands.append(VerifyCommand(name=name, argv=argv, timeout_s=timeout))
    return v


def _parse_ui(raw) -> UiConfig:
    if not isinstance(raw, dict):
        raise ConfigError("[ui] bir tablo olmalı.")
    _check_unknown(raw, {"port", "open_browser"}, "ui")
    u = UiConfig()
    if "port" in raw:
        u.port = _int(raw["port"], _where("ui", "port"), minimum=1024)
        if u.port > 65535:
            raise ConfigError("[ui] port: en fazla 65535 olmalı.")
    if "open_browser" in raw:
        u.open_browser = _bool(raw["open_browser"], _where("ui", "open_browser"))
    return u


# ---------------------------------------------------------------- giriş noktası

def load_config(path: Path) -> Config:
    """Config dosyasını okur, doğrular ve Config nesnesi döndürür (yalnızca etkin ajanlar)."""
    path = Path(path)
    try:
        # Windows Not Defteri gibi editörler dosyanın başına UTF-8 BOM ekleyebilir; tomllib bunu kabul
        # etmediği için okunmadan önce çıkarılır.
        raw = path.read_bytes().removeprefix(b"\xef\xbb\xbf")
        data = tomllib.loads(raw.decode("utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"Config dosyası bulunamadı: {path}") from e
    except UnicodeDecodeError as e:
        raise ConfigError(f"Config dosyası UTF-8 değil: {path} ({e}). Dosyayı UTF-8 olarak kaydet.") from e
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
    return Config(
        settings=settings,
        agents=agents,
        guard=_parse_guard(data.get("guard", {})),
        verify=_parse_verify(data.get("verify", {})),
        ui=_parse_ui(data.get("ui", {})),
        path=path.resolve(),
    )


PACKAGED_DEFAULT = Path(__file__).resolve().parent / "varsayilan_config.toml"


def user_config_dir() -> Path:
    """pip/pipx ile kurulumda kullanıcı config klasörü."""
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Orkestra"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "orkestra"


def default_config_path() -> Path:
    """Varsayılan config dosyasını bulur.

    Sıra: ORKESTRA_CONFIG ortam değişkeni → paket klasörünün bir üstündeki config.toml (depodan
    çalıştırma, bölüm 5) → kullanıcı config klasörü (pip/pipx kurulumu). Son durumda dosya yoksa
    paketteki varsayılandan oluşturulur; çalışma klasörü ~/orkestra-calisma olur.
    """
    env = os.environ.get("ORKESTRA_CONFIG")
    if env:
        return Path(env).expanduser()
    repo = Path(__file__).resolve().parent.parent / "config.toml"
    if repo.is_file():
        return repo
    target = user_config_dir() / "config.toml"
    if not target.exists() and PACKAGED_DEFAULT.is_file():
        text = PACKAGED_DEFAULT.read_text(encoding="utf-8").replace(
            'workspace = "../orkestra-calisma"', 'workspace = "~/orkestra-calisma"')
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(target.parent, 0o700)
        target.write_text(text, encoding="utf-8")
    return target
