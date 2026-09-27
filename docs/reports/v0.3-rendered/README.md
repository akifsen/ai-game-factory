# V0.3 Windows review package

These files are copies of a real Godot viewport capture. Human review of this package is **PENDING**.

- Landscape review: [index.html](landscape/index.html)
- First frame: [images/cp-000.png](landscape/images/cp-000.png) (HP 100, 3 enemies, score 0)
- Last frame: [images/cp-090.png](landscape/images/cp-090.png) (HP 40, 2 enemies, score 100)
- Portrait review: [index.html](portrait/index.html)

The workflow that produced the landscape page is still blocked in `.verification/v03-capture/landscape`:

```text
WF-CAPTURE-09ce2ff8
APP-ab0d79da
```

```bash
gamefactory --project .verification/v03-capture/landscape approvals --workflow WF-CAPTURE-09ce2ff8
gamefactory --project .verification/v03-capture/landscape approve APP-ab0d79da --comment "Görseller incelendi"
gamefactory --project .verification/v03-capture/landscape resume WF-CAPTURE-09ce2ff8
```

Approving that workflow does not rewrite this HTML. A separate test-actor approval was recorded on a different workflow and is not this review.
