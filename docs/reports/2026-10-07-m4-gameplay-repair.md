# M4 ve sınırlı gameplay onarım döngüsü — yerel doğrulama

Tarih: 2026-10-07. Karar: **CHANGES REQUIRED**. M4 kapanışı ve Phase B ürün
kabulü sağlanmadı. Bu rapor mevcut kodu ve bağımsız yerel doğrulamayı kaydeder;
geçmiş raporları yeni kabul veya sağlayıcı yetkisi olarak kullanmaz.

## 1. Başlangıç durumu

| Bileşen | Bu makinede doğrulanan durum |
| --- | --- |
| Factory | `C:/Users/akifsen/devel_ext/ai-game-factory`, `main`, `310fa521cb49f1b143218b3e761589008ecb0961`, `0.8.0rc9` |
| Factory çalışma ağacı | İnceleme ve testler başlarken temiz; ürün kaynak kodu değiştirilmedi |
| Tide Bastion | `C:/Users/akifsen/devel_ext/tide-bastion`, `main`, `e33658c15f544b5eed45bf1bb134a98391281329`; oyun kökü `game/` |
| PR #22 | Açık, birleştirilmemiş; kaynak SHA `c10052399a8c58a9268c12072a16f80791297bfe`, hedef mevcut Factory HEAD |
| Python | Ayrı `.verification/m4-repair/venv`; Python 3.11.9; bildirilen `.[dev]` bağımlılıkları kuruldu |
| Godot | `C:/Users/akifsen/devel_ext/godot/Godot_v4.7.2-stable_win64_console.exe`, `4.7.2.stable.official.ed1daf0bf` |
| Blender | `C:/Program Files/Blender Foundation/Blender 5.2/blender.exe`, 5.2.1 LTS |
| Export | Müşteri kopyasında yalnız Android preset var; proje ve standart yerel Godot konumlarında export template bulunamadı |

Müşteri deposunda başlangıçta sekiz tracked değişiklik ve üç untracked dosya
vardı. Bunlar `docs/MESHY_DIAGNOSTIC_REPORT.md`, `docs/MESHY_PIPELINE.md`,
`game/export_presets.cfg`, beş `tools/asset_pipeline/` dosyası
(`meshy-cli.cjs` silinmiş), `docs/SESSION_CHECKPOINT.md` ve iki
`failure-classification` dosyasıdır. Son durum başlangıçla aynı; bunlara
dokunulmadı. Ayrıntı: `.verification/m4-repair/baseline-rechecked.json`.

PR #22'deki tarihsel raporun M1–M3 müşteri commit'leri bu yerel Git nesne
veritabanında bulunmuyor: `1cbf6bafdd255928f08770422230ca9f86ce314e`,
`ee2fb5a1ea7339d85fffe829faf3be0fda3bf87c`,
`b1fb5ecb824162f5271f0a7a99e0dab36978c23b`. Yerel `.gamefactory` durumu ve
M4'ün asıl istek/onay/işlem kanıtları da bulunamadı. Kullanıcı, başka konumdaki
kaydın evdeki bilgisayarda olabileceğini belirtti.

`docs/product-direction.md` içindeki eski geniş kapsam ve araç tercihleri,
bu görevdeki daha yeni sınırlı kapsam, önce M4 kapanışı, test ve ek model
çağrısı yetkisi talimatlarının yerine kullanılmadı. Bu uygulama aşamasında
yeni alt ajan veya dış AI süreci başlatılmadı.

## 2. Phase A — M4: BLOCKED

İlk sandık dosyası mevcut ve SHA-256 değeri tarihsel ilk pilotla aynı:
`57c71ed83686e9d0baf6bf8667f5ec8ab84cc6ad206bd1d1f03ba2c69f695b08`.
Bu, ikinci asset/M4 kapanışını kanıtlamaz. İlk pilotun
`WF-REUSE-230ee7ad` / `APP-5b2a9f71` kayıtları tarihsel kaynak referansıdır;
bu makinede canlı veritabanından yeniden doğrulanmış M4 onayları değildir.

PR #22'nin `docs/reports/2026-10-04-real-use-milestones.md` raporu M4 için
turkuaz çemberli meşe erzak fıçısını **önerilmiş istek** olarak anlatıyor ve
plan çağrısından önce durulduğunu bildiriyor. Asıl kapsam, source mode,
workflow/revision, hedef, kabul ve sağlayıcı geçmişi olmadan bu öneri mevcut
üretim yetkisi sayılmadı. Tarihsel sıfır gönderim/30 kredi tahmini, bugünkü
hesap veya geçmişin tamamı doğrulanmış anlamına gelmez.

