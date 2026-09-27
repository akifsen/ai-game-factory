# AI GAME FACTORY — V0.2 HELPER FIX CI CLOSURE

## Verification Closure: CLOSED

2026-09-27 helper düzeltmesi incelemesi. Test edilen kod, test, helper ve workflow
revizyonu `fef168cdd19af5251548eae0e3dd922497be71e1` dir. Bu dosyadaki kapanış
metni ve `remote-ci/36290153250/` kanıtı o revizyondan sonra eklenmiştir; yeni CI
başlatmaz ve o SHA'nın içeriğini değiştirmez.

- Yerel HEAD, `origin/main` ve GitHub API `main` aynı SHA. İnceleme başında çalışma
  ağacı temizdi. Üç helper/test dosyasının Git blob SHA'ları GitHub içerik API'si
  ile HEAD'e eşit. CI `commit.txt` aynı SHA, `source-status.txt` boş. Bu dört
  dosyanın içerik SHA256 değerleri CI kaynak manifestiyle, Git nesnesinden
  okunarak, eşleşti.
- [CI run 36290153250](https://github.com/akifsen/ai-game-factory/actions/runs/36290153250),
  attempt 1, push, genel sonuç **success**. Rerun, yeni commit/push, release veya
  deployment yapılmadı.
- Önceki koşu [36289019065](v0.2-closeout/remote-ci/36289019065/)
  (`93ded412242b2bebf0d90ab90225b5826b852489`) bu düzeltmeyi içermez. Recovery
  FAILED ve canlı-child NOT RUN kayıtları değiştirilmedi ve bu kapanışın kanıtı
  sayılmadı.

| Kontrol | Job kimliği | Ortam | Gerçek sonuç |
|---|---|---|---|
| Normal testler | 108538554884 | Ubuntu 24.04.5, Python 3.11.16 | 277 passed, 6 skipped; lint/format/mypy geçti |
| Normal testler | 108538555025 | Ubuntu 24.04.5, Python 3.12.14 | 277 passed, 6 skipped; lint/format/mypy geçti |
| Normal testler | 108538555027 | Windows Server 2025, Python 3.11.9 | 280 passed, 3 skipped; lint/format/mypy geçti |
| Normal testler | 108538554959 | Windows Server 2025, Python 3.12.10 | 280 passed, 3 skipped; lint/format/mypy geçti |
| Gerçek Godot kabulü | 108538554797 | Ubuntu 24.04.5, Python 3.12.14, Godot 4.7.2.stable.official.ed1daf0bf | PASSED, 43 komut |
| Temiz wheel / kaynak dışı CLI | 108538554797 | Aynı Linux ortamı | PASSED; venv site-packages, pip check, checkout dışı workspace |
| Recovery | 108538554797 | Aynı Linux ortamı | PASSED; interpreter `/home/runner/work/_temp/gamefactory-venv/bin/python`; fault helper exit 91; paket yolu venv site-packages |
| Canlı-child crash | 108538554797 | Aynı Linux ortamı | PASSED; 2026-09-27T03:02:18.444586Z–03:02:26.549803Z, 8.105 s; Godot pid 2811 kill öncesi canlı, kill sonrası alive, final terminated |
| Filesystem/mount | 108538554797 ve iki Ubuntu matrix job'ı | Linux | `/dev/sda1`, ext4 |

Linux interpreter regresyonu `test_real_venv_regression_without_downloads` her iki
Ubuntu işinde recovery ve live_crash için **PASSED**, skip değil. Test gerçek
symlink venv kullanır; seçilen yolun dereference edilmediğini, `sys.prefix`'in
venv dizini olduğunu ve dereference edilmiş yorumlayıcının prefix'inin venv
olmadığını çalıştırarak doğrular. Kurulu `gamefactory` erişimi bu birim testinden
değil, aynı `_resolve_python` yolunu kullanan godot-real fault helper kaydından
gelir: `package_path` venv `site-packages` altındadır ve checkout `src` ağacından
import reddi tetiklenmeden exit 91'e ulaşılmıştır. Hostedtoolcache yorumlayıcısı
recovery/canlı-child kayıtlarında yoktur. Wheel SHA256
`b83989ea5e6be1bfc209fe8df222a8d256eef6e8fbb94e8e91ab5fcf4dda2947`.

Kanıt: [verification-summary.json](v0.2-closeout/remote-ci/36290153250/verification-summary.json)
ve [remote-ci/36290153250/](v0.2-closeout/remote-ci/36290153250/). Üç artifact ZIP
hash'i API digest ile eşleşti. `resolve()` kaldırma işlemi Godot kimliği veya
dosya güvenliği kontrollerine yayılmadı.

Mevcut soğuk doğrulayıcı yeniden kullanıldı. Checkout dışı `python -I` sonucu
[ci-cold-verification-36290153250.json](v0.2-closeout/ci-cold-verification-36290153250.json)
içindedir: pozitif PASS, yeni kabul dosyası eksikliği FAIL, log tahrifi FAIL,
geri yükleme PASS. Bu kontrol Linux koşusunu yeniden oynatmaz.

## Tarihsel Verification Closure: PARTIAL

Aşağıdaki bölüm run `36289019065` / SHA `93ded41` incelemesinin kaydıdır.
O koşunun recovery FAILED ve canlı-child NOT RUN sonuçları durur.

2026-09-27 incelemesi. Dört normal matrix işi geçti; gerçek Linux Godot kabulü,
temiz wheel ve mount kanıtı tamamlandı. Recovery helper venv Python symlink'ini
çözerek kurulu ortamdan çıktığı için başarısız oldu. Aynı shell adımındaki Linux
canlı-child doğrulaması **çalışmadı**. Bu eksik runtime kanıtı platform-N/A değildir.

## Doğrulanan revizyon ve yetki

- HEAD ve GitHub `main`: `93ded412242b2bebf0d90ab90225b5826b852489`.
- İnceleme başlangıcında çalışma ağacı temizdi. Remote ref GitHub API ile okundu;
  push edilmiş olduğu varsayılmadı. CI artifact `commit.txt` ve temiz
  `source-status.txt` aynı kaynağı doğrular. Sekiz ilgili dosyanın Git blob SHA256
  değerleri CI kaynak manifestiyle bağımsız karşılaştırıldı.
- [CI run 36289019065](https://github.com/akifsen/ai-game-factory/actions/runs/36289019065),
  attempt 1, push, genel sonuç **failure**. Rerun/dispatch, commit/push,
  deployment/release, sürüm artışı veya V0.3 çalışması yapılmadı.
- Aşağıdaki sonuçlar yalnız bu SHA'ya aittir. Sonradan hazırlanan helper düzeltmesi
  ve rapor/kanıt ekleri bu CI'da test edilmiş sayılmaz.

## Gerçek CI sonuçları

Tüm satırların SHA'sı `93ded412242b2bebf0d90ab90225b5826b852489`, run kimliği
`36289019065`. Log ve artifact bağlantıları [run kanıt dizinindedir](v0.2-closeout/remote-ci/36289019065/).

| Kontrol | Job kimliği | Ortam | Gerçek sonuç |
|---|---|---|---|
| Normal testler | 108535325836 | Ubuntu 24.04.5, Python 3.11.16 | 260 passed, 6 skipped; lint/format/mypy geçti |
| Normal testler | 108535325869 | Ubuntu 24.04.5, Python 3.12.14 | 260 passed, 6 skipped; lint/format/mypy geçti |
| Normal testler | 108535325862 | Windows Server 2025, Python 3.11.9 | 263 passed, 3 skipped; lint/format/mypy geçti |
| Normal testler | 108535325786 | Windows Server 2025, Python 3.12.10 | 263 passed, 3 skipped; lint/format/mypy geçti |
| Gerçek Godot kabulü | 108535325678 | Ubuntu 24.04.5, Python 3.12.14, Godot 4.7.2.stable.official.ed1daf0bf | PASSED, 43 komut |
| Temiz wheel / kaynak dışı CLI | 108535325678 | Aynı Linux ortamı | PASSED; yeni venv, non-editable wheel, pip check, site-packages import, checkout dışından gerçek CLI |
| Recovery | 108535325678 | Aynı Linux ortamı | FAILED; fault helper exit 1, beklenen 91; ModuleNotFoundError: gamefactory |
| Canlı-child crash | 108535325678 | Aynı Linux ortamı | NOT RUN; önceki recovery hatası shell'i durdurdu |
| Filesystem/mount | 108535325678 ve iki Ubuntu matrix job'ı | Linux | Kaydedildi; Godot checkout/temp `/dev/sda1`, ext4 |

Normal matrix işlerinde Godot sürümü N/A: motor çalıştırılmadı. Hiçbir test
deselect edilmedi. Ubuntu'daki altı skip: iki Windows Job Object testi, iki Windows
junction testi (platform-N/A), iki opt-in gerçek Godot testi. Windows'taki üç skip:
POSIX process-group cleanup (platform-N/A) ve aynı iki opt-in Godot testi.
Gerçek kabul ayrı job'da çalıştı; canlı-child skip'inin runtime karşılığı henüz yok.
Kimlikler, kaynak satırları ve tam nedenler
[verification-summary.json](v0.2-closeout/remote-ci/36289019065/verification-summary.json)
ve `job-<id>.txt` dosyalarında korunur.

## Önceki dört Linux hatası

İki Linux Python sürümünde de şu testler **PASSED**:

- `test_cleanup_error_message_redacts_request_secret`
- `test_manifest_rejects_linked_source_directory`
- `test_stager_rejects_root_directory_link`
- `test_attempt_parent_link_is_rejected_before_writing_outside_scratch`

Bu sonuçlar yeni revizyona aittir. Eski `8ad413b` / run `36287573467` sonuçları
ayrı tarihsel kanıttır; yeni düzeltmeleri doğrulamak için kullanılmadı.

## Temiz kurulum ve kalıcı kanıt

Linux wheel SHA256:
`8946102f9443ec6a87b3b7e24496d3b11137c31a0eac6db6b39559b993306472`.
Godot executable SHA256:
`8d106cbe6144c2dc7e881d61d2429c1a8a76e6b22ef48bd5e48dcf934953f71e`.
Kurulu paket `/home/runner/work/_temp/gamefactory-venv/lib/python3.12/site-packages`
içinden yüklendi; `pip check` geçti. Kabulün 43 CLI/engine komutu checkout dışındaki
workspace'te yürüdü. Bu başarı helper'ın yanlış Python seçimini gizlemez.

[Yeni run kanıtları](v0.2-closeout/remote-ci/36289019065/) run/jobs/ref metadata,
beş tam job logu, üç özgün artifact ZIP'i ve çıkarılmış dosyaları içerir.
Artifact 10921132346, 10921012629 ve 10922050240 arşiv hashleri API digest'leriyle
karşılaştırıldı. Kabul PASSED ve recovery FAILED JSON'ları değiştirilmedi.
Linux canlı-child JSON'u yoktur; mevcut olmadığı açıkça kaydedildi.

Mevcut soğuk doğrulayıcı aynen kullanılır. Yeni kompakt paket ve soğuk kontrol
sonucu kanıt indeksinde listelenir. Yeni CI dosyalarında dosya bütünlüğü kontrolü,
önceki seçili Windows graph'ında mevcut semantik kontrol uygulanır; bu işlem
Linux recovery/canlı-child başarısı veya bütün Linux ham artifact'larının
arşivlendiği anlamına gelmez. CI kabul JSON'u gömülü sonuçları içerir; orijinal
Linux Temp yolları yeniden okunmadan incelenebilir.

## Hata sınıflandırması ve kalan engel

Kanıtlanan kusur ürün veya paket kurulumunda değil, test/helper interpreter
seçimindedir. `Path.resolve()` Linux venv Python symlink'ini base Python'a
çevirmiştir. Recovery helper'ın argv'si `/opt/hostedtoolcache/Python/3.12.14/x64/bin/python3.12`
olmuş, bu ortamda `gamefactory` bulunamamıştır. Canlı-child betiğinde de aynı
çağrı-yolu sorunu vardır. Dar yerel düzeltme ve regresyon sonucu ayrıca kaydedilir.
Assertion, skip, cleanup veya güvenlik kontrolü gevşetilmez.

Kapanış için gerekli sonraki işlem: yerel helper düzeltmesini normal yetkili
commit/push akışıyla remote'a taşımak ve mevcut CI'ın o **yeni SHA** üzerindeki
recovery ve canlı-child dahil sonuçlarını incelemek. Bu ajan commit/push yapmaz.
Şimdiki SHA'nın rerun'ı yerel düzeltmeyi test etmeyeceğinden başlatılmadı.
Yeni revizyonun Windows/Linux matrix kanıtı da onunla eşleşmelidir.

## Önceki kapanışın korunması

Önceki yerel Windows kabul/recovery/canlı-child, 41→39 karşılaştırması ve
259/5/2 yerel regresyon kanıtları [mevcut indekste](v0.2-closeout/README.md)
korunur. Önceki raporun teslim anındaki kopyası
[evidence-package/closeout-report.md](v0.2-closeout/evidence-package/closeout-report.md)
içindedir. Tarihsel V0.1/V0.2 raporları ve eski başarısız koşular değiştirilmedi.

## Yerel dar düzeltme ve bağımsız lider doğrulaması

Remote'da henüz bulunmayan ilgili değişiklikler:

- `scripts/verify_godot_recovery.py`
- `scripts/verify_godot_live_crash.py`
- `tests/integration/test_verify_godot_python_resolution.py` (yeni)

İki helper, verilen Python yolunu symlink dereference etmeden mutlak yola çevirir;
hedefin varlığı/dosya niteliği denetlenir. Godot kimlik ve cleanup kontrolleri,
ürün ve workflow değişmedi. Lider incelemesinde testin POSIX venv oluştururken
`symlinks=True` kullanması ve bunu assert etmesi sağlandı (`EnvBuilder` varsayılanı
False). Beklenmeyen symlink hataları skip edilmez; yalnız Windows WinError 1314
izin eksikliği gerekçeli skip olabilir.

Son yerel Windows/Python 3.12.14 hedefli regresyonu **56 passed, 6 skipped,
1 deselected**: yeni interpreter testleri, canlı-child yardımcıları, staging ve
process runner testleri. Beş skip WinError 1314, biri POSIX platform-N/A;
real Godot testi marker ile ayrıldı. Ruff lint/format ve `git diff --check` geçti.
İlk varsayılan Temp koşusu ortam erişim hatası verdi; ayrı workspace basetemp ile
tekrar geçti. Bu sonuç Linux runtime veya yeni remote CI başarısı sayılmaz.

Tam argv/cwd/zaman/exit kayıtları, test çıktıları ve üç değişen dosyanın son SHA256
kimliği [final-ci-local-validation.json](v0.2-closeout/final-ci-local-validation.json)
içindedir. Commit/push yapılmadı. **Verification Closure: PARTIAL** kalır.

Bu paragraf `93ded41` incelemesinin o andaki sonucudur. Düzeltmenin sonraki
`fef168c` revizyonu ve run `36290153250` sonucu yukarıdaki CLOSED kaydındadır.
