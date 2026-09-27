# AI Game Factory belgeleri

Bu çalışma dört kaynak metindeki 123 bölümü esas alır. Uygulama sınırı **Factory
Core V0.1**; üretim asset/AI entegrasyonları bu teslimin parçası değildir.

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
