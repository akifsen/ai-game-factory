# V0.2 closeout kanıt indeksi

Bu dizin yeni closeout koşularını içerir. Üst dizindeki V0.1/V0.2 raporları tarihsel
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
