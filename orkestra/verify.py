"""Doğrulama komutları (bölüm 9.5, kural 19).

Testleri ajanlar değil Orkestra çalıştırır ve yalnızca config'te tanımlı argv listelerini
kullanır. Model çıktısından hiçbir komut, argüman veya ortam değişkeni türetilmez.
"""

from __future__ import annotations

import asyncio
import os
import re
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


async def run_verify(cmd: VerifyCommand, workspace: Path, passthrough) -> VerifyResult:
    start = time.monotonic()

    def result(code, output, ok):
        return VerifyResult(cmd.name, code, round(time.monotonic() - start, 2),
                            redact(output[-OUTPUT_TAIL:]), ok)

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
        return result(None, f"Zaman aşımı: {cmd.timeout_s} sn içinde bitmedi.", False)
    except asyncio.CancelledError:
        await _kill_tree(proc)
        raise
    text = out_b.decode("utf-8", errors="replace")
    return result(proc.returncode, text, proc.returncode == 0)
