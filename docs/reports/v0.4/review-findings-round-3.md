# V0.4 bağımsız inceleme — üçüncü tur

Bu turda agent tamamlandı bildirimi kabul yerine kullanılmadı. Team Lead kaynak,
CLI çıktıları, gerçek Blender/Godot denemeleri ve hata testlerini inceledi.

## Giderilen bulgular

1. **Workflow kayıt kapsamı:** revision FK için oluşturulan boş placeholder ikinci
   register işleminde çakışıyordu. Yalnızca açık opt-in ile aynı proje/PENDING/boş
   workflow kabul edilir; normal duplicate register davranışı korunur.
2. **Artifact sahipliği:** concept/final/validation aşamaları önceki task'ın
   artifact'ını kendi çıktısı olarak döndürüyordu. Her stage kendi receipt/report
   artifact'ını kaydeder. Gerçek lifecycle bunu bağımsız olarak doğruladı.
3. **Approval evidence export:** final fingerprint yeniden kurulurken scope/cost
   ayrıntıları farklılaşabiliyordu. Export aynı engine canonical builder'ını ve
   approval'ın kaydedilmiş artifact kümesini kullanır. Pending final karar için de
   review export mümkündür; integrity PASS insan onayı anlamına gelmez.
4. **Godot runtime:** typed image erişimi ve JSON revision/attempt integer değerleri
   düzeltildi; gerçek üç capture ve physics ray hit Windows'ta doğrulandı.
5. **Known-ID recovery maliyeti:** UNCERTAIN query-only yolunda ikinci reservation
   engellendi. Bilinen actual cost reservation'ı aşarsa yalnızca ilave fark eklenir.
6. **Başarısız indirme/validation sonrası maliyet:** actual 15 / reserve 10 iken
   download veya raw preflight hatası eski akışta ledger'ı 10 bırakıyordu. Known
   billing dosya işlemlerinden önce uygulanır; provider exception yolunda durable
   intent actual_cost da okunur. Malformed raw, missing raw ve terminal exception
   regresyonlarının üçü de gerçek SQLite ledger'da 15'i doğrular.
7. **Fake revision kimliği:** her CLI instance'ının FAKE-OP-0001 üretmesi ikinci
   revision'da unique index ile çakışabiliyordu. Durable fake işlemleri workflow,
   task, revision ve fingerprint bağlı ayrı kimlik kullanır; legacy fake korunur.
8. **Pillow global state:** eski Godot PNG decoder'ın MAX_IMAGE_PIXELS ataması
   sonraki GLB texture doğrulamasını etkiliyordu. Header boyutu decode öncesinde
   yerel kontrol edilir, global değer değişmez. Art arda capture/GLB testi geçti.
9. **Kabul betiği Python yolu:** Linux venv symlink'ini resolve etmek base Python'a
   düşürebiliyordu. Invocation path korunur; mevcut gerçek-venv regression matrix
   asset acceptance betiğini de kapsar.
10. **Raw hata sınıfı:** structural raw GLB preflight, Blender ve valid-artifact
    kaydından önce çalışır; yanlış format RAW_ARTIFACT_INVALID olur.
11. **Şartname sunumu:** dry-run spec/concept/gates/steps/readiness ve sıfır state
    mutation içerir; offline HTML spec/metrics/findings/cost/actors/hashes gösterir
    ve fake evidence belirgin biçimde etiketlenir. Gerekli capability isimleri
    eski identifier'ları bozmadan eklendi.

## Saklanan başarısızlıklar

- İlk live Windows acceptance denemeleri registration, artifact scope, fingerprint
  ve observation tip hatalarında durdu; hiçbirisi başarı diye raporlanmadı.
- Linux Blender embedded Python'da NumPy eksikti. Disposable container'a Debian
  NumPy 2.2.4 kurulunca aynı 12 test geçti; CI dependency listesine de eklendi.
- Bir ara tam koşuda 0.5 saniyelik eski process startup testleri yoğunlukta zaman
  aşımına uğradı. Üç hedefli test tekrarında geçti; nihai tam koşu ayrıca yapılır.
- İlk frozen Windows pytest -I koşusu repository içindeki `tests` import'unu
  engelledi. Wheel module yolu -I ile ayrıca kanıtlandı; pytest PYTHONPATH boş,
  src-layout frozen checkout'tan normal modda çalışır. Cold verifier -I kullanır.
- İlk iki snapshot, raw taxonomy ve failure-billing düzeltmeleri için superseded
  edildi. Bu koşular kesildi ve final kabul sayılmadı. Nihai hash ve iki platform
  sonuçları kapanış raporundaki source-manifest/v04-source digest'ine bağlıdır.

Gerçek Meshy ücretli çağrı sayısı **0**. Production workflow gerçek concept insan
onayını bekler; fake/test approval production kararı değildir.
