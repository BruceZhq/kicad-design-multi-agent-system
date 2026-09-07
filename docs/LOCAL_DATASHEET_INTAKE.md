# Local manufacturer PDF intake

This is an operator-curated shared technical library, not a tenant-private upload
API. Do not import confidential tenant documents into it. Chat/model output cannot
approve provenance. The operator must check the original manufacturer document.

From the repository root (PowerShell):

```powershell
$env:PYTHONPATH='src'
uv run python -m agents.ratsnestpro.local_datasheets ./stm32g070rb.pdf --identity STM32G070RBT6 --source-url https://www.st.com/resource/en/datasheet/stm32g070rb.pdf --approve-source
```

The workspace root defaults to `data/ratsnestpro`; Compose mounts that directory
at `/data/ratsnestpro`. Original documents and source attestations are stored in
`reference-datasheets`. Files are addressed by SHA-256; every read checks bytes.
Use the exact requested identity; there is deliberately no fuzzy part substitution.

Parts consults this registry before external web fallback and puts page evidence,
source digest and import provenance into its result and Selection handoff. CAD
preparation consults the same registry on checkpoint resume, even when Parts has
already completed. It independently validates the actual symbol/package/pins;
import approval is NOT electrical approval. Visual observations are cached by
document digest and selected asset identity. Downstream review uses the existing
prepared-component manifest and release checks, not the filename as proof.

Importing does not start a Run or modify its checkpoint. Resume the existing Run
only after checking evidence validation. Never commit the local PDF/registry data.
