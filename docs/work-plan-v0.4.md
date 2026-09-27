# V0.4 — Asset production uygulama ve doğrulama planı

Başlangıç: 2026-09-27. Hedef paket: **0.4.0**. Kaynak baseline:
`334b4fc368ab880d0352728c04548562fe5c6ffe`.

## Yetki ve teslim sınırı

Kullanıcının 57 bölümlük sprint şartnamesi bu planın kapsamıdır. Phase A geliştirme,
fake provider ve gerçek yerel Blender/Godot doğrulamalarını kapsar. Gerçek concept
insan incelemesinde durur. Concept onayı ücretli üretim onayı değildir. İnsan concept
onayı sonrası ayrı paid approval kaydı açılır; gerçek Meshy isteği açık onay ve ayrı
resume olmadan **gönderilmez**. Bu görevde gerçek ücretli çağrı hedefi **0**.
Phase B ve nihai görsel onay, bu bağımlılıklar gerçekleşmeden tamamlandı sayılamaz.

Workflow engine tek durum otoritesidir. Asset revision/artifact/operation kayıtları
ikinci bir workflow engine oluşturmaz. Raw, processed, validation, runtime ve review
kanıtları immutable ve hash ilişkili tutulur. Test actor insan değildir.

## Başlangıç bulguları

- HEAD istenen baseline ile aynı. Önceden var olan V0.3 completion report değişikliği
  ve `docs/reports/v0.3-rendered/ci-334b4fc/` kayıtları korunacak.
- V0.3 platform kapanmış, human visual review PENDING; tarihsel başarı bugünkü test değildir.
- Mevcut TaskHandlerRegistry, approval fingerprint, SQLite operation tracking,
  ProcessRunner, ArtifactManager ve Godot capture yaklaşımı yeniden kullanılacak.
- Blender 5.2.1 LTS ve Godot 4.7.2 gerçek version probe ile bulundu.
- Meshy mevcut adapter'ı boundary-only; credential presence değeri mevcut, değeri okunmaz/loglanmaz.
- ai-assets capabilities/health incelendi. MCP ortamında CUDA yok; ayrı CUDA Python
  mevcut. Worker durumu ve ortak storage ayrıca doğrulanmadan generation başlatılmaz.
- İlk baseline `ruff check .` / `format --check .` eski rapor betikleri nedeniyle başarısız;
  CI kapsamındaki `ruff check src tests`, `ruff format --check src tests`, mypy geçti.
  Orijinal sonuçlar `reports/v0.4/baseline/` altında korunur.

## Sıralı çalışma paketleri

Durumlar: TODO, IN_PROGRESS, VERIFIED, HUMAN_BLOCKED, ENV_BLOCKED. Kodun yazılmış
olması VERIFIED değildir; bağımsız diff ve çalıştırılmış doğrulama gerekir.

| Adım | İş ve çıktı | Şartname bölümleri | Kabul / kanıt | Durum |
|---|---|---|---|---|
| 01 | Repo/ADR/V0.3 kapanış incelemesi; baseline test/statik kontroller | 1,55 | Güncel pytest, Ruff, format, mypy ve engine baseline logları | VERIFIED |
| 02 | Asset domain, strict versioned spec, monoton revision; örnek energy crate | 2–5,27,36 | Strict schema, path/budget ve concurrent revision testleri geçti; wheel kontrolü adım 12 | VERIFIED |
| 03 | ImageGenerationProvider, gerçek concept ingest ve provenance | 6 | PNG tam decode ve hash-bound provenance testleri; gerçek SDXL concept mevcut | VERIFIED |
| 04 | Mevcut engine ile concept/paid/final gate DAG | 7–8,25–26,44 | Ayrı fingerprint; reject kalıcı; tamper eski onayı geçersiz kılar | VERIFIED |
| 05 | Meshy image-to-3d adapter + durable intent/query/recovery + cost | 9–11,28,30–31 | Onaysız 0; onaylı fake 1; resume/crash duplicate yok; UNKNOWN dürüst | VERIFIED |
| 06 | Blender background processing; raw korunur; scale/origin/LOD/collider | 12–17,32,35 | Gerçek script, version/input/script/output hash, süre/exit/timeout | VERIFIED |
| 07 | Executable GLB validator ve processing report | 18–19,34 | Parse/geometry/material/texture/bounds/origin/LOD/collider findings | VERIFIED |
| 08 | Staged Godot import, wrapper, runtime ve 2–3 açı capture | 20–23,33 | Gerçek mesh/physics/transform/bounds observation, attempt-bound PNG | VERIFIED |
| 09 | Offline comparison review, manifest, portable cold verifier | 24,39–40 | HTML her referansı bağlı; hash/size/revision/approval/runtime ilişkisi | VERIFIED |
| 10 | CLI dry-run/run/inspect/artifacts/report, mevcut approve/reject/resume | 29,37–38 | Dry-run 0 çağrı, doğru doctor availability ve tam resume komutu | VERIFIED |
| 11 | Negatif/failure/recovery/mutation kapsamı | 30,32–35,41–42 | Collider kaldırma ve scale×10; downstream/final gate bloklanır | VERIFIED |
| 12 | Windows/Linux offline acceptance, wheel ve CI | 48–51 | Fake provider + gerçek Blender/Godot; paid secret/çağrı yok | VERIFIED |
| 13 | Gerçek concept üret; insan concept review; sonra paid checkpoint | 43–47,56–57 | Concept path/hash, provider/op/cost/approval ID/resume; Meshy 0 | HUMAN_BLOCKED |
| 14 | Dokümantasyon ve bağımsız kapanış raporu | 52–55 | Kanıtlı implementation durumu, asset review ayrı; tüm açık maddeler | VERIFIED |

