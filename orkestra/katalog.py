"""Eklenebilir yapay zekâ kataloğu (sürüm 0.4).

Kullanıcı isterse /ekle <ad> ile config.toml'a eklenir; hiçbiri zorunlu değildir. Her girişin ayar
bloğu sabittir (model çıktısından türetilmez) ve eklenmeden önce config doğrulamasından geçer.

durum:
  "hazır"    — Orkestra'nın zaten kullandığı araçla aynı yöntem (Claude Code) veya Orkestra'nın kendi kodu (Ollama).
  "deneysel" — bayraklar aracın belgelerinden/kaynaklardan alındı ama gerçek bir hesapla burada denenmedi.
               İlk kullanımda /test <ad> ile doğrula; çalışmazsa /cikar <ad>.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_CLAUDE_BASE = {
    "command": ["claude", "-p", "--output-format", "json", "--max-turns", "30"],
    "prompt_via": "stdin",
    "output": "json",
    "json_field": "result",
    "read_only_args": ["--allowedTools", "Read,Glob,Grep"],
    "write_args": ["--allowedTools", "Read,Glob,Grep,Edit,Write"],
    "timeout_s": 900,
}


@dataclass
class Entry:
    ad: str
    baslik: str
    saglayici: str
    ucret: str
    durum: str
    aciklama: str
    ayar: dict
    npm: str = ""                     # npm paketi (varsa /kur ile kurulur)
    kurulum: str = ""                 # npm yoksa elle kurulum talimatı
    giris: list[str] = field(default_factory=list)
    notlar: list[str] = field(default_factory=list)
    kaynak: str = ""


CATALOG: list[Entry] = [
    Entry(
        ad="claude-kodcu", baslik="Claude — 2. ekip üyesi (kodlama)", saglayici="Anthropic",
        ucret="Mevcut Claude Pro/Max aboneliğin (aynı kotayı paylaşır)", durum="hazır",
        aciklama="Ek bir Claude ekip üyesi; kodlama, hata ayıklama ve refactor görevleri alır. Ek giriş gerekmez.",
        ayar={**_CLAUDE_BASE, "display": "Claude (2. ekip üyesi: kodlama)", "color": "#b45309",
              "strengths": "özellik kodlama, hata ayıklama, refactor"},
        npm="@anthropic-ai/claude-code",
        notlar=["Tüm Claude üyeleri tek aboneliğini kullanır; kota daha hızlı biter."],
    ),
    Entry(
        ad="claude-testci", baslik="Claude — 3. ekip üyesi (test ve inceleme)", saglayici="Anthropic",
        ucret="Mevcut Claude Pro/Max aboneliğin (aynı kotayı paylaşır)", durum="hazır",
        aciklama="Ek bir Claude ekip üyesi; test yazma, kod inceleme ve güvenlik kontrolü görevleri alır.",
        ayar={**_CLAUDE_BASE, "display": "Claude (3. ekip üyesi: test ve inceleme)", "color": "#0f766e",
              "strengths": "test yazma, kod inceleme, güvenlik kontrolü"},
        npm="@anthropic-ai/claude-code",
        notlar=["Tüm Claude üyeleri tek aboneliğini kullanır; kota daha hızlı biter."],
    ),
    Entry(
        ad="codex", baslik="OpenAI Codex CLI", saglayici="OpenAI",
        ucret="ChatGPT Plus/Pro aboneliği (veya OpenAI API anahtarı)", durum="deneysel",
        aciklama="Claude'dan farklı bir model ailesi; güçlü kod yazarı ve 'ikinci görüş'. Ekibe en çok çeşitlilik katan seçenek.",
        ayar={"display": "OpenAI Codex (Codex CLI)", "color": "#10a37f",
              "command": ["codex", "exec", "--skip-git-repo-check"], "prompt_via": "stdin", "output": "text",
              # '-' istemin stdin'den okunacağını söyler; sandbox modundan sonra gelmelidir.
              "read_only_args": ["--sandbox", "read-only", "-"],
              "write_args": ["--sandbox", "workspace-write", "-"],
              "strengths": "kod yazma, hata ayıklama, refactor, alternatif çözüm ve ikinci görüş",
              "timeout_s": 900},
        npm="@openai/codex",
        giris=["Açılan Codex içinde 'Sign in with ChatGPT' seçeneğini seç.",
               "Tarayıcıda ChatGPT hesabınla giriş yap.", "Terminale dönünce Ctrl+C ile çık."],
        notlar=["codex exec, son yanıtı stdout'a, ilerlemeyi stderr'e yazar.",
                "Bayraklar sürüme göre değişebilir: sorun olursa 'codex exec --help' çıktısını paylaş."],
        kaynak="https://developers.openai.com/codex/cli/reference",
    ),
    Entry(
        ad="qwen", baslik="Qwen Code", saglayici="Alibaba (Qwen)",
        ucret="Qwen hesabıyla ücretsiz katman (koşullar değişebilir) veya API anahtarı", durum="deneysel",
        aciklama="Gemini CLI'dan türetilmiş açık kaynak kod ajanı. Ücretsiz katmanı olduğu için Gemini'nin yerine düşünülebilir.",
        ayar={"display": "Qwen Code", "color": "#6d28d9",
              "command": ["qwen", "-p", "Asagidaki gorevi uygula."], "prompt_via": "stdin", "output": "text",
              "read_only_args": ["--approval-mode", "plan"],
              "write_args": ["--approval-mode", "auto-edit"],
              "strengths": "kod yazma, uzun bağlam, belge ve test yazma", "timeout_s": 900},
        npm="@qwen-code/qwen-code",
        giris=["Açılan Qwen Code içinde giriş yöntemi olarak Qwen OAuth'u seç.",
               "Tarayıcıda giriş yap.", "Terminale dönünce /quit ile çık."],
        notlar=["Ücretsiz katmanın sınırları ve veri politikası değişebilir; hassas projelerde dikkatli ol."],
        kaynak="https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/",
    ),
    Entry(
        ad="cursor", baslik="Cursor CLI", saglayici="Cursor (Anysphere)",
        ucret="Cursor aboneliği", durum="deneysel",
        aciklama="Cursor aboneliğin varsa onun modellerini (Claude, GPT vb.) ekibe katar.",
        ayar={"display": "Cursor (Cursor CLI)", "color": "#111827",
              "command": ["cursor-agent", "--print", "--output-format", "json", "--trust"],
              "prompt_via": "file",
              "file_args": ["Read the file {path} in the current directory and carry out exactly the task described there."],
              "output": "json", "json_field": "result",
              # --force olmadan baskı modu değişiklik önerir ama uygulamaz (salt-okunur için uygun).
              "read_only_args": [], "write_args": ["--force"],
              "strengths": "kod yazma, düzenleme, çok dosyalı değişiklikler", "timeout_s": 900},
        kurulum="cursor.com/cli adresindeki resmi kurulum komutunu kullan.",
        giris=["Terminalde 'cursor-agent login' çalıştır ve tarayıcıda giriş yap."],
        notlar=["Yeni sürümlerde komutun adı 'agent' olabilir; öyleyse config.toml'da command'ın ilk elemanını değiştir.",
                "Yalnızca zaten Cursor kullanıyorsan mantıklı."],
        kaynak="https://cursor.com/docs/cli/reference/parameters",
    ),
    Entry(
        ad="ollama", baslik="Ollama (yerel model, ücretsiz)", saglayici="Bu bilgisayar",
        ucret="Ücretsiz; internet ve abonelik gerekmez", durum="hazır",
        aciklama="Model bu bilgisayarda çalışır; kod dışarı çıkmaz. Dosya yazamaz: danışma, özet ve araştırma görevleri alır, "
                 "diğer ajanların kotasını korur.",
        ayar={"display": "Ollama (yerel: qwen2.5-coder)", "color": "#64748b", "command": ["ollama"],
              "api": "ollama", "api_model": "qwen2.5-coder:7b", "api_url": "http://127.0.0.1:11434",
              "can_write": False,
              "strengths": "hızlı sorular, özetleme, belge metni, ikinci görüş (dosya yazamaz)", "timeout_s": 600},
        kurulum="ollama.com adresinden Ollama'yı kur, sonra terminalde: ollama pull qwen2.5-coder:7b",
        giris=["Giriş gerekmez. Ollama uygulamasının çalıştığından emin ol.",
               "Modeli indir: ollama pull qwen2.5-coder:7b (birkaç GB)."],
        notlar=["Kalite bilgisayarının gücüne bağlıdır ve büyük bulut modellerinden zayıftır.",
                "Başka model için config.toml'da api_model değerini değiştir (ör. llama3.1:8b)."],
        kaynak="https://ollama.com",
    ),
]


def get(ad: str) -> Entry | None:
    return next((e for e in CATALOG if e.ad == ad), None)
