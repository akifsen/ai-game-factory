# V0.2 closeout kanıt indeksi

## Helper düzeltmesi CI kapanışı — 2026-09-27

**Verification Closure: CLOSED.** Test edilen revizyon
`fef168cdd19af5251548eae0e3dd922497be71e1`, run `36290153250`.
Ubuntu 3.11/3.12 **277 passed, 6 skipped**; Windows 3.11/3.12 **280 passed, 3 skipped**.
Linux Godot kabulü 43 komut, temiz wheel/CLI ve ext4 mount geçti. Recovery **PASSED**
(venv `bin/python`, fault helper exit 91, paket site-packages). Linux canlı-child
**PASSED** ve tamamlandı (8.105 s, Godot pid 2811). Linux symlink venv regresyonu
iki Ubuntu işinde de geçti, skip edilmedi. Rapor ekleri bu SHA'dan sonra yazıldı.

| Yeni kalıcı kanıt | Kapsam |
|---|---|
| [remote-ci/36290153250/verification-summary.json](remote-ci/36290153250/verification-summary.json) | SHA/ref/CI kaynak eşleşmesi, job kimlikleri, regresyon ve recovery/canlı-child sonucu, artifact digest |
| [remote-ci/36290153250/](remote-ci/36290153250/) | Beş job logu, run/jobs/ref metadata, üç ZIP ve çıkarılmış kabul/recovery/canlı-child dosyaları |
| [ci-catalog-36290153250.json](ci-catalog-36290153250.json) | Mevcut verifier için açık dosya listesi; yeni kanıt sistemi değildir |
| [evidence-package-ci-36290153250/](evidence-package-ci-36290153250/) | Taşınabilir paket; yeni CI dosyaları hash kontrolü ve eski seçili Windows graph semantik kontrolü |
| [ci-cold-verification-36290153250.json](ci-cold-verification-36290153250.json) | Checkout dışı `python -I` soğuk kontrol kaydı |

Önceki başarısız koşu silinmedi ve bu kapanışın kanıtı sayılmadı.

## Güncel final CI incelemesi — 2026-09-27 (tarihsel PARTIAL)

**Verification Closure: PARTIAL.** SHA `93ded412242b2bebf0d90ab90225b5826b852489`,
run `36289019065`: Ubuntu 3.11/3.12 **260 passed, 6 skipped**; Windows 3.11/3.12
**263 passed, 3 skipped**. Önceki dört Linux hatası geçti. Linux Godot kabulü
43 komut, temiz wheel/CLI ve ext4 mount kanıtı geçti. Recovery helper venv Python
symlink çözümü nedeniyle başarısız; Linux canlı-child **NOT RUN**.

| Yeni kalıcı kanıt | Kapsam |
|---|---|
| [remote-ci/36289019065/verification-summary.json](remote-ci/36289019065/verification-summary.json) | SHA/ref/CI kaynak eşleşmesi, job kimlikleri, test/skip satırları, artifact digest kontrolleri |
| [remote-ci/36289019065/](remote-ci/36289019065/) | Beş tam job logu (`.txt`), run/jobs/ref metadata, üç özgün ZIP ve çıkarılmış environment/mount/kabul/recovery dosyaları |
| [ci-catalog-36289019065.json](ci-catalog-36289019065.json) | Mevcut verifier için açık dosya listesi; yeni kanıt sistemi değildir |
| [evidence-package-ci-36289019065/](evidence-package-ci-36289019065/) | Bağımsız taşınabilir kompakt ek paket; yeni CI dosyaları hash kontrolü ve eski seçili Windows graph'ı semantik kontrolü |
| [ci-cold-verification-36289019065.json](ci-cold-verification-36289019065.json) | Checkout dışı `python -I`: PASS, yeni kabul eksikliği FAIL, yeni log tahrifi FAIL, geri yükleme PASS |

Paket içindeki `final-ci-report.md` CI incelemesi anının kopyasıdır; sonradan yazılan
yerel düzeltme/test notları ana raporda ayrıca belirtilir. Eski kaynak kimliği yalnız
eski Windows graph bağlamıdır; yeni CI SHA'sı kendi `remote-ci/36289019065/` metadata'sındadır.
Linux canlı-child kanıtı yoktur. Linux ham log/artifact setinin tümü yeniden
üretilmiş veya kopyalanmış sayılmaz. Gömülü JSON ve CI logları Temp'ten bağımsızdır.

```text
python -I evidence-package-ci-36289019065/verify_closeout_evidence.py verify evidence-package-ci-36289019065
```

## Önceki kapanış teslimi (tarihsel)

Aşağıdaki “remote henüz çalışmadı” açıklamaları önceki teslim anını anlatır;
güncel sonuç yukarıdadır. Bu dizin yeni closeout koşularını içerir. Üst dizindeki V0.1/V0.2 raporları tarihsel
olarak korunmuştur; yeni sonuçlar eski koşulara aitmiş gibi sunulmaz.

