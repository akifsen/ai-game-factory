# V0.1 dışındaki gereksinimlerin tasarım sınırları

Bu belge uygulanmış özellik listesi değildir. Kaynakların özellikle “eventually”,
“plan”, “future” dediği ve 62/113. bölümün üretim uygulamasını yasakladığı yetenekleri
korur. Sonraki aşama ayrıca yetkilendirilmeden bu servisler çağrılmaz.

## Factory bilgisi ve oyun bilgisi (§2, §40, §44–46)

V0.1'de küçük sürümlü proje sözleşmesi zorunludur. GAME_SPEC, GAME_VISION, CORE_LOOP,
ART_BIBLE, UX_SPEC, AUDIO_BIBLE, PROGRESSION belgeleri oyun ihtiyacına göre isteğe
bağlıdır. ASSET_MANIFEST, PERFORMANCE_BUDGET, FEATURE_MATRIX ve ACCEPTANCE_CRITERIA
ilgili pipeline devreye girdiğinde sürümlü yapılandırma olarak eklenir. Hiçbir belge
sırf klasör ağacını doldurmak için üretilmez.

Gelecek ContextBuilder sadece görev sözleşmesinde belirtilen kaynakları, proje kökü
içindeki izinli dosyaları ve ilgili geçmiş kanıtı seçer. Factory bilgisi depodaki
tekrar kullanılabilir kurallar; oyun bilgisi hedef oyundaki belgelerdir. Sağlayıcıya
gönderimden önce kaynak yolu, hash/sürüm, boyut ve gönderim amacı kaydedilir. Secret
dosyaları, tüm ortam değişkenleri veya tüm depo kendiliğinden bağlama alınmaz.
V0.1 dış sağlayıcı bağlam gönderimi yapmaz; bu sınırlama gizlilik sınırını basit tutar.

## Ajan ve Director (§8–10, §47–51)

İleride agent definition kimlik/rol/yetenek/izinli-yasak araç/girdi-çıktı şeması,
timeout/retry/maliyet/onay beklentisini taşır. Görev sözleşmesi version, id,
objective, capability, inputs, constraints, dependencies, acceptance ve required
evidence içerir. Üretici yalnızca bir sonuç önerisi döndürür: status, summary,
changes, artifacts, evidence, findings, recommended_next_actions, blocking_issues.
Dosya referansı ve iddialar orkestratör tarafından yeniden doğrulanır.

Director planlar, yetenek seçer ve revizyon önerir; workflow durumunu, onayları,
bütçeyi veya kalite sonucunu doğrudan değiştirmez. Promptlar rol, sözleşme, bağlam ve
çıktı şeması olarak ayrılır. Gerçek prompt kullanımı başladığında şablon sürümü
execution metadata içinde kaydedilir; şimdi sahte prompt arşivi oluşturulmaz.

Routing yetenek adına göre yapılır; ticari model isimleri adaptör yapılandırmasında
kalır. Sağlayıcı hatası ücretli yedeğe sessiz geçiş başlatamaz. İzinli fallback,
maliyet tahmini ve yeni onay kapsamı kontrol edilir; kullanılan fallback kayıtlıdır.

## Motor ve development-only harness (§13–14, §19)

V0.1 motor tespiti yapar. Gelecek harness yalnız test build veya isteğe bağlı editor
addon içinde yer alır. Release oyunu Factory çalışmadan açılmalıdır. Harness için
izinli komutlar: start_game/load_scene/start_level/spawn_entity/simulate_input/wait,
capture_screenshot/dump_state/collect_metrics/quit. Parametreler şema ve izinli
scene/entity listesiyle doğrulanır; arbitrary GDScript, Python veya shell kabul edilmez.

