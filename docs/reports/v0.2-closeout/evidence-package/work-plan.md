# V0.2 verification closeout — adım adım iş planı

Girdi: [kullanıcının eksiksiz kapanış sözleşmesi](requirements/v0.2-verification-closeout.md).

Kapsam: mevcut V0.2 doğrulamasını kapatmak; V0.3, yeni ürün özellikleri,
release/push ve sistem kurulumu kapsam dışı. Tarihsel raporlar korunur.
Başlangıç HEAD: `169a8571d8e9f36d6ffac0cafce2d6a575f98473`; çalışma ağacı
V0.2 kaynakları ve kanıtları nedeniyle dirty. HEAD tek başına kaynak kimliği değildir.

| Adım | Beklenen davranış | Mevcut kanıt / eksik | İşlem | Sonuç / kanıt |
|---|---|---|---|---|
| 1 | Başlangıç talimatları ve kullanıcı değişiklikleri korunur | V0.1/V0.2 raporları, ADR 0005 ve dirty tree görüldü | İlgili kaynakları ve tarihsel dosya hashlerini kaydet | Tamamlandı; historical-preservation.json (24 dosya), baseline-source.json |
| 2 | Bugünkü baseline yeniden ölçülür | 238/5/1 yalnız tarihsel sonuç | Normal pytest, lint, format, iki platform type-check; kimlik ve skip listesi | Tamamlandı; baseline.json, baseline-tests.xml: 238/5/1 |
| 3 | Windows gerçek motor ve symlink yeteneği ölçülür | Gerçek Godot 4.7.2 kayıtları var | İzole symlink probe, temiz wheel ve güncel pozitif/negatif kabul | Tamamlandı; installation.json, windows-acceptance.json: 43 komut; symlink-probe.json |
| 4 | Gerçek Linux runtime doğrulanır | Type-check yeterli değil; yalnız durmuş docker-desktop WSL bulundu | Mevcut Docker erişimi ve CI kontrolü; yoksa açık dış engel | KISMİ; remote-ci: Linux Godot geçti, suite dört test hatası; son Linux revizyonu açık |
| 5 | Uzak CI tam test edilen revizyona bağlıdır | origin GitHub; yerel V0.2 commit edilmemiş | Workflow ve remote ref/run incele; push yapmadan eşleşmeyi değerlendir | KISMİ; run 36287573467 / SHA 8ad413b gerçek; yerel düzeltmeler henüz remote koşuda yok |
| 6 | Godot canlıyken Factory zorla kesilir | Mevcut recovery yalnız runtime sonrası kesiliyor | Ayrı supervisor, handshake, canlılık, zorla kesinti, inspect/resume/retry, sahiplik ve sentinel | Windows tamamlandı; windows-live-crash.json; Linux açık |
| 7 | Eski execution tekrarlanmaz, gate açılmaz | Güvenli UNCERTAIN sözleşmesi ADR 0005 | Gerçek launch sayısı, kalıcı kayıtlar, bounded cleanup ve history kontrolü | Windows tamamlandı; lead-live-validation.json: 1 runtime, resume/retry 0 subprocess, sentinel canlı |
| 8 | Kanıt Temp'ten bağımsızdır | JSON kayıtları var; artifact erişimi incelenecek | Seçilmiş ham observation/validation/log/metadata, kaynak kimliği ve manifest paketle | Tamamlandı; artifact-export-map.json: 251 ham artifact, live-process-evidence/ |
| 9 | Soğuk doğrulama ilişkileri ve bütünlüğü denetler | Doğrulayıcı araştırılacak | Boş dizinde offline kontrol; eksik observation, değiştirilmiş log, execution karıştırma negatifleri | Tamamlandı; cold-review.json ve delivery-verification.json; 3 negatif kontrol |
| 10 | V0.1 davranışları korunur | 41 ve 39 komut raporları mevcut | Semantik komut eşlemesi ve invariant tablosu; güncel kabul | Tamamlandı; v01-comparison.md, windows-v01.json: 41 komut |
| 11 | Son kod ve wheel kanıtı eşleşir | Baseline yeni kod kanıtı sayılamaz | Diff incelemesi, gerekli regresyonlar ve temiz non-editable kurulum | Yerel tamamlandı; final-quality.json: 259/5/2, aynı wheel ürün hashleri eşit; remote açık |
| 12 | Karar yalnız doğrulanmış kapsamı ifade eder | Linux/CI açık olabilir | v0.2-closeout-report.md ve paket indeksi; CLOSED/PARTIAL/BLOCKED | Teslim edildi; reports/v0.2-closeout-report.md: APPROVED WITH COMMENTS / PARTIAL |

Her koşu: UTC zamanları, argv/cwd/exit, platform/Python, kaynak manifesti,
dirty state, Godot sürüm/hash ve kullanıldıysa wheel hash'i ile kaydedilir.
Gerçek kusur bulunursa yeniden üret → dar düzeltme → regresyon → yeniden kabul.
Eksik ortam/izin N/A veya minor olarak gizlenmez. Gerçek ücretli API çağrısı sıfır.


## Uygulama günlüğü

- Başlangıç normal suite: **238 passed, 5 skipped, 1 deselected**; Ruff lint/format ve
  iki mypy hedefi geçti. Kimlik/komutlar: `reports/v0.2-closeout/baseline*.json/xml`.
- Windows symlink probe: **WinError 1314**, dört test yetki nedeniyle skip;
  POSIX process-group testi platform N/A; gerçek Godot marker ayrı yürütüldü.