| Kanıt | Kapsam |
|---|---|
| baseline-source.json, baseline.json, baseline-tests.xml | Değişiklik öncesi dirty kaynak manifesti, 238 passed / 5 skipped / 1 deselected; statik kontroller |
| test-exclusions.json, symlink-probe.json | Tek tek skip/deselect kimlikleri ve gerçek WinError 1314 |
| commit-transition.json, product-commit-match.json | Kullanıcının çalışma sırasında oluşturduğu 8ad413b commit'i ile ürün kaynaklarının eşleşmesi |
| installation.json, windows-commands.json | Temiz non-editable wheel, hash ve paket/kaynak byte eşitliği, Python/Godot kimliği |
| windows-runtime-commands.json | Gerçek kabul/recovery/V0.1 komutları, cwd, UTC aralıkları, exit ve çıktı |
| windows-acceptance.json | 43 komut, gerçek pozitif/negatif/mutation/timeout/approval ve bağımsız Godot |
| windows-recovery.json | Runtime sonrası iki ayrı kesinti/recovery durumu; canlı-child testi değildir |
| windows-v01.json, v01-comparison.md | 41 komutluk yeni kabul ve tarihsel 41 → 39 semantik farkı |
| artifact-export-map.json, raw-artifacts/ | 251 kayıtlı ham artifact'in DB hash/boyut doğrulamalı kopyası; canlı DB kopyalanmadı |
| remote-ci/ | 36287573467 run/job logları, 8ad413b SHA, Linux gerçek Godot raporu, indirilen arşivin SHA256 kontrolü |
| platform-access.json | İlk platform keşfi; daha sonra kullanıcı push'u ile remote-ci kanıtı bu ilk durumu genişletti |
| historical-hashes.json, historical-preservation.json | Önceden mevcut 24 rapor/kanıt dosyasının değişmediği kontrol |
| historical-evidence-assessment.json | Eski JSON'daki gömülü semantik kanıtın ve Temp bağımlı ham logların sınırı |
| windows-live-crash.json, windows-live-command.json, live-process-evidence/, lead-live-validation.json | Son Windows canlı kesintisi; 1 runtime, recovery süreçlerinden 0 subprocess, korunan UNCERTAIN attempt |
| final-quality.json, final-tests.xml, final-source.json | Son 259/5/2 test ve statik kontroller, test edilen kaynak kimliği |
| delivery-verification.json | Son taşınabilir paketin soğuk pozitif/negatif kontrolleri |
| cold-review.json | İlk gerçek paketin dış boş dizinde pozitif ve üç negatif kontrolü |

## Taşınabilir paket ve kapsam

`evidence-package/` tek başına taşınır. İçindeki `verify_closeout_evidence.py`
standart Python kütüphanesiyle çalışır; kurulu gamefactory, kaynak checkout, orijinal
Temp dizinleri, Godot ve ağ gerekmez. Manifest relative yolları, boyutları ve
SHA-256 değerlerini kontrol eder. İlk pozitif workflow'un gerçek inspect kaydı,
observation execution/scenario kimliği, execution/task ilişkisi, validation DAG
bağımlılığı ve iki ham raporun artifact hashleri ayrıca karşılaştırılır.
Diğer artifact'lar için hash/boyut kontrolü yapılır; bütün tarihsel workflow'ların
semantiğinin yeniden doğrulandığı iddia edilmez. Bu bir imza/attestation veya
oyunun yeniden çalıştırılması değildir.

```text
python -I evidence-package/verify_closeout_evidence.py verify evidence-package
```

Paket kaynağı `catalog.json` derleme girdisidir ve özgün yerel dosya yollarını
referans verir; cold doğrulama catalog'u kullanmaz. `selected-inspect.json`, gerçek
kabul raporundaki bir CLI stdout JSON kaydından türetilmiş yeni dosyadır; orijinal
artifact gibi etiketlenmez. `raw-artifacts` altındaki dosyalar değiştirilmemiş byte
kopyalarıdır. Binary, venv, cache, save verisi ve tüm scratch dizini taşınmaz.

Uzak CI raporu seçili JSON kanıtıdır; eski workflow `.gamefactory` gizli dosyalarını
artifact olarak yüklemediğinden Linux ham artifact setinin tamamı burada bulunmaz.
Yeni yerel workflow tanımı temiz wheel/hash, filesystem bağlamı ve canlı kesintiyi
hazırlar; bu tanımın varlığı başarılı remote koşu sayılmaz.


## Son yerel helper düzeltmesi

[final-ci-local-validation.json](final-ci-local-validation.json) ve
`final-ci-local-*.txt`: bağımsız lider testi **56 passed, 6 skipped, 1 deselected**;
lint/format/diff kontrolü geçti. Üç helper/test dosyasının hashleri kayıtlıdır.
Bu yerel kayıt run 36289019065'i kapsamaz. Aynı üç dosya daha sonra
`fef168cdd19af5251548eae0e3dd922497be71e1` ile remote'a geçti ve run
`36290153250` içinde test edildi; güncel sonuç yukarıdaki CLOSED kaydıdır.
