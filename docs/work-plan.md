# AI Game Factory — adım adım V0.1 iş planı

Başlangıç: 2026-09-27. İlk incelemede çalışma klasörü boş ve Git deposu değildi.
Kullanıcının dört kaynak metni değiştirilmeden [requirements](requirements/) altında korunur.
Bu planın kapsamı kaynakların 62, 92 ve 113. bölümlerindeki **Factory Core V0.1** sınırıdır.
Her gereksinim [izlenebilirlik tablosunda](requirements/traceability.md) ayrı satırla takip edilir.

## Çalışma ilkeleri

- Deterministik çekirdek, değiştirilebilir ve güvenilmeyen AI/araç uçları.
- Oyunlar Factory olmadan çalışır; Factory geliştirme zamanı aracıdır.
- Görev tamamlanması; çalıştırma kaydı, doğrulanmış kanıt ve gerekli onaylara bağlıdır.
- Ücretli API çağrısı yapılmaz. Ücret güvenliği yalnızca açıkça etiketli sahte sağlayıcıyla sınanır.
- Kullanıcı dosyaları korunur. Git geçmişi değiştirilmez. Bir gereksinim sessizce atlanmaz.
- Uygulayıcının raporu kabul kanıtı değildir; ekip lideri dosya incelemesi ve bağımsız çalıştırma yapar.

## Aşamalar ve kabul ölçütleri

| Adım | İş | Bağımlılık | Kabul kanıtı | Durum |
|---|---|---|---|---|
| 01 | Dört metni oku; mevcut dosyalar, Git ve ortamı incele | — | Kaynak kopyaları; boş klasör gözlemi; yerel araç yolları | Tamamlandı |
| 02 | 1–123 gereksinimini sınıflandır; V0.1 / sonraki aşama ayrımını kur | 01 | Her bölüm için izlenebilirlik satırı | Tamamlandı |
| 03 | Dil, kalıcılık, yürütme, adaptör kararlarını ADR ile kaydet | 01 | Alternatifleri ve sonuçları olan dört ADR | Tamamlandı |
| 04 | Kurulabilir paket, tek sürüm kaynağı ve kalite araçlarını kur | 03 | Temiz sanal ortam kurulumu; help/version | Tamamlandı |
| 05 | Tipli alan modeli, merkezi geçişler, DAG ve kalite kapıları | 03 | Geçerli/geçersiz geçişler; döngü/fan-in/fan-out testleri | Tamamlandı |
| 06 | SQLite şeması, sürümleme, ilişkiler, denetim ve deneme geçmişi | 05 | FK/bütünlük; yeniden açılma; kilit hatası testleri | Tamamlandı |
| 07 | Merkezi politika, onaylar, maliyet/bütçe ve idempotency | 05–06 | Politika matrisi; onaysız ücretli çağrı sayısı 0 | Tamamlandı |
| 08 | Dosya sınırları, artifact, hash, evidence ve kalite doğrulaması | 06 | Eksik/değiştirilmiş çıktı tamamlanmayı engeller | Tamamlandı |
| 09 | Güvenli süreç çalıştırma, timeout, minimal ortam ve redaksiyon | 07 | Argüman dizisi; stdout/stderr; timeout/secret testleri | Tamamlandı |
| 10 | Sahte sağlayıcılar, yetenek kaydı, gerçek Godot/Blender tespiti | 09 | Sürüm sorgusu; olmayan aracın temiz raporu | Tamamlandı |
| 11 | Keşif, tekrar güvenli init, katı sürümlü proje yapılandırması | 06 | Mevcut dosyalar korunur; hatalı yapılandırma reddedilir | Tamamlandı |
| 12 | Sıralı DAG yürütme, onayda durma, resume/retry ve çökme kurtarma | 05–11 | Ayrı süreçte devam; tarihçe korunur; belirsiz ücretli çağrı tekrarlanmaz | Tamamlandı |
| 13 | CLI, JSON, çıkış kodları, inceleme ve açıklayıcı hatalar | 12 | Gerçek kullanıcı komutları; parse edilebilir JSON | Tamamlandı |
| 14 | Küçük Godot fixture; başarılı/başarısız/ücretli sahte örnekler | 13 | Uçtan uca ayrı süreçlerde kabul çalıştırmaları | Tamamlandı |
| 15 | Bağımsız mimari/güvenlik/kurtarma/Windows incelemesi | 14 | Bulgu → düzeltme → regresyon testi | Tamamlandı |
| 16 | Tam test, format/lint/type; temiz kurulum ve belge gerçeklik kontrolü | 15 | Gerçekte çalıştırılmış komutlar ve sonuçları | Tamamlandı |
| 17 | 92. bölüm DoD kapanışı ve 119. bölüm tamamlanma raporu | 16 | Tek mühendislik kararı; sınırlamalar; kanıt yolları | Tamamlandı |

