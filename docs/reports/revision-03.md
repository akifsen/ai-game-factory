# Revizyon 03 — son ekip lideri incelemesi

Bu tarihsel bulgu kaydı son sürüm kararı değildir. Kapanış kanıtları
[tamamlanma raporunda](v0.1-completion-report.md) bulunur.

1. **MAJOR — Sağlayıcı ve görev uygulamaları workflow motoruna gömülüydü.**
   Fake sınıfını tanıyan observer bağlantısı kaldırıldı. Sağlayıcı portu iç katmana,
   yerleşik task action ve validator'ları açık kayıtlı ayrı modüle taşındı.
   Bağımsız mimari yeniden incelemesi bu bulguyu kapattı. Somut SQLite bileşimi
   V0.1 façade tercihi olarak ADR'de korunur.

2. **MAJOR — Config politikaları CLI motoruna aktarılmıyordu.**
   CLI composition tüm beş policy alanını `PolicyRule` içine aktarır. Görev türü
   repo-write sınıflandırması eklendi. Ayrı CLI süreçlerinde değiştirilen YAML ile
   düşük bütçe çağrıyı engeller; yazma onayı konsept artifact'ından önce durdurur.

3. **MAJOR — Migrations ve bütçe rezervasyonu yarışları.**
   Migration `executescript` örtük commit'i kaldırıldı, `BEGIN IMMEDIATE` geçmiş
   okumasından önce alınır. Hata DDL ve sürüm kaydını birlikte geri alır.
   Ayrı workflow'lar için bütçe read/reserve atomik writer transaction'ına alındı.
   Sonradan kaybedilen bütçe claim'i ayrıca BLOCKED durumunu kalıcı yazar.

4. **CRITICAL — Maliyet sınıfı düşürme ve METERED belirsizliğinin atlanması.**
   Etkin sınıf görev ve sağlayıcı beyanının yüksek olanıdır; METERED, PAID ve
   EXPENSIVE belirsiz çağrı korumasına dahildir. Sağlayıcıya özel olmayan çağrı
   günlüğü dispatch öncesinde kaydedilir. Onay hash'i sağlayıcı, görev türü,
   workflow, etkin maliyet, tahmin ve upstream artifact hash'lerini içerir.

5. **MAJOR — Bazı durum geçişlerinde audit bağlamı eksikti.**
   Repository update-status ve claim işlemleri önceki/yeni durum, zaman ve aktörü
   transaction içinde kaydeder; attempt başlangıcında execution referansı bulunur.
   Finalize kaydının da terminal durum ve execution kimliğini taşıması gerekir.

6. **CRITICAL — Ekran redaksiyonu kalıcı kayıt güvenliği sağlamıyordu.**
   CLI çıktısı maskelenirken repository'ler ham provider exception, evidence ve
   audit payload'larını SQLite'a yazabiliyordu. Düzeltme sözleşmesi: payload'lar
   persistence sınırında merkezi redaksiyondan geçmeli; kimlik/hash/sorgu anahtarları
   değişmemeli; secret içeren görev parametreleri sessizce değiştirilmek yerine
   kayıttan önce reddedilmeli. Regresyonlar yalnız konsola değil SQL satırlarına ve
   DB/WAL içeriğine bakmalıdır.

Son iki test turundan önce bulunmuş kusurların varlığı, eski başarılı test
çıktılarının tek başına teslim onayı sayılmamasının nedenidir. Nihai onay, son kodun
yeniden çalıştırılmış test/kabul/paket kanıtına dayanır.
