# Revizyon 02 — workflow, onay ve veri bütünlüğü

Durum: **CHANGES REQUIRED**. İnceleme kapsamı `workflows/engine.py`,
`workflows/definitions.py`, persistence repository'leri ve config loader'dır.
Satır numaraları inceleme anındaki sürüme aittir. Bu rapor yürütme kilidi,
process timeout ve redaksiyon incelemesini tekrar etmez.

1. **CRITICAL — Onaydan sonra ilerleme kilitleniyor.** `_execute_dag` satır
   250–253, `BLOCKED` görevleri `in_progress` kümesine koyup DAG seçicisinden
   çıkarıyor. `get_ready_tasks` satır 70–79 bu görevleri daima atlıyor. Demo
   akışında ücretli görev onaylanıp `resume` çağrılsa bile `_execute_task` yeniden
   çağrılmıyor; workflow BLOCKED döner, provider çağrı sayısı sıfır kalır.
   Onay verilmiş, uzlaşma beklemeyen BLOCKED görevi yeniden hazır aday yapın;
   bekleyen/reddedilen onayı ve belirsiz ücretli denemeyi ayrıca ele alın.

2. **CRITICAL — Ücretli çağrının belirsiz sonucu güvenli retry ile korunmuyor.**
   `paid_generation` satır 523'te provider'a istek gönderir. Provider yanıt
   vermeden timeout/hata atarsa `_execute_task` satır 438–446 denemeyi doğrudan
   FAILED yapıyor. `retry_task` satır 595–625 FAILED görevini PENDING yapıp
   aynı ücretli isteği yeniden gönderebilir. `FakeAssetGenerationProvider`
   `simulate_crash=True` ile çağrıdan sonra exception üretir (fake_provider.py
   satır 49–66); bu yol somut bir tekrarlı çağrı senaryosudur. Çağrı sonrası
   doğrulanmamış sonucu UNCERTAIN/BLOCKED koruyun; idempotency anahtarı ve
   provider uzlaştırması olmadan tekrar göndermeyin. Hata yolunda biliniyorsa
   `external_op_id` kaybolmamalı.

3. **CRITICAL — Durable claim atomik değil.** `_execute_task` satır 390–405,
   görevi RUNNING olarak commit ettikten sonra ayrı transaction ile execution
   satırı açıyor. İki commit arasında süreç ölürse `RUNNING` ücretli görev için
   `get_latest_attempt` boş döner; `_execute_dag` satır 174–197 bu durumu
   kurtarmıyor. Ayrıca satır 410–417 execution COMPLETED ve task COMPLETED
   durumlarını ayrı commit ediyor; crash tutarsız terminal state bırakır.
   Görev claim'i + attempt oluşturmayı tek SQLite transaction içinde, koşullu
   state kontrolüyle yapın. Deneme sonucu, artifact/evidence/gate ve task terminal
   state ilişkisini de tutarlı şekilde commit edin; crash injection testleri
   her ara noktayı kapsamalı.

4. **CRITICAL — Kalite kapısı gerçekte doğrulamıyor.** `record_evidence` satır
   558–579 hiçbir artifact okumadan veya hash hesaplamadan `PASSED` kaydı
   oluşturuyor. `validate_artifact` satır 503–511 daha önce hash baksa bile
   sonraki görevler dosyayı değiştirebilir; ayrıca gate ve evidence kayıtları
   ayrı transaction'larda. Satır 409–417 dispatch dönüşünü koşulsuz başarı
   kabul ediyor. Gerçek önkoşul artifact'larını bitişte yeniden doğrulayın,
   başarısız/bekleyen gate varsa completion'ı engelleyin ve doğrulanan hash ile
   execution/evidence bağını saklayın. Dosya silme/değiştirme testi gerekir.

5. **CRITICAL — Provider beyanı yanlış başarıya dönüşebiliyor.**
   `GenerationResponse.status` tanımlı (external/base.py satır 18–24), fakat
   `paid_generation` satır 523–542 bunu kontrol etmiyor. Provider `status=FAILED`
   ve `output_path=None` döndürürse engine satır 528–531 sahte mesh dosyası
   üretip artifact kaydeder ve görevi tamamlar. Sahte dosya üretimini gerçek
   provider yolundan çıkarın; yalnız açık test fake'i kendi çıktısını üretmeli.
   Başarı statüsü, gerçek dosya, beklenen tür ve doğrulama kanıtı zorunlu olsun.

