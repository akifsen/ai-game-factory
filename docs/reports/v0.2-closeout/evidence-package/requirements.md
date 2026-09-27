# AI GAME FACTORY — V0.2 VERIFICATION CLOSEOUT
# Platform, Canlı Süreç Kurtarma ve Kalıcı Kanıt Kapanışı

Mevcut repository'de Engineering Lead ve Verification Engineer olarak çalış.

Bu görev yeni bir ürün sürümü geliştirme sprinti değildir.
Mevcut V0.2 tesliminin açık doğrulama başlıklarını kapatacaktır.

V0.1 veya V0.2'yi yeniden yazma.
V0.3'e başlama.
Yeni özellik kapsamı oluşturma.

Hedef dört başlık:

1. Gerçek Linux runtime ve yetkili uzak CI doğrulaması.
2. Godot çalışmaya devam ederken Factory ana sürecinin kesilmesi.
3. Geçici çalışma klasörlerinden bağımsız, doğrulanabilir kanıt teslimi.
4. V0.1 kabul akışındaki 41 → 39 komut farkının davranış bazında açıklanması.

Bu başlıkları peşinen uygulama hatası sayma.
Önce mevcut kod, test ve kanıtları incele.

Mevcut kanıt yeterliyse yeniden implementasyon yapma.
Kanıt eksikse hedefli doğrulama ekle.
Gerçek kusur bulunursa en küçük doğru düzeltmeyi ve regresyon testini uygula.

---

## 1. REPOSITORY-FIRST İNCELEME

Önce mevcut çalışma talimatlarını, Git durumunu ve ilgili dosyaları incele.

Başlangıç belgeleri:

- README.md
- docs/work-plan-v0.2.md
- docs/reports/v0.2-completion-report.md
- docs/reports/v0.1-completion-report.md
- İlgili mimari kararlar ve ADR'ler.

Bu belgelerden referans verilen gerçek dosyaları takip et:

- V0.2 quality, acceptance ve recovery kayıtları.
- V0.1 regresyon kabul kayıtları.
- Paket kurulum ve artifact bütünlük kayıtları.
- Kabul ve recovery betikleri.
- ProcessRunner, process ownership ve recovery kodu.
- İlgili güvenlik/regresyon testleri.
- CI workflow tanımları.

Dosya yollarını ve komutları repository'den doğrula.
Mevcut olmayan bir dosyanın içeriğini tahmin etme.

Tarihsel raporları değiştirme veya yeni sonuçlarla ezme.
Kullanıcının mevcut değişikliklerini koru.
Git reset, clean, force push veya geçmiş değiştirme yapma.

Dar bir kapanış planı oluştur:

docs/work-plan-v0.2-closeout.md

Her başlık için şunları göster:

- Beklenen davranış.
- Mevcut kanıt.
- Eksik doğrulama.
- Yapılacak işlem.
- Sonuç ve kanıt yolu.

---

## 2. BASELINE VE KANIT KİMLİĞİ

Değişiklikten önce mevcut normal test paketini ve statik kontrolleri çalıştır.

Rapordaki:

238 passed / 5 skipped / 1 deselected

sonucunu tarihsel başlangıç bilgisi olarak ele al.
Bugünkü sonuçmuş gibi kopyalama.

Atlanan ve seçilmeyen testleri test kimliği ve gerekçesiyle kaydet.

Şunları birbirinden ayır:

- İlgili platformda uygulanamaz.
- Yetki eksikliği.
- Araç eksikliği.
- Bilinçli marker ayrımı.
- Gerçek test hatası.

Windows'ta POSIX testinin uygulanamaz olması ile Linux'ta POSIX testinin
çalıştırılmaması aynı şey değildir.

Her doğrulama koşusunu ilgili kaynak durumuna bağla:

- Git commit, varsa.
- Dirty working tree bilgisi.
- Test edilen kaynakların manifest/hash bilgisi.
- İşletim sistemi ve mimari.
- Python sürümü.
- Godot sürümü ve executable hash'i.
- Kurulu wheel hash'i, kullanıldıysa.
- Gerçek komut, cwd, başlangıç/bitiş, exit code.
- Test sonucu ve kanıt yolları.

