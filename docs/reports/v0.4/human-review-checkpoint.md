# Gerçek concept — insan inceleme checkpoint'i

Durum: `CONCEPT_APPROVAL_REQUIRED` (authoritative workflow: `BLOCKED`).
Concept insan tarafından henüz onaylanmadı; ücretli approval kaydı henüz açılmadı.
Şartnamenin 7, 44 ve 56. bölümleri gereği bu iki karar birleştirilmez.

| Alan | Değer |
|---|---|
| Asset | `prop_energy_crate_01` |
| Revision | `r001` |
| Workflow | `WF-ASSET-96f68a4f` |
| Concept approval | `APP-9878c627` — PENDING |
| Concept SHA-256 | `be3e9cf1a190fdfb31be85580508f73397217f399c553bd34c18924adc30e25d` |
| Specification fingerprint | `42f1a38e28c7b95e36505e47318fb4ed116ca723a67d46c3e895e750ea99e432` |
| Provider / operation | Meshy / image-to-3d |
| Generation estimate | `UNKNOWN` |
| Configured reservation ceiling | 50 credits; policy ceiling only, neither estimate nor spending approval |
| Provider availability | Local CLI 0.4.0 and credential source verified; see `meshy-adapter-doctor.json` |
| Actual paid invocations | **0** |

Project: `C:/Users/lenovo/devel/ai-game-factory/.verification/v04-human-review`.
Retained concept: `.gamefactory/assets/prop_energy_crate_01/r001/concept.png`.
Portable review copy: [concept PNG](concept/prop_energy_crate_01.png).
State evidence: [inspect JSON](human-review-inspect.json).

After the human explicitly approves this concept, record that decision with the
operator's name, then resume to reach the **separate paid approval checkpoint**:

```powershell
& 'C:/Users/lenovo/devel/ai-game-factory/.verification/v04-wheel-env/Scripts/gamefactory.exe' --project 'C:/Users/lenovo/devel/ai-game-factory/.verification/v04-human-review' approve APP-9878c627 --actor '<human reviewer>' --comment 'Concept reviewed and approved'
& 'C:/Users/lenovo/devel/ai-game-factory/.verification/v04-wheel-env/Scripts/gamefactory.exe' --project 'C:/Users/lenovo/devel/ai-game-factory/.verification/v04-human-review' resume WF-ASSET-96f68a4f
```

These commands are documented, **not executed**. The first is a human decision;
the second cannot generate while the separate paid approval remains pending.
Do not approve that paid operation within this sprint's autonomous work. No final
production visual approval is claimed. Offline fake-provider test decisions do
not apply to this workflow.
