"""Ajanlar: resmi CLI araçlarını alt süreç olarak çalıştıran CliAgent ve sahte MockAgent (bölüm 6.3)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
from pathlib import Path

from .config import AgentConfig
from .security import SecurityError, check_argv, make_private_dir, redact


class AgentError(Exception):
    """Ajan çağrısı başarısız oldu."""


# Etiketler motor tarafından yalnızca güvenli kimliklerden üretilir (ör. "t1-danisma-5").
# Yine de dosya adı olarak kullanılmadan önce burada ikinci kez denetlenir.
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")


class Agent:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg

    @property
    def name(self) -> str:
        return self.cfg.name

    async def run(self, prompt: str, *, write: bool, workdir: Path, tag: str) -> str:
        raise NotImplementedError


# ====================================================================== CliAgent

class CliAgent(Agent):
    """Resmi CLI aracını kabuk kullanmadan, liste argümanlarla çalıştırır."""

    def resolve(self) -> str:
        exe = shutil.which(self.cfg.command[0])
        if not exe:
            raise AgentError(
                f"{self.name}: '{self.cfg.command[0]}' programı bulunamadı. "
                f"Kurulumu kontrol etmek için 'python -m orkestra doctor' çalıştır."
            )
        return exe

    async def run(self, prompt: str, *, write: bool, workdir: Path, tag: str) -> str:
        cfg = self.cfg
        if not _TAG_RE.match(tag):
            raise SecurityError(f"Güvensiz çağrı etiketi: {tag[:60]!r}")
        workdir = Path(workdir)
        argv = [self.resolve(), *cfg.command[1:], *(cfg.write_args if write else cfg.read_only_args)]
        stdin_data: bytes | None = None
        prompt_file: Path | None = None
        try:
            if cfg.prompt_via == "stdin":
                stdin_data = prompt.encode("utf-8")
            else:
                pdir = make_private_dir(workdir / ".orkestra" / "istemler")
                prompt_file = pdir / f"{tag}.md"
                prompt_file.write_text(prompt, encoding="utf-8")
                rel = prompt_file.relative_to(workdir).as_posix()
                argv += [a.replace("{path}", rel) for a in cfg.file_args]
            check_argv(argv)

            kwargs = {}
            if os.name != "nt":
                kwargs["start_new_session"] = True
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workdir),
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(stdin_data), cfg.timeout_s)
            except asyncio.TimeoutError:
                await _kill_tree(proc)
                raise AgentError(f"{self.name}: {cfg.timeout_s} sn içinde yanıt vermedi.") from None
            except asyncio.CancelledError:
                await _kill_tree(proc)
                raise
        finally:
            if prompt_file is not None:
                try:
                    prompt_file.unlink(missing_ok=True)
                except OSError:
                    pass

        out = out_b.decode("utf-8", errors="replace")
        err = err_b.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            detail = redact((err.strip() or out.strip())[-1500:])
            raise AgentError(f"{self.name}: çıkış kodu {proc.returncode}. {detail}")
        text = self._extract(out) if cfg.output == "json" else out
        return text.strip()[: cfg.max_output_chars]

    def _extract(self, text: str) -> str:
        data = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            i = text.find("{")
            if i != -1:
                try:
                    data = json.loads(text[i:])
                except json.JSONDecodeError:
                    data = None
        if data is None:
            raise AgentError(f"{self.name}: JSON çıktısı okunamadı.")
        if not isinstance(data, dict):
            raise AgentError(f"{self.name}: JSON çıktısı bir nesne değil.")
        if data.get("is_error") or data.get("error"):
            detail = data.get("error") or data.get(self.cfg.json_field) or data
            if not isinstance(detail, str):
                detail = json.dumps(detail, ensure_ascii=False)
            raise AgentError(f"{self.name}: CLI hata bildirdi: {redact(detail)[:800]}")
        value = data.get(self.cfg.json_field)
        if not isinstance(value, str):
            raise AgentError(f"{self.name}: JSON çıktısında '{self.cfg.json_field}' metin alanı yok.")
        return value

    async def version(self) -> str:
        exe = self.resolve()
        argv = [exe, "--version"]
        check_argv(argv)
        kwargs = {"start_new_session": True} if os.name != "nt" else {}
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, **kwargs,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), 30)
        except asyncio.TimeoutError:
            await _kill_tree(proc)
            raise AgentError(f"{self.name}: sürüm sorgusu 30 sn içinde yanıt vermedi.") from None
        if proc.returncode != 0:
            raise AgentError(f"{self.name}: sürüm sorgusu başarısız (çıkış kodu {proc.returncode}).")
        text = (out_b or err_b).decode("utf-8", errors="replace").strip()
        return redact(text.splitlines()[0]) if text else "(sürüm bilgisi yok)"


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Süreci ve tüm alt süreçlerini sonlandırır."""
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/T", "/F", "/PID", str(proc.pid),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


# ====================================================================== MockAgent

class MockAgent(Agent):
    """Testler ve --mock modu için deterministik sahte ajan."""

    def __init__(self, cfg: AgentConfig, all_names: list[str]):
        super().__init__(cfg)
        self.all_names = list(all_names)

    async def run(self, prompt: str, *, write: bool, workdir: Path, tag: str) -> str:
        await asyncio.sleep(0.05)
        n = self.all_names
        role = prompt.split("\n", 1)[0]
        if "<<ROL:PLANLAYICI>>" in role:
            plan = {
                "summary": "Sahte plan: iki bağımsız görev ve bunları birleştiren üçüncü görev.",
                "tasks": [
                    {"id": "t1", "title": "Araştırma", "assignee": n[1 % len(n)], "depends_on": [],
                     "instructions": "Konuyu araştır.", "expected_output": "Kısa notlar.",
                     "needs_write": False},
                    {"id": "t2", "title": "Taslak kod", "assignee": n[2 % len(n)], "depends_on": [],
                     "instructions": "Taslak kod yaz.", "expected_output": "Kod parçası.",
                     "needs_write": False},
                    {"id": "t3", "title": "Birleştirme", "assignee": n[0], "depends_on": ["t1", "t2"],
                     "instructions": "Sonuçları birleştir.", "expected_output": "Birleşik çıktı.",
                     "needs_write": False},
                ],
            }
            return "```json\n" + json.dumps(plan, ensure_ascii=False, indent=2) + "\n```"
        if "<<ROL:DENETCI>>" in role:
            return '{"verdict": "approve", "feedback": "Uygun."}'
        if "<<ROL:DANISMAN>>" in role:
            return f"[{self.name}] danışman görüşü."
        if "<<ROL:SENTEZ>>" in role:
            return "# Sonuç\n\nSahte sentez raporu."
        if "<<ROL:ISCI>>" in role:
            if self.name == n[0] and "DANIŞMA CEVAPLARI" not in prompt:
                return f'Taslak hazır.\n<<SOR ajan="{n[1 % len(n)]}">>Bu yaklaşım hakkında ne düşünüyorsun?<</SOR>>'
            return f"[{self.name}] görev çıktısı."
        return f"[{self.name}] bilinmeyen rol."