## Mimari uygulama sözleşmesi

Python 3.11+; küçük standart-kütüphane ağırlıklı CLI; yerel SQLite; sıralı yürütme.
Alan modeli CLI, SQLite ve motor/sağlayıcı bağımlılıklarını içeri almaz.
Adaptörler iç katmanın sözleşmelerini uygular; bileşim giriş noktasında yapılır.
İş akışı ve görev geçişleri tek otoriteden yürür. Onay kapısı adaptör iyi niyetine bırakılmaz.
Her deneme ayrı tutulur; başlamış fakat sonucu bilinmeyen ücretli işlem yeniden gönderilmez.
Çalışan süreç ile ölmüş süreç ayırt edilmeden kurtarma yapılmaz.

## Doğrulama sırası

1. Her uygulama diliminde hedefli test.
2. Uygulayıcı tamamladıktan sonra tüm değişen dosyaları inceleme.
3. Temiz kurulum ve gerçek `gamefactory --help`, `--version`, `doctor`.
4. Kontrollü oyun kopyasında init → status → demo → BLOCKED → süreç sonu.
5. Yeni süreçte onay kaydı → resume → evidence/gate → COMPLETED → inspect.
6. Başarısız sağlayıcı → downstream çalışmaz → retry → eski deneme korunur.
7. Sahte ücretli sağlayıcıda onaysız 0 çağrı → onaylı 1 → yeniden başlatmada hâlâ 1.
8. Eksik araç, hatalı config, timeout, dosya kaybı, onay reddi, çakışan çalıştırma.
9. Tam test ve statik kontroller; doküman iddialarının çalışan kodla karşılaştırılması.

## V0.1 sonrasına ayrılan kapsam

Gerçek Meshy/image/audio/vision servisleri, otomatik Blender işleme, Godot oyun test
harness'ı, AI gameplay değerlendirmesi, web arayüzü, dağıtık yürütme ve ek motorlar
uygulanmaz. Bunların sınırları ve gelecek sözleşmeleri belgelenir. Bu bir eksik
V0.1 işini erteleme izni değildir; 92. bölümün tüm uygulanabilir maddeleri kapanmalıdır.

## Ortam notu

Kullanıcının Godot dizini: `C:\Users\lenovo\devel\godot`.
Blender standart kurulumda ve PATH üzerinde tespit edildi.
Kabukta `python` / `py` yok; mutlak yol üzerinden `.venv` ve bağımsız doğrulama ortamı kuruldu.
Linux fiziksel doğrulaması bu Windows oturumunda yapılamaz; test edilebilir
taşınabilirlik ve uygun CI tanımı ayrı raporlanır.

## Kapanış kanıtı

17 adım V0.1 kapsamı için kapatıldı. [Tamamlanma raporu](reports/v0.1-completion-report.md), [kalite sonuçları](reports/quality.json), [kabul kaydı](reports/acceptance.json), [temiz kurulum](reports/installation.json). Linux fiziksel doğrulama ve üç Windows symlink testi sınırlamaları raporda korunur. V0.2 uygulanmadı.
