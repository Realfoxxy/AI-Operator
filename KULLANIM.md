# Orkestra Kullanım Kılavuzu

Orkestra; Claude, Gemini ve GitHub Copilot'u tek bir yazılım ekibi gibi çalıştıran bir terminaldir. Sen ne geliştirmek istediğini yazarsın. Claude işi planlar ve ekibe dağıtır, ajanlar kodu yazar ve birbirine danışır, Claude her işi denetler. Sonunda ne yapıldığını anlatan bir rapor alırsın.

Herkes **kendi** aboneliğini kullanır. Orkestra hiçbir şifre veya anahtar saklamaz; girişi her aracın resmi ekranı yapar.

---

## 1. Neye ihtiyacın var?

| Gereken | Neden | Nereden |
|---|---|---|
| Python 3.11 veya üstü | Orkestra'nın kendisi | [python.org](https://www.python.org/downloads/) (Windows'ta "Add to PATH" kutusunu işaretle) |
| Node.js LTS | Claude, Gemini ve Copilot CLI'ları Node ile çalışır | [nodejs.org](https://nodejs.org/) |
| Git (önerilir) | Kontrol noktaları ve geri alma | [git-scm.com](https://git-scm.com/) |
| Claude Pro veya Max | Planlayıcı ve denetçi (**zorunlu**) | claude.ai |
| Google AI Pro | Gemini ekip üyesi (isteğe bağlı) | gemini.google.com |
| GitHub Copilot | Copilot ekip üyesi (isteğe bağlı) | github.com/features/copilot |

Gemini veya Copilot aboneliğin yoksa sorun değil; `config.toml` içinde o ajanı `enabled = false` yaparsın ve ekip daha küçük olur.

---

## 2. Kurulum

### Seçenek A: İndir ve kur (önerilen)

```bash
git clone https://github.com/Realfoxxy/AI-Operator.git orkestra
```

```bash
cd orkestra
```

Git kullanmıyorsan GitHub'daki **Releases** sayfasından ZIP dosyasını indirip açabilirsin.

Ardından kurulum yardımcısını çalıştır. Python ve Node'u denetler, eksik CLI'ları kurmayı önerir (onay vermezsen hiçbir şey kurmaz) ve testleri çalıştırır.

Linux / macOS:

```bash
bash kur.sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File .\kur.ps1
```

Bu yolda Orkestra'yı **Windows'ta `.\orkestra.cmd`** ile başlatırsın. Bu başlatıcı doğru Python sürümünü (3.11+) kendisi bulur; bilgisayarında Visual Studio ile gelen eski bir Python olsa bile sorun çıkmaz. Mac ve Linux'ta komut `python3 -m orkestra` şeklindedir. Bu kılavuzda `python -m orkestra` geçen her yerde Windows'ta `.\orkestra.cmd` yaz.

### Seçenek B: Tek komutla (pipx)

```bash
pipx install git+https://github.com/Realfoxxy/AI-Operator.git
```

Bu yolda komut kısaca `orkestra` olur. Ayarlar ilk çalıştırmada `~/.config/orkestra/config.toml` (Windows'ta `%APPDATA%\Orkestra\config.toml`) dosyasına yazılır. Projeler varsayılan olarak `~/orkestra-calisma/` altında tutulur.

---

## 3. Hesaplarını bağla

Terminali aç:

```bash
python -m orkestra
```

Açılış ekranında hesapların durumunu görürsün:

```
  Hesaplar
    claude   planlayıcı  ● kurulu  → /baglan claude · /test claude
    gemini   ekip        ✖ kurulu değil  → /kur
    copilot  ekip        ✖ kurulu değil  → /kur
```

Sırasıyla:

1. **`/kur`**: Eksik CLI'ları npm ile kurar (`@anthropic-ai/claude-code`, `@google/gemini-cli`, `@github/copilot`).
2. **`/baglan hepsi`**: Her aracın **resmi giriş ekranını** sırayla açar ve ne yapacağını adım adım söyler:
   - **Claude:** `/login` yaz, tarayıcıda Claude hesabınla giriş yap, `/exit` ile çık.
   - **Gemini:** "Login with Google"ı seç, Google AI Pro hesabınla giriş yap, `/quit` ile çık.
   - **Copilot:** Klasör güven sorusunu onayla, `/login` yaz, kodu github.com/login/device adresine gir, `/exit` ile çık.
3. **`/test hepsi`**: Her ajana "yalnızca TAMAM yaz" gibi küçük bir istem gönderip bağlantıyı doğrular. Aboneliğinden çok küçük bir kullanım harcar.

Terminali açmadan bağlamak istersen `python -m orkestra hesap` komutunu kullan.

**Kimlik bilgileri nerede duruyor?** Her CLI kendi giriş bilgisini senin kullanıcı klasöründe tutar. Orkestra bunlara hiç dokunmaz. Aynı bilgisayarı birden fazla kişi kullanıyorsa her kişi kendi işletim sistemi kullanıcısıyla giriş yapmalıdır; böylece herkes kendi aboneliğini kullanır. Hesap değiştirmek için ilgili CLI'ı açıp kendi çıkış komutunu kullan (ör. Claude'da `/logout`), sonra `/baglan` ile yeniden bağlan.

---

## 4. İlk projen

```
orkestra[orkestra-calisma]> /proje not-uygulamasi
✔ Proje: /home/sen/orkestra-calisma/not-uygulamasi

orkestra[not-uygulamasi]> Python ve SQLite ile komut satırından çalışan bir not uygulaması yaz. Not ekleme, listeleme, arama ve silme olsun; testleri de yaz.
```

Bundan sonra şunlar olur:

1. **Plan:** Claude işi aşamalara ve görevlere böler, her görevi en uygun ajana atar. Plan ekrana gelir.
2. **Onay:** Dosya yazan veya komut çalıştıran her plan için sana sorulur:
   - `e`: onayla
   - `d`: planı editöründe aç ve düzenle (görevi başka ajana ata, talimatı değiştir…). Düzenlenen plan aynı güvenlik kurallarıyla yeniden doğrulanır.
   - `h`: reddet
3. **Çalışma:** Görevler paralel ilerler. `▶` görev başladı, `💬` ajanlar birbirine danışıyor, `✓`/`↺` Claude'un denetimi, `🧪` testler, `📝` değişen dosyalar.
4. **Aşamalar:** İlk aşama iskelet ve mimariyi kurar. Sonraki aşamalar projenin gerçek durumuna bakılarak planlanır.
5. **Rapor:** Sonunda ne yapıldığını, hangi dosyaların değiştiğini ve sonraki adımları anlatan rapor gelir.

### Çalışırken

- **Düz metin yaz:** Ekibe not olarak gider. Örnek: `Arayüz Türkçe olsun`.
- **`/not @copilot testlerde pytest kullan`:** Yalnızca o ajana not gönderir.
- **`/durum`, `/gorevler`, `/dosyalar`, `/kullanim`:** Anlık durum.
- **`/duraklat`, `/surdur`:** Yeni çağrıları bekletir veya devam ettirir.
- **`/durdur` (veya Ctrl+C):** Durdurur ve durumu kaydeder; `/devam` ile kaldığı yerden sürer.

### Sonraki gün aynı projeye devam

`/proje not-uygulamasi` yazıp yeni hedefini ver; örneğin `Etiketleme özelliği ekle`. Orkestra her projede bir **proje hafızası** tutar: hedefler, mimari kararlar, hangi dosyanın kim tarafından yazıldığı ve açık sorunlar. Ekip bu hafızayla kaldığı yerden devam eder.

---

## 5. Büyük projeler için ipuçları

1. **Testleri Orkestra'ya çalıştırt.** Planlayıcı testleri görevlere bağlar; başarısız olursa ajan otomatik olarak düzeltir. Ajanlara kabuk izni verilmez, testleri Orkestra çalıştırır.

   ```
   /dogrulama ekle pytest python -m pytest -q
   /dogrulama ekle web npm test --silent
   ```

   Kalıcı yapmak için `config.toml` içindeki `[[verify.commands]]` bölümüne ekle.
2. **Bütçeyi büyüt.** Büyük işlerde 60 çağrı yetmeyebilir: `/butce 150`, `/asama 10`. Bütçe biterse durum kaydedilir; `/devam` ile sürer.
3. **Sıkı mod:** `/sikimod ac` ile bir ajan dosya yazarken diğerleri bekler. Hangi değişikliği kimin yaptığı kesinleşir; bedeli biraz daha yavaş çalışmadır.
4. **Docker yalıtımı:** Güvenmediğin işlerde `config.toml` içinde `[verify] sandbox = "docker"` ayarla. Testler ağsız, yetkisiz bir kapsayıcıda çalışır.
5. **Devretme:** Bir ajanın kotası dolarsa görev otomatik olarak başka ajana geçer (`⇄`). `/devretme kapat` ile kapatılır.
6. **Oturum sürekliliği:** Claude, revizyonlarda önceki konuşmasını hatırlar (`--resume`). Bu, daha tutarlı düzeltmeler sağlar.
7. **Talimatları net ver.** Hangi dil, hangi kütüphane, hangi klasör yapısı istediğini yaz. Sonradan `/not` ile yönlendirebilirsin.

---

## 6. Güvenlik ağı: kontrol noktaları, fark, geri alma

Her çalıştırmanın başında ve her aşamanın sonunda projenin bir kopyası (**kontrol noktası**) alınır. Bu kopyalar git ile, senin kendi `.git` deponu hiç etkilemeden, proje klasörünün dışında tutulur.

```
/kontrol                 → kontrol noktalarını listele
/fark src/app.py         → dosyanın çalıştırma başından bu yana farkı
/geri-al 3               → projeyi 3 numaralı noktaya döndür
```

Geri almadan önce şu anki hal de kaydedilir; yani geri almayı da geri alabilirsin.

Orkestra ayrıca ajanların kendilerine yetki verebilecek dosyalara (`.claude/`, `.git/`, `CLAUDE.md` vb.) dokunmasını izler. Böyle bir değişiklik olursa çalışma durur ve sana sorulur (`⛔`).

---

## 7. Görsel çalışma alanı (isteğe bağlı)

Ajanların masalarını, canlı yazışlarını ve danışma oklarını tarayıcıda izlemek istersen:

```bash
python -m orkestra ui
```

Plan onayı, plan düzenleme, fark görüntüleyici, kontrol noktaları, tarayıcı bildirimleri ve İngilizce arayüz (EN düğmesi) burada da var. Sunucu yalnızca kendi bilgisayarından erişilebilir (`127.0.0.1`).

Abonelik harcamadan denemek için: `python -m orkestra --mock` (terminal) veya `python -m orkestra ui --mock` (tarayıcı).

---

## 8. Komut listesi

| Komut | Ne yapar |
|---|---|
| *(düz metin)* | Boştayken yeni hedef; çalışırken ekibe not |
| `/hesaplar` · `/kur` · `/baglan [ajan\|hepsi]` · `/test [ajan\|hepsi]` | Hesap durumu, kurulum, giriş, bağlantı testi |
| `/proje [ad\|yol]` · `/projeler` | Proje seç, oluştur, listele |
| `/mod tek\|asamali` · `/butce <n>` · `/asama <n>` | Çalışma ayarları |
| `/sikimod ac\|kapat` · `/devretme ac\|kapat` · `/otomatik ac\|kapat` | Sıkı mod, devretme, salt-okunur planları sormadan onaylama |
| `/dogrulama [ekle <ad> <komut…> \| sil <ad>]` | Test ve derleme komutları |
| `/durum` · `/gorevler` · `/dosyalar` · `/kullanim` | Anlık durum |
| `/not [@ajan] <metin>` · `/duraklat` · `/surdur` · `/durdur` | Çalışmaya müdahale |
| `/devam [no]` · `/gecmis` · `/rapor [no]` | Yarım kalanı sürdür, geçmiş, raporlar |
| `/kontrol` · `/fark <dosya>` · `/geri-al <no>` | Kontrol noktaları |
| `/ayrinti ac\|kapat` · `/temizle` · `/yardim` · `/cikis` | Diğer |

Komut satırı karşılıkları: `orkestra run "hedef"`, `orkestra run --asamali "hedef"`, `orkestra devam runs/<zaman>`, `orkestra ui`, `orkestra doctor`, `orkestra hesap`.

---

## 9. Sorun giderme

| Belirti | Çözüm |
|---|---|
| `'claude' programı bulunamadı` | `/kur` çalıştır; npm'in global klasörü PATH'te mi kontrol et (`npm prefix -g`). |
| npm `EACCES` hatası (Linux/macOS) | npm'in global klasörünü kullanıcı klasörüne taşı (npm belgelerindeki "global packages without sudo" adımları) veya Node'u nvm ile kur. |
| Bağlantı testi başarısız | `/baglan <ajan>` ile yeniden giriş yap. Hata "quota/limit" diyorsa aboneliğin kullanım sınırı dolmuştur; devretme açıksa görev başka ajana geçer. |
| Copilot hiç yanıt vermiyor | Copilot'un proje klasörüne güvenmesi gerekir: `/baglan copilot` ile o klasörde bir kez aç ve onayla. Her yeni proje için bir kez gerekir. |
| PowerShell "running scripts is disabled" (ör. `npm` yazınca) | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` çalıştır ve `E` de; veya `npm` yerine `npm.cmd` yaz. |
| `unsupported operand type(s) for \|` veya "Python 3.11 veya üstünü gerektirir" | Eski bir Python (ör. Visual Studio'nun 3.9'u) seçilmiş. `.\orkestra.cmd` kullan; o doğru sürümü bulur. Kurulu sürümler için: `py --list`. |
| `npm` tanınmıyor | Node.js kurulmamış: `winget install OpenJS.NodeJS.LTS` veya nodejs.org'dan LTS `.msi` dosyasını kur; PowerShell'i yeniden aç. |
| `git clone` "Permission denied" | Yazma izni olmayan bir klasördesin (ör. `C:\Windows\System32`). Önce `cd ~` yaz, PowerShell'i yönetici olarak açma. |
| Windows'ta "izin verilmeyen karakter" | `config.toml` içindeki sabit argümanlarda `" % ^ & \| < > !` karakterlerini kullanma. |
| "Çalışma klasörü ... içeremez" | Proje klasörü Orkestra'nın kendi klasörünün içinde veya ona eşit olamaz; `/proje <ad>` ile proje kökü altında bir klasör kullan. |
| Kontrol noktası alınamadı | Git kurulu değil veya proje çok büyük. Git'i kur; çok büyük klasörleri (`node_modules`) `.gitignore` ile dışarıda bırak. |
| Bütçe doldu | `/butce 150` ardından `/devam`. |

---

## 10. Gizlilik

- Orkestra hiçbir şifre, token veya API anahtarı okumaz ve saklamaz.
- Her çalıştırmanın tüm istem ve cevapları `runs/` klasöründe tutulur. Bu klasör yalnızca sana açıktır; gizli bilgi desenleri maskelenir. Yine de **`runs/` klasörünü asla paylaşma veya commit etme.**
- Web arayüzü yalnızca kendi bilgisayarından erişilebilir ve her açılışta yeni bir erişim anahtarı üretir.