İki somut Factory bağımlılığı yeniden üretildi:

- `saved_factory_manifests`: yazma/kabul gerektiren kayıtlı manifest,
  yanlış ilk yeniden kurma adayının Pydantic hatası nedeniyle yüklenemiyor.
  Değiştirilmiş hash ve bozuk spec regresyonları da aynı noktada başarısız.
- Godot code gate: `main` üzerindeki bağımsız `--check-only --script`
  çağrısı geçerli proje autoload'unu çözemiyor (`GateAutoload`).

Bu dört regresyon mevcut `main` üzerinde başarısız oldu. PR #22 kaynakları
branch değiştirmeden ayrı geçici dizine çıkarıldı; ilgili 21 kontrol geçti.
Düzeltmeler hâlâ mevcut uygulama baseline'ında yok. PR birleştirilmedi,
cherry-pick yapılmadı veya düzeltmeler yeniden yazılmadı.

Gerçek müşteri kaynaklarının birebir geçici kopyasında PR #22 code gate de
çalıştırıldı. Import exit 0; proje bağlamındaki kontrol
`Content load failed: manifest checksum mismatch` nedeniyle **FAIL**.
76 script sonucu bu ortak proje tanı hatası nedeniyle başarısız işaretlendi;
bu, 76 ayrı syntax hatası bulunduğu anlamına gelmez.

Salt okunur neden analizi:

- Manifestin beklediği içerik SHA:
  `67d374e7fc2e89774e5c8bdc672b686d32160550547f78ff9bb65fc741493ce3`.
- 37 JSON dosyasının mevcut byte'larından hesaplanan SHA:
  `7c9d1845873dccd927226b717442df0208022dfd37e1cfd6d764b4dae9f2b4de`.
- Yalnız bellekte CRLF → LF dönüşümü yapıldığında beklenen SHA tam eşleşiyor.
  Yerel `core.autocrlf=true`. Kaynak dosyalar veya manifest değiştirilmedi.
- Kontrol öncesi/sonrası kaynak manifest SHA aynı:
  `300ebfc84e7f47f1b3daebde6bd156bceea733955410da1ab0b980483b21b8ca`
  (Factory stager kapsamındaki 536 dosya, 110949019 byte).

Kanıtlar `.verification/m4-repair/tide-context-summary.json`,
`tide-context-gate-evidence.json`, `tide-content-checksum.json` içindedir.
Bu çalışma gerçek oyun dosyalarının kopyasında teknik kontroldür; gerçek
oynanış, M4 asset kurulumu, görsel kabul veya bağımsız build kanıtı değildir.

## 3. Phase B — Gameplay repair: BLOCKED, başlanmadı

M4 kabul edilip doğrulanmadan Phase B'ye geçilmemesi talimatı uygulandı.
Yeni onarım episode'u, corrective task veya sağlayıcı çağrısı oluşturulmadı.
Checkout checksum sorunu Phase B'de başarı göstermek amacıyla seçilmedi;
önce doğru müşteri baseline'ının kurtarılması gerekiyor. Tarihsel M1 text-scale
problemi de yeni bir müşteri hatası gibi sunulmadı.

## 4. Değişen dosyalar

- Bu rapor: durum, tekrarlanabilir doğrulama ve devam sırası.
- Git tarafından yok sayılan `.verification/m4-repair/`: izole Python ortamı,
  SHA'sı sabit PR kaynak/test kopyaları, log/JUnit ve salt okunur inceleme
  sonuçları. `verify_tide_project_context.py` yalnız geçici kopyada mevcut
  code gate'i çalıştıran doğrulama yardımcısıdır; ürün yeteneği değildir.

Doğrulama sırasında Factory ürün kaynakları, müşteri dosyaları,
workflow/onay veritabanı, bağımlılık tanımları ve Git geçmişi değiştirilmedi;
commit/push yapılmadı. Kullanıcının sonraki commit/push talimatı yalnız bu
raporun yayımlanmasını kapsar; yerel ham kanıtlar Git dışında kalır.

## 5. Doğrulama

