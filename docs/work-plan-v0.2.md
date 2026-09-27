# AI Game Factory — V0.2 adım adım iş planı

Hedef paket sürümü **0.2.0**. Kaynak: [21 bölümlük sprint sözleşmesi](requirements/v0.2-feature-completion.md).
V0.1 yeniden yazılmaz. V0.1 raporları ve kanıt dosyaları tarihsel olarak korunur.

## Başlangıç kanıtı

Git HEAD: `169a857`; başlangıç çalışma ağacı temiz. Önceki teslimden sonra Git
başlatılmış; bu çalışma Git geçmişini değiştirmez. 2026-09-27 yerel başlangıç
kontrolünde **149 passed, 3 skipped**; lint, format ve mypy geçti. Atlanan testler
Windows symlink yetkileriyle ilgilidir. [Baseline](reports/v0.2-baseline.json).
Godot yeniden tespit edildi: **4.7.2.stable.official.ed1daf0bf**. `--version` ve
`--help` çalıştırıldı; headless/path/import/script ve kullanıcı argüman ayırıcısı
mevcut. [Araç başlangıç kaydı](reports/v0.2-godot-baseline.json).

## Uygulama adımları

| Adım | İş | Bağımlılık | Kabul kanıtı | Durum |
|---|---|---|---|---|
| 01 | Kaynağın tamamını oku; Git, doküman, ilgili kod ve araçları incele | — | Kaynak kopyası; temiz Git; baseline ve Godot help/version | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 02 | Dar tasarım, task sınırı ve güvenlik sözleşmesini kaydet | 01 | ADR 0005: staging, oracle ayrımı, onay/recovery kararı | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 03 | Strict, sınırlı senaryo ve observation şemaları; Python assertion validator | 02 | Strict model/şema, güvenli yollar ve snapshot roundtrip testleri | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 04 | Gerçek scene/node fixture ve development-only paketlenebilir harness | 03 | Gerçek 100/3/0 → 40/2/100 gözlendi; mutable Dictionary regresyonu | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 05 | ProcessRunner metadata ve truncation bilgisini geriye uyumlu genişlet | 01 | Bağımsız inceleme bulguları düzeltildi; timeout/capture/cleanup kanıtı | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 06 | Kaynak manifest, güvenli attempt staging, scratch/artifact ayrımı | 03 | Boyut/hash/junction/secret/cache testleri; junction koşuları gerçek | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 07 | Ayrı Godot import/runtime işlemleri ve hata sınıflandırması | 04–06 | Import hatasında runtime 0; nonzero/timeout/eksik rapor başarısız | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 08 | Mevcut workflow, policy, approval, artifact/evidence/gate entegrasyonu | 07 | Kalıcı DAG; sahte PASS gate açmaz; kaynak/exe/harness onay fingerprint'i | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 09 | Kesinti, sahiplik belirsizliği ve retry/resume davranışı | 08 | Belirsiz child tekrar başlatılmaz; eski attempt/log korunur | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 10 | CLI godot-verify, capability/doctor, paket kaynakları ve sürüm | 08 | JSON ve exit-code uyumu; config/state geriye uyumluluğu | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 11 | Fake process entegrasyonları, negatif ve stale kanıt testleri | 03–10 | Her hata sınıfı ve onaydan önce 0 çağrı | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 12 | Gerçek Godot pozitif/negatif/mutation/timeout ve üç tekrar | 11 | Gerçek gözlem; expected/actual; aynı semantik state | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 13 | Ayrı CLI süreçlerinde onay, recovery, bağımsız oyun kabulü | 12 | Kaynaklar değişmez; tamamlanmış resume ek süreç açmaz | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 14 | Temiz wheel, kaynak repo dışında CLI ve gerçek doğrulama | 13 | Paket içi GDScript/şema; izole kurulum; Unicode/boşluklu yollar | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 15 | Ayrı inceleme, V0.1 regresyonları, statik kalite ve CI tanımı | 14 | Bulgu→düzeltme→test; pinli Godot ve binary bütünlük kontrolü | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 16 | Doküman gerçeklik kontrolü ve tek mühendislik kararı | 15 | V0.2 raporu; 21 madde kanıt eşlemesi; açık sınırlamalar | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |

