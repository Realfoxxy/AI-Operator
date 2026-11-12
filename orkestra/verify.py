"""Doğrulama komutları (bölüm 9.5, kural 19).

Testleri ajanlar değil Orkestra çalıştırır ve yalnızca config'te tanımlı argv listelerini
kullanır. Model çıktısından hiçbir komut, argüman veya ortam değişkeni türetilmez.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .agents import _kill_tree
from .config import VerifyCommand
from .security import SecurityError, check_argv, redact

OUTPUT_TAIL = 6000
_SECRET_SUBSTRINGS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")


@dataclass
class VerifyResult:
    name: str
    exit_code: int | None      # None: zaman aşımı veya başlatılamadı
    duration_s: float
    output: str
    ok: bool


def is_secret_name(name: str) -> bool:
    """Adında TOKEN, KEY, SECRET, PASSWORD, CREDENTIAL veya PAT geçen değişkenler gizli sayılır.

    PAT, PATH ve PATHEXT'i yanlışlıkla elememek için alt dize olarak değil, ayrı bir sözcük
    (ör. GITHUB_PAT, PAT_TOKEN) olarak aranır.
    """
    up = name.upper()
    if any(s in up for s in _SECRET_SUBSTRINGS):
        return True
    return "PAT" in re.split(r"[^A-Z0-9]+", up)


def filter_env(passthrough) -> dict[str, str]:
    env = {}
    for name in passthrough:
        if is_secret_name(name):
            continue
        val = os.environ.get(name)
        if val is not None:
            env[name] = val
    return env


def docker_argv(cmd: VerifyCommand, workspace: Path, image: str, name: str = "") -> list[str] | None:
    """Doğrulama komutunu ağsız, kaynak sınırlı bir Docker kapsayıcısında çalıştıran argv.

    Yalnızca çalışma klasörü /work olarak bağlanır; ağ kapalıdır (--network none); ayrıcalık
    yükseltme ve ek yetenekler kapatılır. Komut yine config'teki argv'dir.
    """
    docker = shutil.which("docker")
    if not docker:
        return None
    argv = [docker, "run", "--rm", "-i", *(["--name", name] if name else []), "--network", "none", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "512", "--memory", "2g", "--cpus", "2",
            "-v", f"{Path(workspace).resolve()}:/work", "-w", "/work"]
    if os.name != "nt" and hasattr(os, "getuid"):
        argv += ["--user", f"{os.getuid()}:{os.getgid()}"]   # oluşan dosyalar root'a ait olmasın
    return argv + [image, *cmd.argv]


async def _docker_kill(container: str) -> None:
    """İstemciyi öldürmek kapsayıcıyı durdurmaz; zaman aşımında kapsayıcı adıyla durdurulur."""
    docker = shutil.which("docker") if container else None
    if not docker:
        return
    try:
        p = await asyncio.create_subprocess_exec(docker, "kill", container, stdin=asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), 30)
    except (OSError, asyncio.TimeoutError):
        pass


async def run_verify(cmd: VerifyCommand, workspace: Path, passthrough, *, sandbox: str = "none",
                     image: str = "python:3.12-slim") -> VerifyResult:
    start = time.monotonic()

    def result(code, output, ok):
        return VerifyResult(cmd.name, code, round(time.monotonic() - start, 2),
                            redact(output[-OUTPUT_TAIL:]), ok)

    container = f"orkestra-dogrulama-{secrets.token_hex(6)}" if sandbox == "docker" else ""
    if sandbox == "docker":
        argv = docker_argv(cmd, workspace, image, container)
        if argv is None:
            return result(None, "Docker bulunamadı; [verify] sandbox = \"docker\" için Docker kurulu olmalı.", False)
    else:
        exe = shutil.which(cmd.argv[0])
        if not exe:
            return result(None, f"'{cmd.argv[0]}' programı bulunamadı.", False)
        argv = [exe, *cmd.argv[1:]]
    try:
        check_argv(argv)
    except SecurityError as e:
        return result(None, str(e), False)
    kwargs = {"start_new_session": True} if os.name != "nt" else {}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(workspace), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=filter_env(passthrough), **kwargs,
        )
    except OSError as e:
        return result(None, f"Komut başlatılamadı: {e}", False)
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), cmd.timeout_s)
    except asyncio.TimeoutError:
        await _kill_tree(proc)
        await _docker_kill(container)
        return result(None, f"Zaman aşımı: {cmd.timeout_s} sn içinde bitmedi.", False)
    except asyncio.CancelledError:
        await _kill_tree(proc)
        await _docker_kill(container)
        raise
    text = out_b.decode("utf-8", errors="replace")
    return result(proc.returncode, text, proc.returncode == 0)