Dirty tree varsa yalnız HEAD kullanarak kaynak kimliğini tanımlama.

Kod düzeltildikten sonra eski koşuyu yeni kodun kanıtı diye sunma.
Etkilenen kontrolleri yeniden çalıştır.

---

## 3. GERÇEK PLATFORM DOĞRULAMASI

### Windows

Mevcut Windows ortamında ilgili testleri ve gerçek Godot kabulünü doğrula.

File-symlink testleri için önce izole bir geçici dizinde gerçek capability probe yap.

Yetki yoksa:

- Skip gerekçesini koru.
- Testi gevşetme.
- Junction sonucunu symlink testinin yerine geçmiş sayma.
- Sistem genelinde Developer Mode, ACL veya güvenlik ayarı değiştirme.
- Yönetici yetkisi almaya çalışma.

Yetkili Windows CI ortamı varsa ilgili testleri orada ayrıca çalıştır.

### Linux

Mevcut erişilebilir Linux yürütme ortamlarından birini kullan:

- Yetkili Linux CI runner.
- Mevcut WSL.
- Mevcut ve erişilebilir Linux container ortamı.

Hepsini kurmak veya hepsinde tekrar koşmak zorunda değilsin.
Gereken kanıtı sağlayan en küçük yolu seç.

Linux ortamında gerçekten çalıştır:

- Normal unit/integration suite.
- Uygulanabilir POSIX ve symlink güvenlik testleri.
- Temiz wheel kurulumu.
- Kaynak checkout dışında CLI.
- Gerçek Godot pozitif ve negatif kabulü.
- İlgili timeout/recovery kontrolleri.

Linux hedefli type check, Linux runtime kanıtı değildir.

Dosya sistemi güvenlik testlerinde kullanılan mount/filesystem bilgisini kaydet.
Windows-backed bir mount üzerinde alınan sonucu açıklamasız biçimde
genel Linux dosya sistemi doğrulaması olarak sunma.

Yeni WSL, Docker, işletim sistemi özelliği veya sistem servisi kurma.
Bunlar gerekiyorsa dış engel olarak belirt.

Godot için mevcut pinli sürüm ve bütünlük kontrolünü doğrula.
Beklenen sürüm bulunamıyorsa sessizce başka sürüm kullanma.
Normal kullanıcıya ait yönetilen alana gerekli test aracını hazırlamak yeterlidir;
global sistem kurulumu yapma.

---

## 4. UZAK CI: YETKİ VE DOĞRU REVİZYON

Önce mevcut Git remote, CI sağlayıcısı ve erişilebilir araçları belirle.
GitHub veya belirli bir CLI'ın mevcut olduğunu varsayma.

Workflow tanımını çalıştırmadan önce oku.

Yalnız bu doğrulama için gerekli, mevcut yetki kapsamındaki
build/test workflow'unu çalıştır veya yeniden çalıştır.

Şunları yapma:

- Yetkisiz push, commit, branch oluşturma.
- Release, package publish veya deployment.
- Repository görünürlüğünü değiştirme.
- Secret ekleme veya yetkileri genişletme.
- Güvenilmeyen kodu ayrıcalıklı runner üzerinde çalıştırma.
- Açık yetki olmadan yeni harcama doğuran runner kullanma.
- Başarısız koşuyu sınırsız tekrar ederek CI kotası tüketme.

CI sonucu şu kaynak durumuyla eşleşmeli:

- Doğrulanan branch/ref.
- Commit SHA.
- İlgili workflow tanımı.
- Gerçekte test edilen kaynak/paket.

Eski remote commit'in yeşil sonucu, yeni yerel değişiklikleri doğrulamaz.

Yerel düzeltmeler remote'a taşınmadan CI'da test edilemiyorsa:

1. Yerel doğrulamayı tamamla.
2. Hazır değişiklikleri ve gerekli sonraki eylemi belirt.
3. Push yapma.
4. Remote doğrulamasını açık bırak.
5. Başka revizyonun sonucuyla açığı kapatma.

