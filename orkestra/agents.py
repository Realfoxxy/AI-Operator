"""Ajanlar: resmi CLI araçlarını alt süreç olarak çalıştıran CliAgent ve sahte MockAgent (bölüm 6.3)."""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import re
import shutil
import signal
from pathlib import Path
from typing import Callable

OnChunk = Callable[[str], None] | None
STDERR_LIMIT = 64 * 1024
LINE_LIMIT = 8 * 1024 * 1024      # tek bir JSON satırı için üst sınır (bellek koruması)

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

    async def run(self, prompt: str, *, write: bool, workdir: Path, tag: str, on_chunk: OnChunk = None) -> str:
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

    async def run(self, prompt: str, *, write: bool, workdir: Path, tag: str, on_chunk: OnChunk = None) -> str:
        cfg = self.cfg
        if not _TAG_RE.match(tag):
            raise SecurityError(f"Güvensiz çağrı etiketi: {tag[:60]!r}")
        workdir = Path(workdir)
        streaming = cfg.stream_format != "none"
        base = cfg.stream_command if cfg.stream_format.endswith("-stream-json") else cfg.command
        exe = shutil.which(base[0]) if base is cfg.stream_command else self.resolve()
        if not exe:
            raise AgentError(f"{self.name}: '{base[0]}' programı bulunamadı. "
                             f"Kurulumu kontrol etmek için 'python -m orkestra doctor' çalıştır.")
        argv = [exe, *base[1:], *(cfg.write_args if write else cfg.read_only_args)]
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
                if streaming:
                    return await asyncio.wait_for(self._stream(proc, stdin_data, on_chunk), cfg.timeout_s)
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

    # ------------------------------------------------------------------ canlı akış (bölüm 10.3)

    async def _stream(self, proc, stdin_data: bytes | None, on_chunk: OnChunk) -> str:
        cfg = self.cfg
        emit = on_chunk or (lambda _t: None)
        err_buf = bytearray()
        out_parts: list[str] = []
        sum_len = [0]
        state = {"result": None, "is_error": False, "texts": []}

        async def feed():
            if stdin_data is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

        async def drain_err():
            # stderr eş zamanlı boşaltılır; boru dolup kilitlenme olmasın diye 64 KB ile sınırlı.
            while chunk := await proc.stderr.read(65536):
                err_buf.extend(chunk)
                if len(err_buf) > STDERR_LIMIT:
                    del err_buf[:-STDERR_LIMIT]

        async def read_out():
            dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
            pending = ""
            while True:
                chunk = await proc.stdout.read(4096 if cfg.stream_format == "text" else 65536)
                text = dec.decode(chunk, final=not chunk)
                if cfg.stream_format == "text":
                    if text:
                        # Bellek koruması: max_output_chars'ın iki katından fazlası tutulmaz (sonradan kesilir).
                        if sum_len[0] < cfg.max_output_chars * 2:
                            out_parts.append(text)
                            sum_len[0] += len(text)
                        emit(text)
                else:
                    pending += text
                    if len(pending) > LINE_LIMIT and "\n" not in pending:
                        pending = ""          # satır sonu gelmeyen dev satır atılır
                    *lines, pending = pending.split("\n")
                    if not chunk:
                        lines.append(pending)
                    for line in lines:
                        self._handle_json_line(line, state, emit)
                if not chunk:
                    return

        await asyncio.gather(feed(), drain_err(), read_out())
        await proc.wait()
        if proc.returncode != 0:
            err = err_buf.decode("utf-8", errors="replace").strip() or "".join(out_parts).strip()
            raise AgentError(f"{self.name}: çıkış kodu {proc.returncode}. {redact(err[-1500:])}")
        if cfg.stream_format == "text":
            text = "".join(out_parts)
            text = self._extract(text) if cfg.output == "json" else text
        elif cfg.stream_format == "claude-stream-json":
            if state["is_error"]:
                raise AgentError(f"{self.name}: CLI hata bildirdi: {redact(str(state['result']))[:800]}")
            if not isinstance(state["result"], str):
                raise AgentError(f"{self.name}: akışta sonuç satırı bulunamadı.")
            text = state["result"]
        else:  # gemini-stream-json
            text = state["result"] if isinstance(state["result"], str) and state["result"] else "".join(state["texts"])
        return text.strip()[: cfg.max_output_chars]

    def _handle_json_line(self, line: str, state: dict, emit) -> None:
        line = line.strip()
        if not line:
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return  # ayrıştırılamayan satırlar yok sayılır
        if not isinstance(obj, dict):
            return
        typ = obj.get("type")
        if self.cfg.stream_format == "claude-stream-json":
            if typ == "assistant":
                content = (obj.get("message") or {}).get("content") or []
                for item in content if isinstance(content, list) else []:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text" and isinstance(item.get("text"), str):
                        emit(item["text"])
                    elif item.get("type") == "tool_use":
                        emit(f"🔧 {str(item.get('name', 'araç'))[:60]}")   # araç girdisi gösterilmez
            elif typ == "result":
                state["result"] = obj.get("result")
                state["is_error"] = bool(obj.get("is_error"))
        else:
            if typ == "result":
                if isinstance(obj.get("response"), str):
                    state["result"] = obj["response"]
                return
            if obj.get("role") in ("user", "system"):
                return
            text = obj.get("content") if isinstance(obj.get("content"), str) else obj.get("text")
            if isinstance(text, str) and text:
                state["size"] = state.get("size", 0) + len(text)
                if state["size"] <= self.cfg.max_output_chars * 2:   # bellek koruması
                    state["texts"].append(text)
                emit(text)

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
    """Testler ve --mock modu için deterministik sahte ajan.

    hiz: bekleme sürelerinin çarpanı (0 = beklemesiz; testler 0 kullanır).
    """

    def __init__(self, cfg: AgentConfig, all_names: list[str], hiz: float = 1.0):
        super().__init__(cfg)
        self.all_names = list(all_names)
        self.hiz = hiz

    async def _sleep(self, sec: float) -> None:
        await asyncio.sleep(sec * self.hiz)

    async def _chunked(self, text: str, on_chunk: OnChunk) -> str:
        """Nihai metni 3–5 parça halinde, parçalar arasında 0,2–0,4 sn bekleyerek verir."""
        if on_chunk is None:
            return text
        n = 3 + (len(text) % 3)
        size = max(1, -(-len(text) // n))
        parts = [text[i:i + size] for i in range(0, len(text), size)]
        for i, part in enumerate(parts):
            on_chunk(part)
            if i < len(parts) - 1:
                await self._sleep(0.2 + 0.1 * (i % 3))
        return text

    def _plan(self, phase: int | None) -> dict:
        n = self.all_names
        if phase is not None and phase >= 2:
            return {"summary": "Tamamlandı", "phase_title": "Bitiş", "done": True, "tasks": []}
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
        if phase == 1:
            plan["tasks"][0]["needs_write"] = True
            plan["tasks"][0]["files"] = ["ornek/notlar.md"]
            plan["phase_title"] = "İskelet"
            plan["done"] = False
        return plan

    async def run(self, prompt: str, *, write: bool, workdir: Path, tag: str, on_chunk: OnChunk = None) -> str:
        await self._sleep(0.05)
        n = self.all_names
        head = prompt.split("\n", 1)[0]
        if write:
            # Değişiklik denetimi ve Dosyalar paneli sahte modda da görülebilsin diye.
            target = Path(workdir) / "ornek" / "notlar.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# Notlar\n\n[{self.name}] tarafından yazıldı ({tag}).\n", encoding="utf-8")
        if "<<ROL:PLANLAYICI>>" in head:
            m = re.search(r"<<ASAMA:(\d+)>>", head)
            plan = self._plan(int(m.group(1)) if m else None)
            # Arayüzde planlayıcının "düşünüyor → yazıyor" geçişi görülebilsin diye kısa bir düşünme
            # süresinden sonra plan parça parça verilir (hiz=0 iken beklemesiz).
            await self._sleep(0.8)
            return await self._chunked("```json\n" + json.dumps(plan, ensure_ascii=False, indent=2) + "\n```", on_chunk)
        if "<<ROL:DENETCI>>" in head:
            return '{"verdict": "approve", "feedback": "Uygun."}'
        if "<<ROL:DANISMAN>>" in head:
            await self._sleep(0.4)
            return await self._chunked(f"[{self.name}] danışman görüşü.", on_chunk)
        if "<<ROL:SENTEZ>>" in head:
            return await self._chunked("# Sonuç\n\nSahte sentez raporu.", on_chunk)
        if "<<ROL:ISCI>>" in head:
            await self._sleep(0.4)
            if self.name == n[0] and "DANIŞMA CEVAPLARI" not in prompt:
                text = f'Taslak hazır.\n<<SOR ajan="{n[1 % len(n)]}">>Bu yaklaşım hakkında ne düşünüyorsun?<</SOR>>'
            else:
                text = f"[{self.name}] görev çıktısı."
            return await self._chunked(text, on_chunk)
        return f"[{self.name}] bilinmeyen rol."