Taşıma yerel ve erişimi kısıtlı olmalı; uzak port varsayılan kapalıdır. Oturum tokenı,
istek kimliği, timeout, kaynak bütçesi, seed ve fixed-step ayarları gelecekteki harness
sözleşmesinin parçasıdır. Test çıktısı scenario version + engine version + seed +
state hash + log/screenshot references içerir. AI oyun değerlendirmesi bu kanıtın
üstünde tavsiye üretir; deterministik test sonucunu değiştiremez.

## Asset pipeline ve DCC (§12, §15–18, §55, §72)

Gelecek sıra: specification → art-direction validation → concept → visual advisory
review → concept approval → paid-operation approval → provider submission → external
operation ID persist/query → Blender process → deterministic validation → Godot
integration → in-game verification. Onay artifact hash ve input specification
sürümüne bağlanır; değişen konsept eski onayı devralmaz.

Provider sınırı operation request/result/query/idempotency capability etrafındadır.
Meshy'ye özgü payload/domain state çekirdeğe taşınmaz. Credential referansı ortamda
kalır; log/persistence içine secret yazılmaz. Gerçek sağlayıcı eklenmeden önce crash
window davranışı ve maliyet birimi açıkça test edilmelidir. Provider kabul edip yanıt
kaybolduğunda otomatik ikinci ücretli istek gönderilemez.

Blender deterministik argüman dizisiyle, gözden geçirilmiş script ve sınırlı geçici
çalışma alanında çalışır. Scale/orientation/origin/mesh cleanup/materials/textures,
LOD/collision/UV/export GLB ayrı iş türleri olabilir. V0.1 yalnız detection yapar;
geometriyi onarıyor veya production model üretiyor iddiası yoktur.

Validator çıktısı PASS/WARNING/FAIL + rule id + measured value + expected constraint
+ artifact hash + evidence references taşır. Triangle/texture/material/scale/orientation,
collider/LOD/type/size/broken references ayrı ölçülebilir kurallardır. Vision bulguları
ayrı advisory türüdür: composition, hierarchy, silhouette, contrast, style, HUD/safe
area/VFX/light/readability. Vision sonucu tek başına deterministik gate PASS değildir.

## Performans ve oyun QA (§19–20)

Proje/platform bazlı versioned bütçe; fps/frame-time/CPU/GPU/memory/draw-call/entity/
particle/texture ölçümlerine birim ve toplama yöntemi eklenir. Gelecek karşılaştırıcı
aynı platform ve birimde gözlenen dağılımları eşiklerle karşılaştırır; ölçülmeyen metrik
PASS sayılmaz. Tek screenshot performans kanıtı değildir. Impossible progression,
spawn/economy/reachable-state/win-loss değerlendirmesi için deterministik senaryo
kanıtı gerekir; V0.1 bu yetenekleri sağlamaz.

## Eklentiler, depolama ve yürütme (§24–25, §30, §52)

İç arayüz ve açık kayıt yeterlidir. Uzak plugin indirme, marketplace, dağıtık worker,
broker, bulut veritabanı veya event sourcing eklenmez. Gelecekte başka ArtifactStore
aynı metadata/hash sözleşmesini uygulayabilir. Remote storage gelmeden erişim kontrolü,
retention ve reconciliation gereksinimleri tekrar incelenir. Sıralı DAG'dan paralel
yürütmeye geçiş, claim/lease ve bütçe reservation yarışları test edilmeden yapılmaz.

## Ertelenen kararlar ve gerekçe

- Unity/Unreal: gerçek kullanım yok; sahte adaptör eklemek değer sağlamaz.
- Web dashboard: CLI kabul yolculuğu doğrulanmadan ikinci arayüz yok.
- Audio/video ve gerçek model routing: ücret/credential ve ürün kapsamı ayrı karar ister.
- Çok ajanlı özerklik: çekirdek güvenlik sözleşmesi ve bounded task yaklaşımı önce gelir.
- Release/export/signing: V0.1 zorunluluğu değil; build artifact ve insan release onayı
  sonraki iş akışının kapsamıdır. Mevcut APK/AAB üretimi yapıldığı iddia edilmez.