Tüm göreli yollar aşağıdaki PowerShell komutlarında Factory köküne göredir.
Yerel sağlayıcı anahtarları test çocuk süreçlerinin ortamından çıkarıldı;
canlı AI sağlayıcısı çağrılmadı. Sandbox'ın yerel araç süreçlerini engellediği
kontroller izinli host çalıştırmasıyla yürütüldü.

```powershell
.\.venv\Scripts\python.exe -m venv .verification\m4-repair\venv
& .\.verification\m4-repair\venv\Scripts\python.exe -m pip install -e '.[dev]'
$py = '.\.verification\m4-repair\venv\Scripts\python.exe'
& $py -m pip check
& $py -m ruff check --no-cache src tests
& $py -m ruff format --check --no-cache src tests
& $py -m mypy --cache-dir .verification/m4-repair/mypy src/gamefactory
```

Sonuç: pip check, lint ve format PASS (363 dosya); mypy PASS (179 dosya).
Eksik `jsonschema` sorunu bildirilen development kurulumu ile giderildi.

Geniş yerel regresyon komutu, CI'nın normal matris kapsamıyla aynı marker'ı
kullanır. `candidate_slow` işaretli 428 installed-wheel/CI shard testi ayrıca
çalıştırılmadı; bunlar geçilmiş olarak sayılmaz.

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
Remove-Item Env:MESHY_API_KEY,Env:OPENAI_API_KEY,Env:GAMEFACTORY_TEST_GODOT,Env:GAMEFACTORY_TEST_BLENDER,Env:GAMEFACTORY_GODOT_BIN -ErrorAction SilentlyContinue
$testRoot = Join-Path $env:TEMP ('gf-m4-' + [guid]::NewGuid().ToString('N').Substring(0,8))
& $py -m pytest -q -ra -p no:cacheprovider -m 'not candidate_slow' --basetemp=$testRoot --maxfail=5 --junitxml=.verification/m4-repair/baseline-junit.xml
```

Geniş regresyon: **2177 passed, 72 skipped, 428 deselected, 2 warnings;
1588.29 s (26:28), exit 0.** 2677 collected, 2249 selected; JUnit'te sıfır
failure ve sıfır error. İki uyarı, geçersiz/mutasyona uğramış profil kullanan
`test_v08_candidate_contracts` negatif testlerinin Pydantic serialization
uyarılarıdır; ilgili testler geçti.

72 skip'in dağılımı: 32 gerçek Godot opt-in kontrolü bu geniş koşuda etkin
değildi; 2 Blender/contaminating-Python opt-in koşulu; 3 eksik tarihsel V0.4
golden workspace; 32 symlink/hard-link platform veya yetki koşulu; 3 yalnız
POSIX/Linux kontrolü. Godot'un bu bilgisayarda bulunmadığı iddia edilmiyor:
ilgili M4 araç testleri aşağıdaki ayrı, açık executable yollarıyla koşuldu.
Kapsam dışındaki bütün gerçek animation/review opt-in grupları çalıştırılmış
veya bütün 2677 test geçmiş olarak sunulmaz.

Gerçek araç kontrolleri:

```powershell
$env:GAMEFACTORY_TEST_GODOT = 'C:\Users\akifsen\devel_ext\godot\Godot_v4.7.2-stable_win64_console.exe'
$env:GAMEFACTORY_GODOT_BIN = $env:GAMEFACTORY_TEST_GODOT
$env:GAMEFACTORY_TEST_BLENDER = 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe'
$env:GAMEFACTORY_BLENDER_PATH = $env:GAMEFACTORY_TEST_BLENDER
$testRoot = Join-Path $env:TEMP ('gf-tools-' + [guid]::NewGuid().ToString('N').Substring(0,8))
& $py -m pytest -q -ra -p no:cacheprovider --basetemp=$testRoot --maxfail=2 tests/integration/test_gameplay_harness_real.py tests/integration/test_asset_installation_godot.py tests/integration/test_blender_processing.py --junitxml=.verification/m4-repair/real-tools-junit.xml
```

**10 passed, 1 skipped, 39.97 s.** Skip: Blender contaminated-Python kontrolü
için `GAMEFACTORY_TEST_CONTAMINATING_PYTHON_BIN` sağlanmadı. Gameplay ve asset
fixture başarıları gerçek M4 veya gerçek müşteri onarımının kabulü değildir.

PR #22'nin iki test dosyası immutable SHA'dan byte eşitliği korunarak
`.verification/m4-repair/pr22-regressions/` dizinine çıkarıldı; kaynak bilgileri
`provenance.json` içinde. Mevcut main import edilerek:

```powershell
$testRoot = Join-Path $env:TEMP ('gf-pr22-' + [guid]::NewGuid().ToString('N').Substring(0,8))
& $py -m pytest -q -ra -p no:cacheprovider --basetemp=$testRoot .verification/m4-repair/pr22-regressions/test_saved_factory_manifests.py .verification/m4-repair/pr22-regressions/test_gameplay_harness_pr22.py -k 'not observes_state_and_raw_metrics' --junitxml=.verification/m4-repair/pr22-vs-main-junit.xml
```

**4 failed, 3 passed, 1 deselected, 7.50 s.** Deselected testin asıl main sürümü
gerçek araç grubunda çalıştırıldı; taşınan testin kaynak-relative lookup'ı bu
dar karşılaştırmanın kapsamına alınmadı.

PR #22'yi ana ağaca uygulamadan qualification:

```powershell
git archive --format=zip --output=.verification/m4-repair/pr22-source.zip c10052399a8c58a9268c12072a16f80791297bfe src tests scripts examples schemas pyproject.toml README.md .github .gitignore
& $py -m zipfile -e .verification/m4-repair/pr22-source.zip .verification/m4-repair/pr22-source
$repo = (Get-Location).Path
$python = Join-Path $repo '.verification/m4-repair/venv/Scripts/python.exe'
$env:PYTHONPATH = "$repo/.verification/m4-repair/pr22-source/src;$repo/.verification/m4-repair/pr22-source"
Push-Location .verification/m4-repair/pr22-source
& $python -c "import gamefactory; print(gamefactory.__file__); assert 'pr22-source' in gamefactory.__file__"
$testRoot = Join-Path $env:TEMP ('gf-pr22-qualified-' + [guid]::NewGuid().ToString('N').Substring(0,8))
& $python -m pytest -q -ra -p no:cacheprovider --basetemp=$testRoot tests/unit/test_saved_factory_manifests.py tests/unit/test_godot_import_gate_parse_output.py tests/integration/test_gameplay_harness_real.py --junitxml="$repo/.verification/m4-repair/pr22-qualified-junit.xml"
Pop-Location
& $python .verification/m4-repair/verify_tide_project_context.py
Remove-Item Env:PYTHONPATH
```

**21 passed, 19.91 s**; ardından gerçek müşteri kopyası gate sonucu yukarıdaki
checksum nedeniyle **FAIL, exit 1**. Kaynak import yolunun PR kopyası olduğu
doğrulandı. Test toplamları aynı vakaları kısmen tekrarlar; toplanarak benzersiz
kapsam iddiası yapılmaz. Raw stdout/stderr `.verification/m4-repair/*.log`,
JUnit dosyaları aynı dizindedir.

Salt okunur remote kontrol: mevcut main SHA için
[37213340577](https://github.com/akifsen/ai-game-factory/actions/runs/37213340577)
başarılı; PR kaynak SHA için
[37412187994](https://github.com/akifsen/ai-game-factory/actions/runs/37412187994)
quick CI başarılı. Bu sonuçlar yerel müşteri durumunu doğrulamaz. Doğrulama
sırasında elle CI başlatılmadı. PR qualification yalnız belirtilen 21 yerel
test kapsamındadır.

## 6. Otonomi sınırı

Factory'nin mevcut gate kodu geçici kopyada import ve proje bağlamında script
kontrolünü gerçekleştirdi. Kontrolü Team Lead başlattı; gözlemden onarım
görevine otonom geçiş olmadı. Geçici `local-verification-process-*` kayıtları
yerel süreç niyet kayıtlarıdır, insan onayı veya canlı workflow kaydı değildir.
Staging bir işletim sistemi güvenlik sandbox'ı olarak sunulmaz.

## 7. Maliyet ve onaylar

Bu görevde yeni Factory Codex/plan, concept/image, Meshy veya başka canlı AI
işlemi: **0**. Kullanılmış/oluşturulmuş canlı insan onayı: **0**. Müşteri
uygulaması: **0**. Yerel testler ve paket kurulumu yapıldı. Aktif model
oturumunun veya geçmiş sağlayıcı işlemlerinin faturalandırması doğrulanmadı;
UNKNOWN maliyet sıfır olarak yazılmadı.

Standalone `plan` mevcut main'de doğrudan Codex adaptörüne gidiyor; genel
Factory'nin yetkilendirme/niyet/muhasebe yoluna bağlanmış değil. Bu açık
giderildi denmiyor ve bu yol çağrılmadı. Devamda önce mevcut yetkili
provider execution yolu kullanılmalı; yetki işlem, kapsam, istek ve bütçeye
bağlanmalı. Genel bir billing sistemi eklemek gerekmiyor. Yerel budget
reservation sağlayıcının harcama tavanı garantisi değildir.

## 8. Somut engeller ve adım adım devam sırası

1. **Asıl durumu kurtar.** Evdeki M1–M3 müşteri commit'leriyle eşleşen kaynak,
   M4'ün asıl talebi, `.gamefactory` state/config, bağlı kanıtlar ve mevcut
   provider/approval geçmişi ayrı bir recovery kopyasında alınmalı. Mevcut
   dirty checkout üzerine yazılmamalı. Çıkış ölçütü: baseline, kapsam, source
   mode, workflow/revision, hedef ve toplam önceki gönderim sayısı tutarlı.
2. **Çalışan yerel baseline'ı doğrula.** Kurtarılan kaynakla satır sonu/checksum
   uyuşmazlığını çöz; manifesti yeni yanlış byte'lara uydurarak kontrolü
   gevşetme. Gerekli müşteri yazımı varsa ayrı geçerli yetki gerekir.
   Çıkış ölçütü: içerik checksum'u eşleşir ve içerik yükleme kontrolü geçer.
3. **PR #22 bağımlılığını uygulama baseline'ına taşı.** Yetkili Git kararıyla
   mevcut düzeltmelerin bulunduğu baseline kullanılmalı; otomatik merge veya
   duplicate uygulama yapılmamalı. Çıkış ölçütü: resume/hash-tamper kontrolleri
   ve gerçek autoload bağlamı geçer; bu rapordaki başarısız main testleri çözülür.
4. **M4'ün tam yetkisini hazırla.** Kayıtlardan doğrulanan istek ve source
   mode için mevcut yetkili proposal/plan yolu, geçerli bütçe/işlem onayı ve
   kalıcı niyet kaydı kullanılmalı. M4 Meshy gerektiriyorsa geçmiş dahil en
   fazla bir yetkili gönderim; belirsiz sonuçta yeniden gönderim yok.
5. **M4'ü bitir ve kabul et.** Mevcut üretim/reuse, doğrulama ve insan kabulü
   sonrasında ayrı müşteri yazma onayıyla kur. Kabul/kurulum hash eşitliği,
   gerçek battle görünümü ve regresyonları doğrula. Uygun export preset ve
   Godot sürümüyle eşleşen template sağlandıktan sonra Factory'siz EXE/PCK
   başlat. Çıkış ölçütü: ikinci asset gerçek oyunda, kabul edilmiş ve bağımsız
   çalışan build içinde; o zamana kadar M4 DONE değil.
6. **Yalnız M4 kapandıktan sonra tek gerçek onarım episode'u.** Güncel
   baseline'da gerçek oyuncu hatası yeniden üret; senaryo, assertion/tolerans,
   regresyonlar ve izinli dosyaları hash'le sabitle. Gözlemden kalıcı corrective
   task'a geçişi mevcut workflow/proposal/candidate mekanizmalarıyla bağla.
   En fazla iki aday sayacı restart/resume'da korunmalı; passing baseline,
   eksik/stale yetki ve kapsam dışı değişiklik sıfır dispatch/uygulama ile durmalı.
7. **Aynı senaryoyla kanıtla ve teslim et.** Factory aynı değişmemiş senaryoyu
   her adayda ve onaylı uygulama sonrasında çalıştırmalı. Başarılı aday için
   insan yazma onayı, kontrollü apply, bağımsız build ve insan gameplay kabulü
   ayrı kalmalı. Mock sözleşme testleri gerçek AI teşhisi veya müşteri kabulü
   yerine sayılmamalı.

## 9. Şimdiki tek insan önkoşulu

Evdeki bilgisayardan **M4'ün asıl geçmişini ve eşleşen müşteri baseline'ını
içeren recovery kopyasını** mevcut çalışma ağacından ayrı bir dizine getirmek.
Buna `.gamefactory` durumu ve bağlı kanıtlar, M1–M3'ün yerel commit nesneleri
ve M4 kapsam kaydı dahil olmalı. API anahtarlarının paylaşılması gerekmiyor.
Kullanıcıdan aynı proje/Godot yollarını tekrar istemek veya bu kayıtlar
olmadan genel bir üretim onayı almak bu önkoşulu karşılamaz.

## Başka bilgisayarda devam notu

Kullanıcı kalan çalışmaya akşam başka bilgisayarda devam edecek. Bu rapor ve
devam sırası Git'e taşınır; ürün kodunda bu oturuma ait bir uygulama değişikliği
yoktur. Başka bir makineye taşınması gereken tamamlanmamış bir patch veya
çalışan test süreci bulunmuyor. Geniş test koşusu exit 0 ile tamamlandı.

1. Factory deposunda bu raporu içeren `main` güncellemesini alın ve mevcut
   branch/HEAD/yerel değişiklikleri yeniden kaydedin. Raporun test sonuçları
   **310fa521cb49f1b143218b3e761589008ecb0961** koduna aittir; sonraki kod
   değişiklikleri otomatik olarak bu sonuçlarla doğrulanmış sayılmaz.
2. Diğer bilgisayarın gerçek Tide Bastion, Godot ve Blender yollarını bulun.
   Buradaki `C:/Users/akifsen/...` yolları yalnız bu makinenin kayıtlarıdır.
   Raporun M1–M3 commit'lerini ve `.gamefactory`/bağlı kanıtları eski ortamda
   araştırın; bu makinenin daha eski müşteri HEAD'ine reset atmayın. Var olan
   müşteri değişikliklerini koruyun.
3. Asıl M4 talebini, workflow/revision varsa kimliklerini, source mode'u,
   geçmiş gönderimleri ve geçerli onayları birlikte doğrulayın. Yeni boş bir
   state veritabanı başlatmak veya tarihsel rapordan onay kaydı üretmek
   kurtarma sayılmaz. Raporun erzak fıçısı önerisi yeni üretim yetkisi değildir.
4. Ardından bölüm 8'deki sıradan devam edin. PR #22'nin belirtilen SHA'sı
   ayrı kaynakta sınandı; `main` ile birleştirilmedi. Diğer checkout'ta
   düzeltmelerin varlığını kontrol etmeden yeniden uygulamayın. CRLF/checksum
   bulgusu da bu makineye aittir; diğer makinede aynı sorun varsayılmamalı.

**Git dışında kalanlar:** `.verification/m4-repair/` altındaki Python ortamı,
ham log/JUnit dosyaları, PR test/kaynak kopyaları ve
`verify_tide_project_context.py`; TEMP altındaki müşteri proje kopyaları.
Bunlar push ile diğer bilgisayara gelmez. Yukarıdaki komutlar bu makinedeki
koşuyu ve yeniden üretim yöntemini kaydeder; geçici yardımcı dosyalar yeni
checkout'ta bulunmuş sayılmamalı. Gerekirse aynı sabit kaynak SHA'larından
yeniden oluşturulmalı veya bu makineden ayrıca aktarılmalıdır. Kanıtların
olmadığı yerde mevcut rapor, canlı workflow/insan kabul kaydının yerine geçmez.

Yeni provider çağrısı, müşteri yazımı veya insan onayı bu devirle
yetkilendirilmez. İlk devam işi M4 geçmişini doğrulamaktır; Phase B hâlâ
M4'ün gerçek kapanışına bağlıdır.

## Açık kabul yanıtları

- **M4 gerçekten kapandı mı?** Hayır; BLOCKED.
- **Gerçek runtime bulgusu Factory içinde corrective task oldu mu?** Hayır.
- **Factory düzeltmeyi değişmemiş senaryoyla doğruladı mı?** Hayır.
- **Gerçek müşteri projesi yalnız geçerli onayla mı değiştirildi?** Bu görevde
  müşteri projesi hiç değiştirilmedi; onaylı apply gerçekleştirilmedi.
- **Kabul edilmiş standalone build bağımsız çalışıyor mu?** Bu görevde
  doğrulanmadı; tarihsel M3 raporu mevcut yerel build kanıtı değildir.
- **Ne doğrulanmadı?** M4'ün asıl kapsamı/tam işlem geçmişi, insan kabulü,
  ikinci asset üretim/kurulum/runtime/build zinciri ve gerçek bounded repair
  episode'u. Yeni ürün yeteneği IMPLEMENTED veya APPROVED olarak sunulmaz.