## Negatif test matrisi (atlama yapılmayacak)

- Spec unknown fields, negatif/sonlu olmayan ölçü, geçersiz bütçe/profile, absolute/traversal path, duplicate ID.
- Concept onayı yok / paid onay yok / budget exceeded: provider invocation=0.
- Fake onaylı generation=1; resume/completed resume=1; accepted-before-crash unknown ID=UNCERTAIN, yeniden submit yok.
- Bilinen provider ID query; FAILED provider sonrası Blender çalışmaz; reject sonrası regeneration yok.
- Bozuk raw GLB; generation sonrası raw tamper; validation sonrası processed tamper; approval sonrası artifact tamper.
- Blender unavailable/import/script/export/timeout failure; retry aynı raw ile, yeni paid request yok.
- LOD/collider eksikliği; triangle/material/texture limit aşımı; dış URI/absolute texture/missing texture.
- Godot import hatası/runtime collision yok; yanlış revision/attempt screenshot; unmanifested HTML reference.
- Gerçek test kopyasında collider silme: parse/mesh açık kalır, collider FAIL, runtime/final gate açılmaz.
- Scale×10 mutation: dimensions/bounds FAIL.

## Kapsam dışı

Rig, animasyon, skinning, environment/level/audio/video generation, visual AI scoring,
otomatik estetik onay, web UI, distributed/cloud orchestration, Unity/Unreal,
texture repaint, genel amaçlı Blender/repair agent. Destructive remesh/UV rewrite yok.

## Teslim kanıtları ve raporlama

`docs/integrations/meshy.md`, `docs/integrations/blender-asset-processing.md`,
`docs/pipelines/asset-production.md`, gerekirse tek asset-boundary ADR,
`docs/reports/v0.4-completion-report.md`, README. Her verification gerçek komut,
exit/result ve source kapsamıyla kaydedilir. Çalıştırılmamış Linux/CI veya insan
onayı başarı olarak sunulmaz. Commit/push/branch değişikliği bu kapsamda yapılmaz.

## Uygulama günlüğü

- 2026-09-27: Şartname okundu, başlangıç repo/ADR/workflow/provider sınırları incelendi.
  Baseline kontrolleri başlatıldı; plan oluşturuldu. Henüz V0.4 tamamlanmış değildir.
- Baseline Windows pytest: **302 passed, 13 skipped** (`pytest-workspace.xml`). İlk
  varsayılan temp dizini erişim hatası ayrı kayıtta tutuldu. Kaynak Ruff/format/mypy PASS.
- Gerçek V0.3 Godot headless: **PASSED**, 43 CLI/process komutu;
  `baseline/godot-headless-host.json`. Rendered capture + HUD mutation: **PASS**,
  `baseline/godot-rendered-host.json`. İlk sandbox root-certificate-store hatası
  korunur; host izinleriyle aynı kurallar geçti, ERROR filtreleri değiştirilmedi.
- Antigravity uygulaması başladı. Bağımsız safety review kontrol listesi
  `reports/v0.4/implementation-review-checklist.md` altında. Agent bildirimi kabul değildir.
- Yerel SDXL ilk concept işi `b64fa29d-5295-405e-9db9-cae053b95ed8` tek nesne yerine
  kolaj üretti; üretim referansı olarak seçilmedi. Prompt kesilmesi kaydedildi.
  Kısa promptla tek yerel düzeltme işi `e58caab2-5785-4ff0-ae9e-04099e05397e` başlatıldı.
  Meshy generation sayısı **0**; insan approval henüz yok.
- Antigravity ilk geniş atama 15 dakika bridge sınırına ulaştı; tamamlanma raporu yok,
  kabul edilmedi. Üretilen kod korundu ve güvenlik bulguları kaydedildi. Dar çekirdek /
  provider düzeltmesi Antigravity'ye; yerel GLB/Blender ve workflow/Godot/evidence işleri
  çakışmayan sahiplikle iki native worker'a ayrıldı. Team Lead bağımsız kabul yetkisidir.
- Linux ortamı gerçek tool probe ile hazır; proje testleri henüz yapılmadı. Ayrıntılar:
  `reports/v0.4/linux-environment.md`. Tasarım kararı ADR 0007 altında.
- İkinci bağımsız inceleme ve düzeltmeler `reports/v0.4/review-findings-round-2.md`
  altında. Son yerel çekirdek koşusu: **74 passed, 1 skipped**
  (`reports/v0.4/local-core-lead.xml`); gerçek Blender ve unequal-dimension eksen
  testi dahil. Symlink oluşturma izni olmayan Windows testinin atlandığı kaydedildi.
  Tam workflow, temiz wheel ve Linux son kaynak kabulü ayrıca yapılacak.

- Devam doğrulaması: Windows 468 passed / 16 skipped; bağımsız gerçek Godot 43 komut PASS. Windows ve Linux gerçek Blender/Godot asset acceptance PASS. Son iki Linux test başlatıcısının venv yolu düzeltmesi ve kapanış doğrulaması sürüyor. Kanıt: reports/v0.4/resume-verification.md.

- Nihai teknik karar: APPROVED. Linux test başlatıcılarındaki iki hata düzeltildi; gerçek Godot/crash-recovery dahil 51 hedefli test PASS. Windows hedefli doğrulama 41 PASS / 9 SKIP. Şartname kapsamındaki geliştirme ve offline doğrulama tamamlandı. Adım 13 HUMAN_BLOCKED: APP-9878c627 insan concept kararını bekliyor; gerçek Meshy çağrısı 0. Ayrıntılar: reports/v0.4-completion-report.md.