CI için run/job kimliği, kaynak SHA, ortam, test sonuçları ve
erişilebilir kanıt referanslarını kaydet.

Workflow dosyası oluşturulması veya güncellenmesi,
workflow'un başarıyla çalıştığı anlamına gelmez.

Yetki veya bağlantı engeli varsa bunu açıkça raporla.
Bu engel diğer yerel kapanış işlerini durdurmasın.

---

## 5. CANLI GODOT SIRASINDA KESİNTİ TESTİ

Önce mevcut recovery testlerini incele.

Şu iki durumun farklı olduğunu koru:

A. Godot runtime tamamlandıktan sonra Factory kesiliyor.
B. Godot hâlâ çalışırken Factory ana süreci kesiliyor.

Mevcut kanıt yalnız A'yı kapsıyorsa B için hedefli gerçek test ekle.

Test gerçek Godot executable'ını ve kontrollü fixture'ı kullanmalı.
Fake ProcessRunner sonucu gerçek engine kesinti kanıtı sayılmaz.

### Test sırası

1. Factory'yi test supervisor'ından ayrı süreç olarak başlat.
2. Gerçek Godot runtime'ın başladığını doğrula.
3. Fixture'ın test amaçlı hazır/ilerleme işaretini bekle.
4. Godot'un hâlâ çalıştığını ve terminal receipt'in henüz olmadığını doğrula.
5. Factory ana sürecini finally/normal shutdown'a güvenmeden zorla sonlandır.
6. Godot child process'inin gözlenen durumunu kaydet.
7. Yeni CLI sürecinden inspect/resume/retry davranışını kontrol et.
8. Kalıcı workflow, execution ve launch kayıtlarını doğrula.
9. Yalnız teste ait olduğu kanıtlanan kalan süreçleri güvenle temizle.

Rastgele birkaç saniye sleep edip “runtime başlamıştır” varsayma.
Sınırlı timeout ve açık handshake kullan.

Fault injection yalnız test helper/fixture tarafında olsun.
Üretim CLI'ına genel amaçlı crash veya process-kill komutu ekleme.

Fixture sonsuza kadar çalışmamalı.
Test supervisor'ında bağımsız süre sınırı ve cleanup bulunmalı.

---

## 6. CANLI KESİNTİDE KABUL EDİLEN DAVRANIŞ

Bu sprint otomatik recovery özelliği eklemek zorunda değildir.

Şu iki güvenli sonuç, mevcut sözleşmeye göre kabul edilebilir:

### Sonuç 1: Sahip olunan child güvenle sonlandırılmıştır

- Terminal kanıt ve mevcut recovery kuralları değerlendirilir.
- Eksik başarı kanıtından COMPLETED üretilmez.
- Eski attempt/history korunur.
- Yeniden yürütme gerekiyorsa açık retry ve yeni attempt kullanılır.

### Sonuç 2: Child canlıdır veya sahiplik/sonuç belirsizdir

- Workflow UNCERTAIN/BLOCKED kalır.
- Resume/retry ikinci Godot kopyasını başlatmaz.
- Önceki rapor veya state yeni attempt için kullanılmaz.
- Kullanıcıya gerekli manuel inceleme açıkça bildirilir.

Süreç görünmüyor diye görevi başarılı sayma.
Receipt yokken “kesin çalışmadı” varsayımı yapma.

Zorunlu güvenlik invariants:

- Canlı eski yürütmenin üzerine ikinci yürütme açılmaz.
- PID tek başına sahiplik kanıtı sayılmaz.
- İlgisiz kullanıcı süreçleri etkilenmez.
- Eksik kanıt quality gate açmaz.
- Geçmiş attempt/log kayıtları kaybolmaz.
- Kontrolsüz otomatik retry döngüsü oluşmaz.

İlgisiz süreç güvenliği için testin kendisinin oluşturduğu hafif bir
sentinel süreç kullanılabilir; gerçek kullanıcı süreçleri üzerinde deney yapma.