## Tasarım ve uygulama sınırları

Küçük DAG: `godot_execute` → `godot_validate` → mevcut `record_evidence`.
Execute attempt'i kendi çalışma kopyası, import ve runtime süreçlerini kapsar.
Validation sadece Python tarafındaki bağımsız beklentileri değerlendirir. Import
başarısızsa runtime çağrılmaz; her süreç sonunda veya hata/timeout yolunda eldeki
loglar ve metadata kalıcı artifact olarak kaydedilir. Eski attempt çıktısı yeniden
kullanılmaz. Rapor tamamlama işareti, process sonucu ve assertion birlikte gerekir.

Senaryo tam snapshot'ı workflow'a bağlanır. Harness'a yalnız eylemler, tick sınırı,
örnekleme noktaları ve Factory'nin verdiği korelasyon kimliği gider; beklentiler
gönderilmez. Tick sıfır başlangıç durumudur; tick'teki izinli eylem uygulanıp açık
scene adımı tamamlandıktan sonra snapshot alınır. Fizik/frame sinyali için evrensel
determinism iddiası yoktur. Gerçek fixture integer durum taşır.

Proje kaynakları izinli ve sınırlı bir manifest üzerinden attempt scratch alanına
kopyalanır. `.git`, `.venv`, `.gamefactory`, engine cache'leri ve credential dosyaları
kopyalanmaz. Junction/symlink kaçışları ve aşırı dosya/byte miktarı engellenir.
Harness yalnız staged projeye eklenir; kullanıcı `user://` save alanından ayrılır.
Çalışma kopyası işletim sistemi sandbox'ı değildir; yalnız güvenilen yerel projeler.

Process execution ve managed-write ihtiyaçları mevcut merkezi policy'de
değerlendirilir. Gerekli config alanı varsayılanı eski davranışı koruyacak şekilde
eklenir. Onaydan önce import/runtime yoktur. Senaryo, kaynak manifest, executable,
harness veya ilgili parametre değişirse eski onay yetmez. Süreç yaşam durumu
kesintiden sonra kanıtlanamıyorsa workflow bloklanır; PID'e dayanarak kullanıcı
süreci öldürülmez. Tamamlanmış resume dış süreç çağırmaz.

## 21 kaynak bölümünün izlenebilirliği

| § | Gereksinim | Plan / doğrulama | Durum |
|---|---|---|---|
| 1 | Bugünkü başlangıç durumu | Adım 01; baseline ve gerçek help/version | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 2 | Mevcut mimariyi koruma | 02, 08, 15; domain/adaptör ve regression incelemesi | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 3 | Dar kapsam ve kapsam dışı | 02, 16; yalnız Godot headless | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 4 | Gerçek CLI kullanıcı akışı | 10, 13; ayrı JSON CLI süreçleri | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 5 | Pipeline ve hata bağımlılıkları | 07–08, 11; import/runtime/gate ayrımı | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 6 | Sürümlü senaryo; oracle ayrımı | 03–04; strict schema ve snapshot testleri | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 7 | Gerçek scene/node fixture ve mutation | 04, 12; gerçek durum ve kasıtlı yanlış beklenti | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 8 | Tick semantiği ve üç tekrar | 04, 12; normalize state karşılaştırması | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 9 | Gerçek adapter/flag/kullanıcı argümanı | 01, 07; help doğrulaması ve argv testleri | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 10 | Küçük development-time harness | 04, 13; dar scene sözleşmesi; bağımsız oyun | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 11 | Güvenli staging ve kaynak manifest | 06, 13; kaynak hash ve path sınırları | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 12 | Kalıcı log/state/process artifact | 05, 07–08; hata ve timeout kanıtı dahil | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 13 | Bağımsız assertion ve gate | 03, 08, 11–12; expected/actual ve engine error sınıfları | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 14 | Merkezi policy/onay | 08, 13; 0 çağrı; değişen girdide yeniden onay | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 15 | Hata, timeout, child sahipliği ve recovery | 05, 09, 13; kesinti testi ve korunan attempt | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 16 | Stale/sahte/tamper kanıt reddi | 11; kaynak bölümdeki sekiz negatif durum | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 17 | Unit/fake/real ayrımı ve gerçek testler | 11–12, 15; ayrı işaretler ve sonuçlar | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 18 | Gerçek pozitif/negatif/onay/recovery kabul | 13; bağımsız CLI süreçleri | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 19 | Wheel/uyumluluk/Unicode/CI | 10, 14–15; paket içi kaynak, eski config/state | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 20 | Doküman ve ayrı inceleme | 15–16; düzeltilebilir bulgular kapanır | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |
| 21 | DoD ve kapanış | 16; istenen rapor başlıkları ve tek karar | Tamamlandı; aşağıdaki kanıt eşlemesine bakınız |

