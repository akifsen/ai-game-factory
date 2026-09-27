# V0.1: 41 → 39 komut karşılaştırması

Kaynaklar: `../acceptance.json` (41), `../v0.2-v01-acceptance.json` (39).
Tam sıra eşlemesi ve kaynak hashleri: `v01-command-comparison.json`.
Geçici proje yolları ve rastgele workflow/approval kimlikleri çıkarılarak
komut fiili ve run senaryosu eşleştirildi. İlk 39 adımın tamamı aynı sırada.

| Önceki adım | Güncel karşılık | Korunan davranış | Farkın nedeni / kanıt |
|---|---|---|---|
| 1–3 | 1–3 | version/help/doctor | Aynı anlamsal komutlar |
| 4–7 | 4–7 | İki kez init, oyun dosyası hashleri, status/approvals | init_preserves_original_game_files=true |
| 8–16 | 8–16 | Approval block/approve/resume, tamamlanan resume, artifact inspection | demo_restart_approval_completion ve inspect JSON |
| 17–20 | 17–20 | Failure propagation, retry, iki attempt ve geçmiş | failure_dependency_block_retry_history |
| 21–28 | 21–28 | Fake paid çağrı sayısı 0 → 1 → 1 | Her iki checks.fake_paid_invocations aynı |
| 29–33 | 29–33 | Ret sonrası 0 çağrı | rejected_approval_never_invokes_provider; inspect çıktısı |
| 34–37 | 34–37 | Status, init, bütçe engeli | configured_budget_blocks_dispatch |
| 38–39 | 38–39 | Repository-write onayı, dispatch öncesi engel | configured_repository_write_requires_approval |
| 40 | Bu kayıtta yok | Factory olmadan Godot import | Betikte `if args.godot` koşuluna bağlı |
| 41 | Bu kayıtta yok | Factory olmadan normal Godot runtime | Aynı koşullu blok; --quit-after 2 |

39 komutluk kayıtta bağımsız Godot adımları yoktur. Kayıt üst düzey çağırıcı
argv'sini taşımadığından tarihsel çağırma niyeti tahmin edilmez; betik koşulu
ve iki gerçek kayıt farkı kesindir. V0.2 ana kabul kaydı bu invariantı
`standalone_import` ve `standalone_runtime_without_factory` ile ayrıca doğrular.
Güncel closeout V0.1 kabulü açık --godot ile yeniden yürütülür; sonuç windows-v01.json.
Sayıyı doldurmak için yeni anlamsız komut eklenmedi, betik değiştirilmedi.