Test sonunda gerçek launch sayısını ve süreç durumunu kanıtla.
Yalnız “yeni artifact yok” gözlemini process başlamadığının tek kanıtı yapma.

Windows ve Linux sonuçlarını ayrı raporla.
Bir platformdaki cleanup davranışını diğerine varsayımla taşıma.

---

## 7. GEÇİCİ KLASÖRDEN BAĞIMSIZ KANIT TESLİMİ

Mevcut raporda referans verilen Temp workspace'leri ve JSON kayıtlarını incele.

Önce şu soruyu cevapla:

Temp workspace erişilemez olduğunda,
rapordaki önemli kabul iddiaları eldeki kalıcı dosyalardan incelenebiliyor mu?

JSON'lar yeterli kanıtı zaten içeriyorsa yeni sistem yazma.
Eksik olan kanıtları küçük, taşınabilir bir teslim paketiyle tamamla.

Tercih edilen yer:

docs/reports/v0.2-closeout/

Repository'nin mevcut düzeni farklıysa tutarlı bir karşılık kullan.

Paket en azından gerekli kapsamda şunları içermeli:

- Kapanış raporu ve kapsam indeksi.
- Kaynak/paket kimliği.
- Test ve CI sonuçları.
- Kabul/recovery komut kayıtları.
- İddiaları destekleyen observation ve validation çıktıları.
- İlgili process/launch/terminal metadata.
- Gerekli stdout/stderr.
- Artifact manifest'i, boyutlar ve hash'ler.

Tüm scratch klasörünü, venv'i veya Godot binary'sini pakete koyma.
Secret, kullanıcı save verisi veya gereksiz repository içeriği taşıma.

Ham kanıtı değiştirdiğinde eski hash'i koruyormuş gibi davranma.
Redakte edilmiş/türetilmiş çıktı yeni artifact olarak tanımlanmalıdır.

DB gerekli değilse ilişkili kayıtların sınırlı JSON çıktısı yeterlidir.
SQLite gerekiyorsa canlı DB dosyasını WAL durumunu yok sayarak kopyalama;
tutarlı snapshot/backup kullan.

Eski Temp kanıtı kaybolmuşsa:

- Tarihsel koşuyu yeniden oluşturmuş gibi davranma.
- Mevcut dosyalarda kalan kanıtı belirt.
- Gerekli akışı yeni run/execution kimlikleriyle yeniden çalıştır.
- Yeni koşuyu tarihsel koşudan ayır.

---

## 8. SOĞUK KANIT DOĞRULAMASI

Mevcut bir doğrulayıcı varsa kullan.
Yoksa küçük bir test/betik ekle; yeni ürün alt sistemi veya CLI ailesi oluşturma.

Kanıt paketini farklı, boş bir dizine kopyala veya aç.

Doğrulama yalnız paket içindeki dosyaları kullanmalı:

- Orijinal Temp yollarına erişmemeli.
- Kaynak checkout'a bağımlı olmamalı.
- Ağ veya Godot gerektirmemeli.
- Manifestteki zorunlu dosyaları bulmalı.
- Hash ve boyutları doğrulamalı.
- Run/workflow/task/execution ilişkilerini kontrol etmeli.
- Eksik ve değişmiş dosyayı reddetmeli.

Arşiv kullanılıyorsa traversal veya dışarı çıkan linkleri kabul etme.

Negatif kontroller:

- Zorunlu observation kaldırıldığında FAIL.
- Bir log/rapor değiştirildiğinde hash FAIL.
- Başka execution'a ait rapor karıştırıldığında ilişki FAIL.

Bu kontrol yeniden oyun çalıştırma değildir.
Aynı şekilde harici imza veya bağımsız güvenilir attestation değildir.

Kanıt paketinin kendi içinde eksiksiz, taşınabilir ve tutarlı olduğunu doğrular.

Pakette yalnız belirli artifact'lar varsa kapsamı açıkça yaz.
Bir alt küme kontrolünü bütün tarihsel artifact'lar doğrulandı diye sunma.

---

## 9. V0.1 KABULÜ: 41 → 39 KOMUT FARKI

