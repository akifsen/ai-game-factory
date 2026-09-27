# V0.4 — 57 maddelik gereksinim izlenebilirliği

Bu tablo şartnamenin her bölümünü uygulama veya açık insan checkpoint'i ile eşler.
Son kabul kararı ve sabit kaynak test sonuçları [kapanış raporundadır](../v0.4-completion-report.md).
`Fake` kanıt, gerçek Meshy üretimi veya gerçek asset için insan estetik onayı değildir.

| § | Gereksinim | Uygulama / kanıt |
|---|---|---|
| 1 | Başlangıç doğrulaması | `baseline/` pytest, Ruff, mypy, gerçek Godot headless/rendered; V0.3 kullanıcı değişiklikleri korundu |
| 2 | Kapsam | `static_prop` V0.4; ADR 0007; rig/animation/remesh/cloud kapsam dışı |
| 3 | İlk profil | `resources/specs/prop_energy_crate_01.yml`; katı profil/orientation/policy testleri |
| 4 | Asset domain | `core/domain/asset_contracts.py`, revision ve intent repository; task/execution/artifact modeline eşleme; engine authoritative |
| 5 | Specification | `asset-spec-0.4.0.schema.json`; `test_asset_contracts.py`: unknown/duplicate/NaN/bool/path/range/budget |
| 6 | Concept | ImageGenerationProvider/fake ve bounded PNG/provenance ingest; gerçek yerel SDXL işi, model/seed/hash `concept/` |
| 7 | Concept human approval | Ayrı mandatory `concept_review`, tamper fingerprint; gerçek `APP-9878c627` PENDING |
| 8 | Paid approval | Ayrı mandatory `paid_generation`, policy switch kapalıyken de zorunlu; cost/revision/provider/concept bağlı |
| 9 | Meshy provider | `adapters/external/meshy_cli.py`; pinned 0.4.0 v1 envelope, sadece image-to-3D |
| 10 | Paid güvenlik | Atomic intent ve unique task/fingerprint/asset-revision; known ID query, unknown ID UNCERTAIN; yarış testleri |
| 11 | Meshy sonuç doğrulama | Terminal status, tek GLB, byte/hash/path, bound decoded resources; raw immutable |
| 12 | Blender adapter | `BlenderAssetProcessor`, gerçek background process, packaged script ve version/hash raporu |
| 13 | Processing | Scale, bottom-center, applied transforms, LOD/collider/naming; gerçek Blender integration testleri |
| 14 | Cleanup sınırı | LOD0 destructive decimation yok; bütçe aşımı fail; raw dosya hash'i korunur |
| 15 | LOD | İsim/geometry/triangle-budget bağımsız kontrolü; LOD1 eksikliği negatif testi |
| 16 | Collider | Gerçek box vertices/triangles/envelope kontrolü; Godot convex collision ve physics ray hit |
| 17 | Material/texture | Tüm texture slotları, embedded PNG full decode, UV/material/dimension limitleri |
| 18 | Validator | `adapters/assets/glb_validator.py`; parse/accessor/ancestor transforms/bounds/origin/LOD/collider |
| 19 | Asset report | Processing + validation JSON, metrics/findings/hashes; offline HTML üzerinde özet |
| 20 | Godot import | Isolated managed scratch project, gerçek editor import, hata pattern/exit kontrolü; orijinal fixture korunur |
| 21 | Wrapper | `asset_runtime_harness.gd`: imported mesh, StaticBody3D, CollisionShape3D, revision/attempt observation |
| 22 | Runtime | Görünür mesh, finite dimensions/transform, collision ray, floor/neutral ışık/camera/1 m reference |
| 23 | Rendered evidence | Gerçek Godot front/three_quarter/side PNG; full decode ve attempt/revision/GLB binding |
| 24 | Concept/runtime review | Offline HTML dört sütun karşılaştırma; estetik karar otomatik verilmez |
| 25 | Final human review | Ayrı mandatory final_visual_review; concept/processed/validation/runtime/capture hashes bağlı |
| 26 | Rejection | Concept/paid/final rejection testleri; terminal revision, otomatik regeneration yok; yeni revision yeni gates |
| 27 | Revision | SQLite monoton/atomic allocation, immutable hashes, append-only runtime evidence; concurrent allocation testi |
| 28 | Cost ledger | Estimate/actual/UNKNOWN ayrı; reservation liability, known-ID delta ve actual overage regression testleri |
| 29 | Dry-run | CLI spec/concept/gates/steps/tool readiness; subprocess testinde tüm state tabloları değişmeden 0 çağrı |
| 30 | Provider mock | Missing/rejected/forged approval, budget, accepted response loss, known-ID query, concurrency, terminal failure |
| 31 | Gerçek Meshy kuralı | Gerçek ücretli çağrı **0**; free/local readiness gerçek generation kabulü sayılmaz |
| 32 | Blender recovery | Tool/process hata kontrolü, raw tamper bloklama, explicit PROCESS retry; ikinci generation yok |
| 33 | Godot recovery | Import/runtime structured failure; explicit GODOT retry; final gate ve generation sayısı regression testleri |
| 34 | Failure taxonomy | Asset-specific spec/approval/budget/provider/raw/DCC/validation/import/runtime/review structured codes; legacy uyumluluk |
| 35 | Security | PathGuard, symlink/junction/URI reddi, file/decode/time bounds, shell=False, structured secret redaction |
| 36 | Workspace | `.gamefactory/assets/<asset>/rNNN`, fresh attempt files, managed scratch ve review snapshots |
| 37 | CLI | asset-create, dry-run, inspect, artifacts, report, approve/reject, resume/retry; ayrı süreç testleri |
| 38 | Doctor | image.generate, asset.3d.generate, dcc.blender.process, engine.godot.import/rendered_capture; dürüst NOT_VERIFIED |
| 39 | Offline report | Tek HTML: spec, generation/process/validation, cost/approval/hash summary, concept ve üç capture |
| 40 | Evidence package | 17 manifest-bound files + standalone verifier; Python -I, dosya/hash/role/receipt/runtime/HTML ilişki kontrolü |
| 41 | Negatif testler | Spec/path/hash/approval/budget/provider/raw/processed/LOD/collider/texture/runtime/HTML mutation testleri |
| 42 | Mutation | Collider removal ve scale×10 gerçek validator handler'da FAIL; runtime/final açılmaz; cold bundle byte tamper reddi |
| 43 | Gerçek vertical slice | Gerçek SDXL concept mevcut; gerçek Meshy ve bunun downstream çıktıları insan/paid kapıları nedeniyle PENDING |
| 44 | Human checkpoints | Concept, paid ve final ayrı; test aktörleri production kararlarına dönüştürülmez |
| 45 | Phase A | Teknik offline pipeline doğrulaması + gerçek concept gate; paid checkpoint için gerçek concept insan kararı bekleniyor |
| 46 | Phase B | Kullanıcı paid operation'a ayrıca izin verene kadar çalıştırılmaz; fake full lifecycle ile altyapı sınandı |
| 47 | Real Meshy acceptance | NOT RUN: provider ID/billing/production GLB uydurulmadı; ücretli çağrı sayısı 0 |
| 48 | Real Blender acceptance | Windows ve Linux clean-wheel acceptance raporlarında gerçek executable/version/input/script/output hash/duration/exit |
| 49 | Real Godot acceptance | Her iki OS gerçek import/runtime/capture; fake-generated raw GLB açıkça etiketli |
| 50 | Windows/Linux | Aynı kaynak arşivi, ayrı wheel/env, bağımsız test/real tool evidence; platform skip nedenleri raporda |
| 51 | Package/CI | Packaged schemas/Blender/Godot/cold verifier; clean wheel; CI fake-only asset stage; remote CI koşusu iddia edilmez |
| 52 | Documentation | Plan, ADR, Meshy/Blender integration, pipeline/operator docs, README, bu traceability |
| 53 | Final report | `v0.4-completion-report.md`: technical status ile production human/paid status ayrı |
| 54 | Done | Teknik gate testleri ve bağımsız lead doğrulaması; insan bekleyen bölümler açıkça ayrı |
| 55 | Çalışma disiplini | Delegation → diff review → failing test → revision → bağımsız test; başarısız denemeler saklandı |
| 56 | Paid stop | Gerçek concept PENDING nedeniyle daha erken güvenli kapıda; ücretli onay/call yapılmadı; exact commands checkpoint belgesinde |
| 57 | Başla / sıra | Baseline → plan → domain → gates → provider → processing → runtime → cold evidence → clean packaging; V0.5 yok |

## Kanıt sınırları

- Kaynak kod ve otomatik doğrulamalar insanın görsel kararının yerine geçmez.
- Gerçek SDXL concept üretimi yerel GPU işiyle yapıldı. Fake fixture mavi kutusu bu concept'ten üretilmiş Meshy modeli değildir.
- Yerel Docker Linux çalışması gerçek Linux/Blender/Godot kanıtıdır; uzak GitHub Actions koşusu değildir.
- Tarihî/interim loglar son sabit kaynak kabulüyle karıştırılmamalıdır. Son rapor geçerli source archive hash'ini belirtir.
