# AI Game Factory belgeleri

Bu dizin Factory Core V0.1 ile sonraki Godot ve asset sprintlerinin planlarını,
mimari kararlarını ve ayrı doğrulama kayıtlarını içerir. Her sürümün güncel kapsamı
ve kabul durumu kendi kapanış raporunda belirtilir; tarihsel raporlar korunur.

## Plan ve kapsam

- [Adım adım iş planı](work-plan.md)
- [123 maddelik gereksinim izlenebilirliği](requirements/traceability.md)
- [Bağımsız kabul kontrol listesi](requirements/acceptance-checklist.md)
- [Gelecek aşamaların tasarım sınırları](requirements/future-boundaries.md)
- [Yerel doğrulama ortamı](development-environment.md)

## Kaynak metinler

1. [Master bootstrap](requirements/01-master.md)
2. [Eklentiler, mimari ve V0.1 kapsamı](requirements/02-extensions.md)
3. [Kabul yolculuğu ve tamamlanma ölçütleri](requirements/03-acceptance.md)
4. [Doğrulama, inceleme ve nihai rapor kuralları](requirements/04-verification.md)

## Mimari ve kullanım

- [Mimari genel bakış](architecture/overview.md)
- [Mimari karar kayıtları](adr/)
- [Geliştirme rehberi](development/)
- [Entegrasyonlar](integrations/)
- [Depo README](../README.md)

## İnceleme ve kanıt

- [V0.1 tamamlanma raporu ve mühendislik kararı](reports/v0.1-completion-report.md)
- [Çalıştırılmış test ve statik kontrol sonuçları](reports/quality.json)
- [Gerçek CLI kabul kaydı](reports/acceptance.json)
- [Kaynak klasörden bağımsız wheel kurulumu](reports/installation.json)
- [Son dosya envanteri ve SHA-256 kayıtları](reports/source-manifest.json)
- [İlk inceleme notları](reports/review-notes.md)
- [Yürütme/güvenlik revizyonu](reports/revision-01.md)
- [Workflow/veri bütünlüğü revizyonu](reports/revision-02.md)
- [Son ekip lideri revizyonu](reports/revision-03.md)
- [Ayrı süreçlerle kabul betiği](../scripts/verify_acceptance.py)

İnceleme raporları bir kusurun saptandığı andaki kayıtlardır; düzeltmelerin kapanış
kanıtı nihai raporda ve ilgili regresyon testlerinde bulunur. Bir testin planlanmış
olması çalıştırıldığı veya geçtiği anlamına gelmez.

## V0.2 — Real Godot verification

- [Complete sprint requirements](requirements/v0.2-feature-completion.md)
- [Step-by-step work plan and requirement mapping](work-plan-v0.2.md)
- [Godot integration and recovery contract](integrations/godot-headless-verification.md)
- [ADR 0005: staging and independent oracle](adr/0005-godot-staging-and-independent-oracle.md)
- [V0.2 completion report](reports/v0.2-completion-report.md)
- [V0.2 quality checks](reports/v0.2-quality.json)
- [Installed-package real Godot acceptance](reports/v0.2-acceptance.json)
- [Real crash/recovery acceptance](reports/v0.2-recovery-acceptance.json)
- [Clean wheel installation](reports/v0.2-installation.json)
- [V0.1 CLI regression on V0.2](reports/v0.2-v01-acceptance.json)
- [Final artifact/source integrity verification](reports/v0.2-final-verification.json)

V0.1 reports above remain historical records; V0.2 evidence uses separate files.

## V0.2 verification closeout

- [Adım adım kapanış planı](work-plan-v0.2-closeout.md)
- [Kapanış kararı ve platform sınırları](reports/v0.2-closeout-report.md)
- [Kalıcı kanıt kapsamı ve doğrulama](reports/v0.2-closeout/README.md)
- [V0.1 41 → 39 komut eşlemesi](reports/v0.2-closeout/v01-comparison.md)

## V0.3 — Rendered capture

- [Sprint work plan](work-plan-v0.3.md)
- [Rendered capture contract](integrations/godot-rendered-capture.md)
- [ADR 0006: renderer, checkpoints, and human review](adr/0006-rendered-capture-and-visual-review.md)
- [V0.3 completion report](reports/v0.3-completion-report.md)
- [Openable Windows review package](reports/v0.3-rendered/README.md)

## V0.4 — Concept ve static prop üretimi

- [57 bölümlük sprint şartnamesi](requirements/v0.4-feature-completion-sprint.md)
- [Adım adım iş planı ve kapsam eşlemesi](work-plan-v0.4.md)
- [Asset pipeline ve insan onayları](pipelines/asset-production.md)
- [Meshy entegrasyonu ve ücretli işlem sınırı](integrations/meshy.md)
- [Blender işleme sözleşmesi](integrations/blender-asset-processing.md)
- [ADR 0007: revision ve ücretli üretim](adr/0007-asset-revisions-and-paid-generation.md)
- [V0.4 mühendislik kararı ve kanıtlar](reports/v0.4-completion-report.md)
- [İnsan incelemesini bekleyen gerçek concept](reports/v0.4/concept/README.md)

Phase A uygulama doğrulaması, gerçek Meshy üretimi ve nihai insan görsel onayı
ayrı kabul aşamalarıdır. Fake provider kanıtları gerçek üretim veya insan onayı
yerine geçmez.

## V0.5 — Profile-driven asset factory

- [Work plan](work-plan-v0.5.md)
- [Asset profiles](architecture/asset-profiles.md)
- [Generalized production pipeline](pipelines/generalized-asset-production.md)
- [ADR 0008: profile versus specification](adr/0008-asset-profile-versus-specification.md)
- [V0.5 completion report](reports/v0.5-completion-report.md)
