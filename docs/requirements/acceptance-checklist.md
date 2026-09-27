# Bağımsız kabul kontrolü

Kaynak: 63–92 ve 99–120. bölümler. Sonuçlar `docs/reports/` altında tutulur.
Bu liste test sonucu değildir; her satır çalıştırma veya dosya kanıtıyla kapanır.

## Temel davranış

- [x] İzole kurulum; help/version; tek sürüm kaynağı 0.1.0.
- [x] Doctor gerçek config/storage/Godot/Blender/yetenek kontrolleri yapar.
- [x] Olmayan araç temiz UNAVAILABLE; bozuk yol MISCONFIGURED; secret çıkmaz.
- [x] Init mevcut Godot oyununu bulur, oyun dosyalarına dokunmaz; tekrar güvenlidir.
- [x] Status proje, motor, hedefler, iş akışı, onay, çalıştırma ve yetenekleri gösterir.
- [x] JSON stdout tek geçerli JSON değeridir; hata çıkışları anlamlıdır.
- [x] Gerçek ayrı CLI süreçlerinde onayda durma ve devam etme gerçekleşir.
- [x] Artifact dosyası oluşur; hash, producer, task/workflow, validation görünür.
- [x] Inspect çalıştırmaları, kanıtları ve kapıları anlaşılır bağlar.
- [x] Başarısız görevden sonra bağımlı görev çalışmaz; retry yeni deneme ekler.

## Güvenlik ve kurtarma

- [x] Onaysız ücretli sahte sağlayıcı çağrı sayısı 0.
- [x] Onaydan sonra çağrı 1; tekrar resume/restart sayıyı artırmaz.
- [x] Provider kabulü ile yerel sonuç kaydı arasındaki çökme kör tekrar üretmez.
- [x] Canlı çalışan iş başka süreç tarafından kurtarılmaz veya yeniden çağrılmaz.
- [x] Onay reddi ve bekleyen onay yeniden açıldığında korunur.
- [x] Görev girdileri/onay kapsamı bağlanır; eski onay farklı iş için kullanılmaz.
- [x] Eksik/değiştirilmiş artifact bağımsız doğrulamada engellenir.
- [x] Yanlış veya eksik AI sonuç zarfı kabul edilmez.
- [x] Policy read/write/process/free external/paid/destructive matrisini kapsar.
- [x] Unknown config alanı/sürümü, bozuk YAML, geçersiz bütçe açıklayıcı hata verir.
- [x] Traversal, mutlak yol, sürücü ve symlink/junction kaçışı engellenir.
- [x] Shell kapalı; argüman dizisi; stdout/stderr ayrı; timeout kayıtlıdır.
- [x] Secret/environment/authorization değerleri çıktıya ve state'e sızmaz.
- [x] SQLite locked/read-only/eksik dosya hatası teşhis edilebilir.
- [x] DB migration versiyonu, FK, benzersizlik ve deneme geçmişi doğrulanır.

## Mimari ve teslim

- [x] Domain → vendor, CLI veya SQLite bağımlılığı yok.
- [x] State geçişi ve policy tek otoritede; adapter doğrudan başarı ilan edemez.
- [x] DAG linear/fan-out/fan-in/independent/cycle/failure testleri vardır.
- [x] Fixture oyun Factory runtime bağımlılığı olmadan Godot ile açılabilir.
- [x] Dört ADR gerçek kararları ve alternatifleri açıklar.
- [x] README çalışan komutlarla eşleşir; sahteler ve gelecek özellikler açık etiketlidir.
- [x] Tam test, format, lint, type sonuçları gerçek komutlarla kaydedilir.
- [x] Windows çalıştırılır; Linux fiziksel doğrulama sınırlaması dürüstçe yazılır.
- [x] Temizlik incelemesi kullanıcı dosyalarını korur; secret/log/DB paketlenmez.
- [x] Her 1–123 satırı kanıt veya açık gelecek kapsam gerekçesiyle kapatılır.
- [x] Son rapor tek karar ve CRITICAL/MAJOR/MINOR açık bulguları içerir.

## Kabul sırasında kullanılacak senaryolar

**Başarı:** kontrollü oyun → init iki kez → demo oluştur → artifact/doğrulama →
BLOCKED → ilk süreç biter → approval inspect → açık test onayı → yeni süreçte
resume → yerel komut → kanıt → COMPLETED → tekrar resume hiçbir işi tekrarlamaz.

**Hata:** deterministik transient provider failure → FAILED task → downstream
PENDING → inspect failure evidence → retry → ayrı ikinci attempt → tamamlanma.

**Ücret:** yalnız fake provider → absent approval/0 invocation → açık test
approval → resume/1 invocation → restart/resume/1 invocation. Gerçek kredi yok.

**Çökme:** atomik execution claim sonrası kesinti; ücretli işlem sonucu bilinmiyorsa
reconciliation gerektirir. Yerel güvenli retry önceki denemeyi saklar.

**Bütünlük:** onay beklerken artifact değiştir/kaldır → resume completion engeli.
Tamamlandı görünümü tarihsel sonuç ile güncel dosya bütünlüğünü karıştırmamalıdır.

## Kapanış ve kanıt sınırları

[Son rapor](../reports/v0.1-completion-report.md), [quality.json](../reports/quality.json), [acceptance.json](../reports/acceptance.json), [installation.json](../reports/installation.json). Onaylanan maddeler V0.1 sınırındadır. Read-only/locked DB tanıları kontrollü exception enjeksiyonuyla sınandı; fiziksel ACL testi iddia edilmez. Windows junction testleri geçti; üç sembolik bağlantı testi yetki nedeniyle atlandı. Linux çalıştırılmadı.