Önceki V0.1 raporu 41 komut,
V0.2 içindeki V0.1 regresyon raporu 39 komut bildiriyor.

Bu farkı otomatik olarak bug veya kapsam kaybı sayma.

Gerçek kayıtları ve kabul betiğini karşılaştır.

Komutları geçici yol/ID farklarından arındırılmış anlamsal adımlarla eşleştir:

- Hangi davranışı test ediyor?
- Hangi önkoşul veya ortam koşuluna bağlı?
- Güncel koşuda karşılığı var mı?
- Atlandıysa neden?
- Aynı invariant başka bir adımda doğrulanıyor mu?

Küçük bir karşılaştırma tablosu üret:

Önceki adım
→ Güncel karşılık
→ Korunan davranış
→ Farkın nedeni
→ Kanıt.

Özellikle korunduğunu doğrula:

- Tekrar güvenli init.
- Oyun dosyalarının korunması.
- Approval block/approve/resume.
- Failure propagation.
- Retry ve attempt geçmişi.
- Fake paid çağrı sayısı 0 → 1 → 1.
- Ret sonrası çağrı sayısı 0.
- Bütçe ve repository-write policy.
- Artifact/evidence inspection.

Sırf sayıyı 41'e çıkarmak için anlamsız komut ekleme.
Gerçek davranış kaybı varsa testi ve gerekiyorsa implementasyonu düzelt.

Eski ayrıntılı kayıt yoksa tam farkı bildiğini iddia etme.
Doğrulanabilen invariant'ları ve açıklanamayan farkı ayrı yaz.

---

## 10. DÜZELTME SINIRI

Doğrulama sırasında gerçek hata bulunursa:

1. Yeniden üret.
2. Kök nedeni belirle.
3. En küçük doğru düzeltmeyi yap.
4. Regresyon testi ekle.
5. İlgili gerçek kabulü yeniden çalıştır.
6. Yeni kaynak/paket kimliğiyle kanıtı kaydet.

Şunları değiştirmek bu görevin amacı değildir:

- Workflow engine mimarisi.
- Persistence yaklaşımı.
- Agent organizasyonu.
- Provider routing.
- Senaryo dilinin kapsamı.
- Genel plugin izolasyonu.
- Yeni process orchestration framework.
- Screenshot/vision/Meshy/Blender entegrasyonu.

Mevcut güvenli UNCERTAIN davranışını sırf akış “tamamlansın” diye gevşetme.
Test assertion'larını, secret kontrollerini veya izinleri zayıflatma.

Sürümü sırf rapor üretildi diye artırma.
Release/publish yapma.

---

## 11. SON REGRESYON VE TEMİZ KURULUM

Kod değiştiyse son kaynak durumunda tekrar çalıştır:

- Formatter/linter/type checker.
- Normal test suite.
- Etkilenen gerçek Godot kabul testleri.
- Canlı kesinti kabulü.
- V0.1 davranış regresyonları.
- Kanıt paketinin soğuk doğrulaması.

Wheel yeniden üretildiyse temiz ortama non-editable kur.
Gerçek kabulün hangi wheel üzerinde çalıştığını hash ile belirt.

Kaynak testlerinin eski wheel'de,
gerçek kabulün yeni wheel'de çalışmasını açıklamasız tek sonuçta birleştirme.

Ortamlar arası skip sayılarının aynı olmasını bekleme.
Uygulanabilir testlerin gerçekten çalışmasını bekle.

Gerçek ücretli provider/API çağrısı sıfır kalmalı.

---

## 12. KAPANIŞ KRİTERLERİ

Bu görev ancak ilgili iddiaların kanıtı varsa kapatılır.

Beklenenler:

- Windows doğrulaması güncel kaynakla ilişkili.
- Linux runtime gerçekten çalışmış.
- Yetkili uzak CI doğru revizyonda çalışmış.
- Canlı Godot kesintisi gerçek süreçlerle doğrulanmış.
- Duplicate launch ve yanlış cleanup olmadığı gösterilmiş.
- Kanıt paketi Temp'ten bağımsız doğrulanmış.
- 41 → 39 farkı açıklanmış veya doğrulama sınırı açıkça belirtilmiş.
- Gerçek kusurlar düzeltildikten sonra testler yeniden çalışmış.
- V0.1/V0.2 tarihsel raporları korunmuş.
- V0.3 kapsamına geçilmemiş.

