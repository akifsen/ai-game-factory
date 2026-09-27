# AI GAME FACTORY — V0.2 FINAL CI CLOSURE

## Verification Closure: PARTIAL

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
