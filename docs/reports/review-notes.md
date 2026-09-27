# Ekip lideri inceleme günlüğü

Bu dosya devam eden incelemenin notlarıdır; tamamlanma veya onay belgesi değildir.

## Doğrudan gözlenen ortam ve ilk kontroller

- Kaynak klasör başlangıçta boş; `git status --short` → Git deposu değil.
- Godot console `--version` → `4.7.2.stable.official.ed1daf0bf`.
- Blender `--version` → `Blender 5.2.1 LTS`.
- GPU venv Python mevcut ancak temel Python yolu kayıp; çalıştırma başarısız.
- Bundled Python → 3.12.14. Bununla ayrı `.verify-venv` oluşturuldu.
- `.verify-venv/Scripts/python.exe -m pip install -e '.[dev]'` → başarılı.
  İlk sandbox denemesinde ağ engeli oldu; izinli yeniden çalıştırma başarılı.
- Bağımsız domain smoke: fan-out/fan-in, cycle, invalid transition → PASS.
- `.verify-venv/Scripts/python.exe -m pytest tests/unit -q` → ilk dilimde 24 passed.
  Pytest ortak cache dizininde erişim uyarısı oluştu; son doğrulamada ayrı cache kullanılacak.

## Son incelemede kapatılacak sorular

1. Paket sürümü iki sabite dönüşüyor mu? `pyproject.toml` ve `__init__` tek otoriteye bağlanmalı.
2. Bütçe girdilerinde NaN/infinity/negative; METERED sınıfında onay kaçışı var mı?
3. Redaksiyonda örtüşen secret değerleri, kısa secrets, punctuation içeren password,
   Basic authorization, sonradan gelen explicit env secrets ve tuple argümanları kapsanıyor mu?
4. Yönetilen alanın kendisi symlink/junction ise PathGuard dış root'u yanlış güveniyor mu?
5. Completion geçişi yalnız state çiftine bakmak yerine evidence/gates ile doğrulanıyor mu?
6. Gerçek process timeout, crash recovery ve aynı anda çalışan iki runner test ediliyor mu?
7. Approval immutable input hash'i maliyet/sürüm/artifact hash'lerini kapsıyor mu?

Bu sorular ilk dosya incelemesinden gelir. Tam uygulama ve testler çıktıktan sonra
her biri somut repro ile değerlendirilir; doğrulanmadan kesin kusur sayılmaz.
