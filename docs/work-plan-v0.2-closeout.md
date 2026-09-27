# V0.2 verification closeout — adım adım iş planı

Kapsam: mevcut V0.2 doğrulamasını kapatmak; V0.3, yeni ürün özellikleri,
release/push ve sistem kurulumu kapsam dışı. Tarihsel raporlar korunur.
Başlangıç HEAD: `169a8571d8e9f36d6ffac0cafce2d6a575f98473`; çalışma ağacı
V0.2 kaynakları ve kanıtları nedeniyle dirty. HEAD tek başına kaynak kimliği değildir.

| Adım | Beklenen davranış | Mevcut kanıt / eksik | İşlem | Sonuç / kanıt |
|---|---|---|---|---|
| 1 | Başlangıç talimatları ve kullanıcı değişiklikleri korunur | V0.1/V0.2 raporları, ADR 0005 ve dirty tree görüldü | İlgili kaynakları ve tarihsel dosya hashlerini kaydet | Devam ediyor |
| 2 | Bugünkü baseline yeniden ölçülür | 238/5/1 yalnız tarihsel sonuç | Normal pytest, lint, format, iki platform type-check; kimlik ve skip listesi | Bekliyor |
| 3 | Windows gerçek motor ve symlink yeteneği ölçülür | Gerçek Godot 4.7.2 kayıtları var | İzole symlink probe, temiz wheel ve güncel pozitif/negatif kabul | Bekliyor |
| 4 | Gerçek Linux runtime doğrulanır | Type-check yeterli değil; yalnız durmuş docker-desktop WSL bulundu | Mevcut Docker erişimi ve CI kontrolü; yoksa açık dış engel | Bekliyor |
| 5 | Uzak CI tam test edilen revizyona bağlıdır | origin GitHub; yerel V0.2 commit edilmemiş | Workflow ve remote ref/run incele; push yapmadan eşleşmeyi değerlendir | Bekliyor |
| 6 | Godot canlıyken Factory zorla kesilir | Mevcut recovery yalnız runtime sonrası kesiliyor | Ayrı supervisor, handshake, canlılık, zorla kesinti, inspect/resume/retry, sahiplik ve sentinel | Bekliyor |
| 7 | Eski execution tekrarlanmaz, gate açılmaz | Güvenli UNCERTAIN sözleşmesi ADR 0005 | Gerçek launch sayısı, kalıcı kayıtlar, bounded cleanup ve history kontrolü | Bekliyor |
| 8 | Kanıt Temp'ten bağımsızdır | JSON kayıtları var; artifact erişimi incelenecek | Seçilmiş ham observation/validation/log/metadata, kaynak kimliği ve manifest paketle | Bekliyor |
| 9 | Soğuk doğrulama ilişkileri ve bütünlüğü denetler | Doğrulayıcı araştırılacak | Boş dizinde offline kontrol; eksik observation, değiştirilmiş log, execution karıştırma negatifleri | Bekliyor |
| 10 | V0.1 davranışları korunur | 41 ve 39 komut raporları mevcut | Semantik komut eşlemesi ve invariant tablosu; güncel kabul | Bekliyor |
| 11 | Son kod ve wheel kanıtı eşleşir | Baseline yeni kod kanıtı sayılamaz | Diff incelemesi, gerekli regresyonlar ve temiz non-editable kurulum | Bekliyor |
| 12 | Karar yalnız doğrulanmış kapsamı ifade eder | Linux/CI açık olabilir | v0.2-closeout-report.md ve paket indeksi; CLOSED/PARTIAL/BLOCKED | Bekliyor |

Her koşu: UTC zamanları, argv/cwd/exit, platform/Python, kaynak manifesti,
dirty state, Godot sürüm/hash ve kullanıldıysa wheel hash'i ile kaydedilir.
Gerçek kusur bulunursa yeniden üret → dar düzeltme → regresyon → yeniden kabul.
Eksik ortam/izin N/A veya minor olarak gizlenmez. Gerçek ücretli API çağrısı sıfır.
