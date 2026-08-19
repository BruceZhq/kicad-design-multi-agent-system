# RatsNestPro embedded EDA engine

This package is the deterministic EDA execution layer used by the CircuitFoundry Agent Runtime.
It is not a standalone device-family template and it does not silently substitute a reference
board when evidence is missing.

The active path is the profile-driven 17-step pipeline in
`ratsnestpro.orchestration.pipeline`. Component identities are grounded against installed KiCad
symbol and footprint libraries; model proposals are validated by deterministic contracts before
materialization, routing, ERC/DRC, manufacturing export, and independent review.

Device selection belongs to the versioned capability profile and the user requirement. If a
requested symbol, footprint, pin mapping, datasheet, or execution capability cannot be verified,
the pipeline fails closed or asks for an approved alternative instead of generating a different
board.

The public product entry point, identity, Run/Revision lifecycle, HITL, artifacts, and deployment
instructions live in the repository root README.
