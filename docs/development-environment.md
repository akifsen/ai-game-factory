# Yerel doğrulama ortamı

- Windows / PowerShell; 2026-09-27.
- Kullanıcı tarafından belirtilen Godot dizini: `C:/Users/lenovo/devel/godot`.
- Gerçek yürütülebilir: `C:/Users/lenovo/devel/godot/Godot_v4.7.2-stable_win64.exe/Godot_v4.7.2-stable_win64_console.exe`.
- Blender: `C:/Program Files/Blender Foundation/Blender 5.2/blender.exe`.
- Çalışan Python 3.12.14: `C:/Users/lenovo/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`.
- `C:/Users/lenovo/devel/local-image-gen/sdxl-env/Scripts/python.exe` mevcut ama temel Python yolu kayıp; bu ortamı kullanmayın.
- Bu yollar yerel doğrulama içindir; ürün varsayılanlarına sabitlenmemelidir. Kullanıcının araç yolu geçersizse yapılandırma hatası raporlanmalıdır.
- Kaynak klasör başlangıçta Git deposu değildi. Git başlatılmadı; dosya incelemesi Git diff yerine kullanılacak.

## Hazır yerel ortam

Depodaki `.venv` çalışan Python ile yeniden kuruldu; `.verify-venv` bağımsız doğrulama ortamıdır.
PATH üzerinde Python bulunmadığından bu makinede komutlar doğrudan kullanılabilir:

```powershell
.\.venv\Scripts\gamefactory.exe --version
.\.venv\Scripts\gamefactory.exe doctor --godot-path 'C:\Users\lenovo\devel\godot\Godot_v4.7.2-stable_win64.exe\Godot_v4.7.2-stable_win64_console.exe'
```

Godot'un ilk editor/import çalıştırması kullanıcı önbelleğine yazdığı için kısıtlı
sandbox dışında doğrulandı. Ürün Godot çalıştırma yeteneği ilan etmez; bu komutlar
fixture oyunun Factory'den bağımsızlığını doğrulamak için kullanıldı.
