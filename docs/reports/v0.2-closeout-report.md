# AI GAME FACTORY — V0.2 VERIFICATION CLOSEOUT REPORT

## Engineering Decision:

**APPROVED WITH COMMENTS** — Yerel Windows kapanış araçları ve kanıtları ekip
liderince incelendi ve doğrulandı. Platform kapanışı **PARTIAL**: son Linux/remote
revizyonun çalışması zorunlu açık doğrulamadır; bu karar tam kapanış değildir.

## Verification Closure:

**PARTIAL**. Tam kapanış için son test/workflow/betik revizyonunun uzak CI'da
çalışması ve Linux canlı-child/temiz kurulum/mount kanıtının tamamlanması gerekir.

## Scope:

V0.2 platform doğrulaması, canlı Godot sırasında Factory kesintisi, taşınabilir
kanıt ve V0.1 41→39 davranış eşlemesi. V0.1/V0.2 yeniden yazılmadı; V0.3,
release/publish, yeni sağlayıcı veya ürün alt sistemi eklenmedi.
Kaynak sözleşme: [eksiksiz istek](../requirements/v0.2-verification-closeout.md).

## Tested Source / Commit / Dirty State:

Başlangıç `169a8571d8e9f36d6ffac0cafce2d6a575f98473` + dirty V0.2 kaynakları.
Kullanıcı çalışma sırasında V0.2'yi `8ad413b385a2338752e1902068da892994acc4e2`
olarak commit/push ettiğini doğruladı. Bu ajan commit/push/branch işlemi yapmadı.
Başlangıç kaynak SHA-256 manifesti, commit geçişi ve ürün Git blob eşleşmesi
[v0.2-closeout](v0.2-closeout/README.md) altında ayrı kaydedildi.
HEAD tek başına dirty kaynak kimliği olarak kullanılmadı.

## Baseline:

Yeni yerel ölçüm: **238 passed, 5 skipped, 1 deselected** (50.50 s).
Ruff lint/format, mypy win32/linux hedefleri geçti (49 ürün kaynak dosyası).
Tarihsel sayılar bugünkü sonuç diye kopyalanmadı; tam komut ve zaman kayıtları
`baseline.json`, test kimlikleri `baseline-tests.xml` içindedir.

## Changes Made:

- `scripts/verify_closeout_evidence.py`: küçük standart-kütüphane paketleme/soğuk
  doğrulama betiği; gerçek acceptance/inspect/observation/validation bağları.
- `tests/unit/test_closeout_evidence.py`: gerekli dosya, hash, ilişki ve yol negatifleri.
- `scripts/verify_godot_live_crash.py` ve ilgili test: test tarafında gerçek canlı
  süreç kesintisi, süreç kimliği ve ayrı recovery CLI gözlemi (gerçek Windows koşusunda doğrulandı).
- `tests/unit/test_godot_staging.py`: POSIX symlink'i unlink, Windows junction'ı
  rmdir ile temizler; dışarı yazmayı reddeden assertion'lar değişmedi.
- `tests/integration/test_process_runner.py`: redaction hata enjeksiyonunu gerçekten
  çağrılan platform cleanup noktasına taşır; secret ve cleanup assertion'ları korunur.
- `.github/workflows/ci.yml`: mevcut matrix/pin korunarak skip nedenleri, wheel/hash,
  mount bilgisi ve gerçek recovery/live doğrulama adımları hazırlanır.
- Plan, bu rapor, gereksinim kopyası ve kalıcı kanıt indeksi eklendi.

Ürün sürümü **0.2.0**, üretim kaynakları değişmedi. Gerçek ücretli API çağrısı **0**.

## Windows Runtime:

Windows 11 / x86-64, Python 3.12.14, Godot **4.7.2.stable.official.ed1daf0bf**.
Yeni non-editable wheel üzerinden **43** gerçek kabul komutu geçti: üç pozitif
semantik koşu, yanlış oracle, hasar mutasyonu, parse hatası, timeout, onay/devam,
girdi değişince yeniden onay ve Factory'siz Godot.
Mevcut runtime-sonrası recovery **23** komutla geçti; terminal yokken UNCERTAIN,
terminal kanıt varken FAILED→açık retry→yeni attempt davranışları korundu.