- Temiz wheel oluşturuldu, offline non-editable yeni venv'e kuruldu; `pip check`,
  checkout dışında CLI ve wheel kaynak byte karşılaştırması geçti.
- Gerçek Windows Godot kabulü **43 komut**, eski recovery **23 komut**,
  V0.1 kabulü **41 komut**, hepsi yeni run kimlikleriyle geçti.
- DB'de kayıtlı **242 artifact** hash/boyutuyla doğrulanıp byte olarak kalıcı
  `raw-artifacts/` içine aktarıldı; DB/WAL dosyaları kopyalanmadı.
- Tarihsel 41→39 farkı, koşullu iki Factory'siz Godot komutudur; ilk 39
  davranış adımı eşleşir. Tam tablo: `reports/v0.2-closeout/v01-comparison.md`.
- Kullanıcı çalışma sırasında V0.2'yi `8ad413b` ile commit/push yaptı; bu ajan
  Git geçmişini değiştirmedi. Ürün kaynaklarının başlangıç manifesti ve yeni
  commit ile eşitliği ayrı kaydedildi.
- Bu commit'in CI koşusu **36287573467** indirildi: Windows 3.11/3.12 **242/2**;
  Linux 3.11/3.12 **235 passed, 4 failed, 5 skipped**; ayrı Linux Godot **1 passed**.
  Dört hata test taşınabilirliğindedir: üç symlink cleanup, bir yanlış cleanup
  enjeksiyon noktası. Güvenlik assertion'ları korunarak iki test dosyası düzeltildi.
- Yeni workflow, temiz wheel hash'i, mount bağlamı ve canlı kesintiyi çalıştırmak
  üzere hazırlandı. Henüz remote'da koşulmadı; eski yeşil Godot sonucu bu yeni
  betikleri/test değişikliklerini doğrulamaz.
- Taşınabilir doğrulayıcı ilk ekip lideri incelemesinden sonra gerçek artifact
  şemalarına ve güvenli yol kontrolüne göre düzeltildi. Gerçek paketin soğuk
  pozitif kontrolü ve eksik observation / değişmiş log / başka execution
  negatifleri geçti (`cold-review.json`). Kapsam: ilk pozitif workflow semantiği,
  bütün seçili ham artifact'larda byte bütünlüğü; tarihsel tüm oyun koşuları değil.
- Canlı kesinti betiğinin ilk teslimi **CHANGES REQUIRED**: Windows HANDLE
  imzaları, recovery CLI launch gözlemi, PID sahipliği ve bounded cleanup
  eksikleri. Revizyon ve gerçek koşu tamamlanmadan bu madde kapatılmaz.


## Son kapanış durumu

- İlk canlı betik revizyonu tekrar incelendi. Windows HANDLE imzaları/sahipliği,
  ayrı CLI subprocess gözlemi ve bounded cleanup düzeltildi. Ekip lideri son
  POSIX belirsiz-cleanup ve platform kimliği düzeltmelerini yaptı; 4 regresyon eklendi.
- Son gerçek Windows koşusu `WF-GODOT-2cd66cd7` / `EXEC-05fc86d9`: Godot canlıyken
  Factory zorla kesildi; child sonlandı; sentinel tüm recovery boyunca yaşadı;
  iki resume ve bir retry toplam **0 yeni subprocess**; UNCERTAIN/BLOCKED korunur.
- Son kaynak normal suite **259 passed, 5 skipped, 2 deselected**; lint, format,
  Windows/Linux hedefli mypy geçti. Ürün kaynakları ve wheel byte'ları değişmedi.
- **APPROVED WITH COMMENTS / PARTIAL**: Windows yerel iş kapandı. Yeni revizyonun
  Linux normal suite/temiz wheel/mount/canlı kesinti ve uzak CI kanıtı açık.
  Kullanıcının normal yetkili commit/push akışı ve yeni CI sonucu gerekir;
  bu çalışma push, release veya V0.3 başlatmaz.

## Kaynak sözleşme kapsam eşlemesi

| İstek bölümü | Plan / kanıt |
|---|---|
| 1 Repository-first | 1; baseline-source, tarihsel raporlar/ADR, korunan dosya hashleri |
| 2 Baseline/kimlik | 2,11; baseline/final-source, installation, komut kayıtları |
| 3 Gerçek platform | 3,4; Windows koşuları, Linux gerçek CI ve açık kalan revizyon |
| 4 CI yetki/revizyon | 5; run/job/SHA kayıtları; kullanıcı commit geçişi; push yapılmadı |
| 5 Canlı kesinti sırası | 6; gerçek handshake, native kimlik, terminal yokluğu, zorla kesinti |
| 6 Güvenlik invariants | 7; launch journal, UNCERTAIN/gate/history, sentinel, cleanup |
| 7 Kalıcı teslim | 8; seçili 251 ham artifact + süreç sidecar'ları; DB backup gerekmedi |
| 8 Soğuk kontrol | 9; kopyalanmış standalone verifier ve üç gerçek negatif |
| 9 41→39 | 10; bütün davranış grupları + iki koşullu bağımsız Godot adımı |
| 10 Dar düzeltme | 7,11; yalnız test/helper/CI, ürün mimarisi ve sürüm değişmedi |
| 11 Son regresyon | 11; 259/5/2, statik kontroller, son canlı koşu, eşit ürün/wheel |
| 12 Kapanış ölçütleri | 12; PARTIAL, Linux ve doğru son revizyon CI açığı saklanmadı |
| 13 Final rapor | 12; istenen bütün başlıklarla v0.2-closeout-report.md |
| 14 Çalışma sırası | Günlük; baseline → hedefli araç → gerçek koşu → review → final kanıt |
