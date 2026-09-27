# Revizyon 01 — güvenlik ve yürütme

Durum: **CHANGES REQUIRED**, ilk bağımsız inceleme. Bunlar devam eden uygulamanın
erken bulgularıdır; son doğrulamada yeniden sınanacaktır.

1. **CRITICAL / çift çalışma:** `core/execution/locks.py` lock var mı kontrolünden
   sonra normal write yapıyor. Aynı kontrol noktasında senkronize edilen iki thread
   aynı workflow kilidini alabiliyor. Atomik cross-process OS lock ve ownership token
   kullanın; stale reclaim ve release de yarışa dayanıklı olsun. Aynı anda iki ayrı
   süreç testinde yalnız biri giriş yapabilmeli; aktif PID kurtarılmamalı.
2. **MAJOR / path:** workflow_id doğrudan lock filename içine giriyor. `../outside`
   lock dizininden çıkıyor. Kimliği doğrulayın veya güvenli sabit fingerprint kullanın.
3. **CRITICAL / ücret politikası:** `FREE_EXTERNAL + METERED + estimated_cost=1`
   onaysız izin alıyor. Metered da maliyet/onay sınıfıdır; provider beyanından bağımsız
   merkezi kontrol uygulayın.
4. **MAJOR / bütçe:** negatif/NaN/infinity tutarlar ve limitler reddedilmiyor. Tüm
   maliyet/bütçe girdileri sonlu, negatif olmayan ve doğru birimde olmalı.
5. **CRITICAL / secret:** `ProcessRunner`'a sonradan verilen
   `env_overrides={'MY_SECRET_TOKEN':'u$p:9!q+2'}` değerini yazdıran child stdout'u
   secret'ı açık döndürüyor. Her request için güncel sensitive environment ve explicit
   override değerlerini redaksiyona katın; log, exception ve persisted metadata aynı
   denetimden geçsin. Eksik executable hatasındaki `args[0]` da redakte edilmeli.
6. **MAJOR / timeout:** process timeout yalnız doğrudan child'ı öldürüyor. Çocuğun
   başlattığı grandchild timeout sonrası dosya yazmaya devam etti. Windows job/process
   tree ve POSIX process group sonlandırması ile bounded cleanup uygulayın; test edin.

Regresyon testleri mevcut normal yolları korumalı. README/ADR atomik lock, lease,
heartbeat veya exactly-once garantisi gibi uygulanmamış mekanizmaları iddia etmemeli.