Platformda uygulanamaz testler gerekçeli şekilde N/A olabilir.
Eksik runtime veya eksik yetki ise N/A diye gizlenmemeli.

Linux veya remote CI erişimi engelliyse:

- Tamamlanan işleri teslim et.
- Açık kalan kanıtı belirt.
- Gerekli tek sonraki kullanıcı/ortam eylemini yaz.
- Tam kapanış veya tüm platformlar doğrulandı deme.

Dış engel, bütün yerel işi yarım bırakma gerekçesi değildir.

---

## 13. FİNAL RAPOR

Yeni rapor oluştur:

docs/reports/v0.2-closeout-report.md

Başlıklar:

AI GAME FACTORY — V0.2 VERIFICATION CLOSEOUT REPORT

Engineering Decision:
APPROVED / APPROVED WITH COMMENTS / CHANGES REQUESTED / REJECTED

Verification Closure:
CLOSED / PARTIAL / BLOCKED

Scope:
Tested Source / Commit / Dirty State:
Baseline:
Changes Made:
Windows Runtime:
Linux Runtime:
Remote CI / Revision / Run IDs:
Skipped / Deselected Tests:
Live-Child Crash Verification:
Duplicate-Launch Protection:
Unrelated-Process Safety:
Evidence Package:
Cold Evidence Verification:
V0.1 Acceptance 41 → 39 Comparison:
Final Regression Results:
Clean Installation / Wheel Hash:
Critical / Major / Minor Findings:
Remaining External Blockers:
Actual Commands / Evidence Paths:
V0.3 Readiness:

Şunları birbirinden ayır:

- Doğrulanmış uygulama kusuru.
- Kanıt eksikliği.
- Ortam/yetki engeli.
- Kapsam dışı özellik.
- Küçük bakım önerisi.

Eksik zorunlu doğrulamayı “minor” etiketiyle tam kapanışa dönüştürme.

Rapor kararını yalnız test sayısına göre verme.
Tamamlanmış delegasyonu bağımsız inceleme yerine sayma.
Aynı agent'ın ikinci geçişini dış bağımsız audit diye sunma.

Final kullanıcı mesajında:

- Nelerin gerçekten kapandığı.
- Hangi platformların gerçekten çalıştığı.
- Açık engel olup olmadığı.
- Değiştirilen temel dosyalar.
- Rapor ve kanıt yolları.

yer alsın.

Büyük logları yanıta dökme.

---

## 14. ŞİMDİ BAŞLA

Mevcut host'un çalışma, araç ve delegasyon kurallarına uy.
Yalnız gerçekten mevcut ve yetkili araçları kullan.

İzlenecek sıra:

İlgili kaynakları incele
→ baseline çalıştır
→ dört başlığın mevcut kanıtını değerlendir
→ eksik hedefli kontrolleri ekle
→ gerçek ortam koşularını gerçekleştir
→ bulunan kusurları dar kapsamda düzelt
→ taşınabilir kanıtı doğrula
→ son regresyonları çalıştır
→ kapanış raporunu üret.

Rutin mühendislik kararları için tekrar tekrar soru sorma.

Yetki, yeni harcama, sistem ayarı değişikliği veya remote push gerektiren
işlemlerde sınırı aşma; kalan işleri tamamlayıp engeli açıkça bildir.

Son kural:

Eksik doğrulama yeni özellik ekleyerek kapanmaz.
Yeşil bir CI rozeti yanlış revizyonu doğrulamaz.
Bir raporun varlığı, raporun dayandığı kanıtın erişilebilir olduğunu göstermez.
Güvenli biçimde bloklanmak, kanıtsız başarıdan daha doğrudur.

Bu görev V0.2 doğrulama kapanışında biter.
V0.3 implementasyonuna geçme.