6. **MAJOR — Onay fingerprint'i yürütmede denetlenmiyor.**
   `_execute_task` satır 319–331 herhangi bir eski `APPROVED` satırına bakıyor;
   `operation_hash` ile güncel `task.parameters` eşitliğini kontrol etmiyor.
   `TaskRepository.save` satır 176–180 mevcut görevin parametrelerini
   değiştirebildiğinden, onaylanan düşük maliyet/prompt daha sonra değişse de
   provider çalıştırılabilir. `ApprovalService.approve` yalnız onay anında
   opsiyonel `current_inputs` verilirse hash bakıyor (satır 89–100).
   Dispatch öncesinde workflow/task/type/input hash'i tekrar karşılaştırın;
   değişiklikte yeni onay isteyin. Ret/changes_requested durumlarının sessizce
   yeni denemeyi yetkilendirmemesini test edin.

7. **MAJOR — Proje bütçesi/config uygulamaya bağlanmıyor.** Engine varsayılan
   `PolicyEngine()` kuruyor (engine.py satır 109). `PolicyRule` işlem üst limiti
   100 iken `PolicyConfig` 50 (policy_engine.py satır 34;
   config/schema.py `PolicyConfig`). `_execute_task` satır 325–331,
   `evaluate` fonksiyonuna `current_spent` vermiyor; varsayılan sıfırdır.
   Örnek: 10 adet onaylanmış 60 dolarlık işlem 500 dolarlık proje bütçesini
   aşabilir. Config limitlerini engine'e aktarın; geçmiş/rezervli maliyetleri
   güvenilir DB sorgusuyla toplayıp atomik bütçe claim'i yapın. Negatif,
   NaN/infinite tutarlar ayrı politika incelemesinde de raporlandı.

8. **MAJOR — `.gamefactory` junction'ı yönetilen dosyaları proje dışına
   yönlendirebilir.** `ConfigLoader.init_project` satır 97–102 mevcut
   `.gamefactory` yolunu çözmeden altında `state`, `locks`, `factory.yml`
   oluşturuyor. `Database.__init__` `db_path.resolve()` kullanıyor
   (database.py satır 15–17), dolayısıyla preexisting junction dış hedefte
   SQLite dosyası oluşturur. `.gamefactory` dizininin canonical yolunun proje
   altında kaldığını ilk yazmadan önce doğrulayın; `load_config` da aynı
   yönetilen alanı doğrulasın. Windows junction ve symlink testleri ekleyin.

9. **MAJOR — Retry yanlış workflow görevini değiştirebiliyor.** `retry_task`
   satır 597–613 yalnız task_id ve workflow_id'nin ayrı ayrı varlığını
   denetliyor; `task.workflow_id == workflow_id` şartı yok. `retry A <B'nin
   task'i>` B'deki FAILED görevi PENDING yapar, sonra A'yı çalıştırır. İlişkiyi
   herhangi bir mutation'dan önce doğrulayın ve iki workflow içeren test ekleyin.

10. **MAJOR — Artifact path'leri görevler arasında çakışıyor.**
    `inspect_project` ve `generate_concept` satır 474–497 tüm workflow'lar için
    aynı `reports/inspection.md`, `specs/concept.md` yollarına yazar. İkinci
    workflow ilk workflow'un saklanan SHA-256 hash'ini geçersiz kılar; paralel
    çalışma daha kötü sonuç verir. Artifact'ları workflow/task/attempt'e özgü
    güvenli namespace altında saklayın veya çakışmayı reddedin. İki workflow
    çalıştırıp ilkinin artifact'larını yeniden doğrulayan test ekleyin.

Mimari not: Uygulama koordinatörü `workflows/engine.py`, concrete fake provider
ve SQLite repository sınıflarını doğrudan import ediyor. Bu, ADR'deki port/adapter
bağımlılık yönünden sapma ve gerçek provider ekleme maliyeti oluşturur; mevcut
kritik davranışları düzeltirken dar arayüzlerle ayırmak uygun olur, geniş
yeniden yazım tek başına bu revizyonun kabul koşulu değildir.