İlk sandbox koşusu Windows root certificate store erişim hatasıyla FAIL oldu.
Bu kanıt silinmedi (`windows-acceptance-sandbox-failed.json`); normal kullanıcı
bağlamında yeni workspace/run ile geçti. Assertion veya engine hata tespiti gevşetilmedi.

## Linux Runtime:

Kullanıcının V0.2 commit'ine ait GitHub Ubuntu runner'ında gerçekten çalıştı:
Godot işi **1 passed / 243 deselected**; gömülü kabul **43 komut PASSED**.
Normal paket her iki Python sürümünde **235 passed / 4 failed / 5 skipped**.
Dört hata test yardımcılarında yeniden üretim kanıtıyla saptandı ve yerelde düzeltildi;
son düzeltmelerin Linux runtime sonucu henüz yoktur.

Eski Linux koşusunun mount/filesystem türü kaydedilmedi; `/tmp` ve checkout yolları
loglarda mevcut olsa da bu bir filesystem sertifikasyonu değildir. Yeni workflow
`findmnt` ve `df -T` kaydeder. Linux hedefli mypy Linux runtime yerine sayılmadı.
Yerelde yalnız durmuş docker-desktop WSL bulundu; mevcut Docker Desktop başlatma
 denemesi sonrası daemon pipe erişilemedi. Yeni WSL/Docker/OS/servis kurulmadı.

## Remote CI / Revision / Run IDs:

[Run 36287573467](https://github.com/akifsen/ai-game-factory/actions/runs/36287573467),
`main`, SHA `8ad413b385a2338752e1902068da892994acc4e2`, genel sonuç **failure**.

| Job | Kimlik | Gerçek sonuç |
|---|---|---|
| godot-real / Ubuntu | 108531129283 | success; 1 passed |
| Ubuntu / Python 3.12 | 108531129365 | failure; 235 passed, 4 failed, 5 skipped |
| Ubuntu / Python 3.11 | 108531129829 | failure; aynı dört test |
| Windows / Python 3.11 | 108531129424 | success; 242 passed, 2 skipped |
| Windows / Python 3.12 | 108531129455 | success; 242 passed, 2 skipped |

`remote-ci/run.log`, `jobs.json`, `run.json` ve gerçek Godot JSON indirildi.
Artifact 10921500736 SHA256
`001b295e22b6863e0a481ed3f1171112a596b1b3053602e61f816f7174ea3141`
API metadata'sıyla eşleştirildi; yalnız seçili üst düzey kabul JSON'u çıkarıldı.
Eski başarılı run 36281528101 yalnız 169a857'i doğrular; closeout için kullanılmadı.
Yeni yerel test/betik/workflow değişiklikleri bu remote koşuda yoktur; eski koşu bunları
onaylamaz. Gereksiz yanlış-revizyon rerun veya yetkisiz push yapılmadı.

## Skipped / Deselected Tests:

Yerel kimlikler ve nedenler `test-exclusions.json`:

- `TestExecutionLock::test_symlink_lock_leaf_cannot_write_outside_lock_directory`
- `test_manifest_rejects_symlinked_file_when_supported`
- `TestPathGuard::test_symlink_escape_rejected_if_pointing_outside`
- `TestPathGuard::test_symlink_parent_escape_rejected_for_missing_leaf`

Bu dört test için gerçek probe **WinError 1314**: yetki eksikliği; sistem ayarı
veya yönetici yetkisi değiştirilmedi. Windows CI 3.11/3.12'de dördü gerçekten geçti.
`TestProcessRunner::test_posix_parent_exit_kills_descendant_ignoring_sigterm`
yerelde platform N/A; Linux CI'da gerçekten geçti.
`test_real_godot_acceptance` normal suite'de bilinçli marker ayrımıdır; gerçek
black-box betik kurulu wheel ile ayrıca yürütüldü.
Linux'ta Windows Job Object/junction testleri platform N/A; real marker normal
pakette opt-in skip, ayrı Godot job'ında çalıştı. Ayrıntılı test/job satırları
`remote-ci/test-summary.json` içindedir. Gerçek başarısızlıklar skip yapılmadı.

## Live-Child Crash Verification:

Son betik hash'iyle gerçek Windows koşusu **PASSED** (`windows-live-crash.json`):
workflow `WF-GODOT-2cd66cd7`, execution `EXEC-05fc86d9`. Gerçek fixture unique-token
handshake'i ve native process handle/creation time/executable kimliğiyle Godot PID
4900 canlı doğrulandı; terminal receipt yokken Factory PID 4740 retained process
handle üzerinden zorla kesildi. Child post-kill ve final gözlemde sonlanmıştı.
Mekanizmaya ilişkin kanıtsız Job Object varsayımı yapılmadı.

Ayrı 10 Python CLI-entry süreci aynı kurulu wheel'in gerçek `gamefactory.cli.main`
girişini kullandı. Test-only Popen gözlemi üretim sonucunu taklit etmez; gerçek
Godot executable çalışır. İki resume exit 3, retry exit 1; tek UNCERTAIN attempt
korundu, workflow BLOCKED ve gate kapalı kaldı. Bounded fixture ve supervisor
cleanup uygulandı. Linux canlı-child koşusu **henüz yürütülmedi**.

## Duplicate-Launch Protection:

Son append-only launch journal **1 gerçek runtime launch** ve **1 fixture startup**
içerir; 10 CLI wrapper başlangıcı gözlendi. Lider ayrıca iki resume + bir retry
parent PID'sinin tüm subprocess kayıtlarını karşılaştırdı: **0 subprocess**.
Bu yalnız yeni artifact olmamasına dayalı değildir. Ham journal ve handshake
`live-process-evidence/`, bağımsız kontrol `lead-live-validation.json` içindedir.

## Unrelated-Process Safety:

Yalnız testin oluşturduğu sentinel PID 55628 kullanıldı; Factory kesintisinden
sonra ve bütün resume/retry boyunca canlı kaldı, sonra sahibi supervisor temizledi.
Windows native handle tutuldu; POSIX cleanup kodu exe/starttime tekrar doğrular,
belirsiz sonucu başarılı cleanup saymaz. Son POSIX davranışının runtime kanıtı açık.
Gerçek kullanıcı süreçleri üzerinde deney yapılmadı.

## Evidence Package:

`v0.2-closeout/raw-artifacts/` içinde yeni Windows kabul/recovery koşularının
**251** kayıtlı artifact (242 kabul/recovery + 9 canlı kesinti)'i DB hash/boyutlarıyla bağımsız karşılaştırıldı.
Byte'lar değiştirilmeden kopyalandı; mapping `artifact-export-map.json`.
Tarihsel Temp klasörleri mevcut, fakat eski JSON'lar ham logların tüm byte'larını
gömmediği için yeni taşınabilir teslim hazırlandı; eski run yeniden yaratılmış gibi
sunulmadı. DB/WAL, binary, venv, tüm scratch ve kullanıcı save verisi kopyalanmadı.

## Cold Evidence Verification:

İlk gerçek paketin kopyası boş başka dizinde `python -I` ile geçti; ağ/Godot/kaynak
checkout/orijinal Temp okumaz. Observation silme FAIL, log değişikliği hash FAIL,
farklı execution kimliği ve yeniden hesaplanmış manifest hash'i ilişki FAIL.
Kapsam: seçili pozitif workflow'un gerçek graph/rapor ilişkileri ve seçili tüm
ham dosyaların boyut/hash bütünlüğü. Tüm tarihsel artifact semantiği, harici imza
veya bağımsız güvenilir attestation iddiası yoktur. Son paketin bağımsız kopyasında aynı kontroller `delivery-verification.json` ile kaydedilir.

## V0.1 Acceptance 41 → 39 Comparison:

İlk 39 anlamsal komut aynı sırada. Eksik iki adım betiğin `if args.godot`
koşulundaki Factory'siz import ve normal runtime'dır; tarihsel çağırıcı argv'si
kaydedilmediği için niyet tahmin edilmez. V0.2 ana kabulde karşılığı ayrıca vardır.
Yeni closeout kabulü açık Godot parametresiyle **41 komut PASSED**.
Init/hash koruma, approval, failure/retry/history, fake paid **0→1→1**, ret sonrası
**0**, bütçe, repo-write ve artifact/evidence davranışları korunur.
[Karşılaştırma tablosu](v0.2-closeout/v01-comparison.md).

## Final Regression Results:

Son kaynak durumunda **259 passed, 5 skipped, 2 deselected** (73.63 s).
Ruff check geçti; **82 Python dosyası** format kontrolünden geçti; mypy win32/linux
hedeflerinde **49 ürün kaynak dosyası** geçti. Final test kimlikleri ve komutlar
`final-tests.xml`, `final-test-exclusions.json`, `final-quality.json` içindedir.
Canlı Godot kesintisi son verifier düzeltmesinden sonra yeniden geçti.
V0.1 41 komut, gerçek Godot 43 komut ve önceki recovery 23 komut aynı temiz wheel'i
kullanır; bu koşulardan sonra ürün byte'ları değişmedi (`final-product-identity.json`).
Yeni workflow yalnız YAML/pin/matrix/diff kontrolünden geçti; remote çalıştı denmez.

## Clean Installation / Wheel Hash:

`gamefactory-0.2.0-py3-none-any.whl` SHA256:
`5a88523723a531efe6bc09a8ffa84939b960ae6815c28dbdaa06e86496d09fe7`.
Yeni venv'e offline non-editable kuruldu, pip check geçti, checkout dışında CLI
ve import yolu doğrulandı; wheel içindeki ürün kaynak/resource byte'ları eşleşti.
Godot console launcher ve gerçek ana executable hashleri `installation.json`.
Test-only closeout değişiklikleri ürün wheel'ini değiştirmedi; son kaynak eşitliği
ayrı kontrol edilir. Linux temiz wheel/hash yeniden koşusu henüz yok.

## Critical / Major / Minor Findings:

- **Doğrulanmış ürün kusuru:** bu closeout'ta bulunmadı.
- **Doğrulanmış test kusuru:** Linux'ta üç symlink cleanup ve bir cleanup redaction
  enjeksiyonu; assertion'lar korunarak düzeltildi, son Linux koşusu açık.
- **Kanıt aracı bulgusu:** ilk canlı kesinti ve ilk portable verifier teslimleri
  lider incelemesinde geri çevrildi. Gerçek kaynak/OS ilişkileri doğrulanmadan
  agent'ın PASS beyanı kabul edilmedi. Aynı ekip çalışması harici audit sayılmaz.
- **Zorunlu kanıt eksikliği:** yeni revizyon remote CI, Linux canlı-child ve temiz
  wheel/mount doğrulaması. Bunlar minor veya N/A olarak sınıflandırılmadı.
- **Kapsam dışı:** otomatik UNCERTAIN reconciliation, V0.3, ücretli provider,
  Meshy/Blender/vision entegrasyonu.

Canlı supervisor watchdog son sınırda exit 124 ile kapanır; bu nadir yolda JSON
raporu oluşmayabilir. Çağırıcı komut kaydı/CI logu exit kodunu tutar. Başarılı
sunulan koşuda bu yol çalışmadı; tam JSON ve ham süreç kanıtı mevcuttur.

## Remaining External Blockers:

Yeni yerel revizyon uzak CI'da değil; yerel Linux daemon erişilemiyor.
Gerekli sonraki kullanıcı eylemi: hazır değişiklikleri inceleyip yetkili normal
commit/push akışıyla yayımlamak. Mevcut CI yeni revizyonda çalışınca Linux sonuçları
ve kalıcı kanıtları bu rapora yeni run kimliğiyle eklenmelidir. Bu ajan push yapmadı.

## Actual Commands / Evidence Paths:

[Kanıt indeksi](v0.2-closeout/README.md), `baseline.json`,
`windows-commands.json`, `windows-runtime-commands.json`, `remote-ci/retrieval.json`
`windows-live-command.json`, `final-quality.json` ve cold doğrulama JSON'ları tam komut/cwd/zaman/exit/çıktı referansları içerir.

## V0.3 Readiness:

V0.3 başlatılmadı. Eksik zorunlu platform kapanışı tamamlanmadan tüm platformlar
onaylandı veya V0.2 verification CLOSED denmez.