## Kapsam dışında

Meshy/ücretli API, image/vision/audio, screenshot/video, Blender işleme,
GPU/FPS sertifikasyonu, mobil export, genel AI oyuncusu, uzaktan komut sunucusu,
web arayüzü, dağıtık worker, paralel scheduler, diğer motorlar ve V0.3 uygulanmaz.
Ücretli gerçek çağrı hedefi **0**. Push, release veya remote publish yapılmaz.

## Resmî teknik dayanak

- [Godot command line](https://docs.godotengine.org/en/stable/tutorials/editor/command_line_tutorial.html)
- [OS kullanıcı argümanları](https://docs.godotengine.org/en/stable/classes/class_os.html)
- [SceneTree yaşam döngüsü](https://docs.godotengine.org/en/stable/classes/class_scenetree.html)

Dokümantasyon tek başına runtime kanıtı sayılmaz; kurulu executable ile gerçek
kontroller ayrıca kaydedilir. Uzak CI tanımı yerel doğrulama yerine geçmez.


## Nihai kanıt eşlemesi

Karar: **APPROVED WITH COMMENTS**. [Tam kapanış raporu](reports/v0.2-completion-report.md).

| Adımlar / kaynak bölümleri | Gerçek kanıt |
|---|---|
| 01 / §1 | [Baseline](reports/v0.2-baseline.json), [Godot help/version](reports/v0.2-godot-baseline.json) |
| 02, 08 / §2–5 | ADR 0005, generic handler metadata, kalite ve CLI kabul kayıtları |
| 03–04 / §6–10 | Strict contract testleri, oracle içermeyen request, gerçek dört snapshot ve mutation |
| 05–07 / §11–12 | Staging/junction/secret/cache testleri; process metadata ve 197 artifact hash kontrolü |
| 08–09 / §13–16 | Bağımsız assertions, approval, stale/tamper testleri ve gerçek crash/recovery kabulü |
| 11–13 / §17–18 | [Kalite](reports/v0.2-quality.json), [gerçek Godot](reports/v0.2-acceptance.json), [recovery](reports/v0.2-recovery-acceptance.json) |
| 10, 14–15 / §19 | [Temiz wheel](reports/v0.2-installation.json), [V0.1 CLI](reports/v0.2-v01-acceptance.json), CI tanımı |
| 15–16 / §20–21 | [İnceleme kapanışı](reports/v0.2-review-findings.md), [son bütünlük](reports/v0.2-final-verification.json), tamamlanma raporu |

Son normal suite: **238 passed, 5 skipped, 1 deselected**. Ana gerçek kabul 43 komut;
V0.1 CLI 39 komut; temiz wheel ve ek gerçek recovery PASSED.
Üç gerçek koşuda HP/düşman/skor: 100/3/0 → 80/3/0 → 80/2/100 → 40/2/100.

“Tamamlandı” Linux runtime veya uzak CI'ın çalıştığı anlamına gelmez: CI tanımı
tamamlandı, bu iki ortamın runtime kanıtı yoktur. Dört file-symlink ve bir POSIX testi
atlanmıştır; Windows junction sınırları gerçekten test edilmiştir. Platform sınırları
ve süreç sahipliği kısıtları nihai raporda açıkça listelenmiştir.
