# Example configs

`sample_batch_config.json` -- a real, previously-verified property (Casa
Tatano / TS 210890) with real candidate URLs on other channels, in the
exact shape `parity batch` expects. Run it with:

    python -m parity.cli batch examples/sample_batch_config.json

Swap in your own TS listing URL and known mirror URLs for a real check.
For a human-reviewed mirror, use a `verified_against` object containing
`url`, `reviewed_by` and `note`; the sample shows the required shape.
Vrbo/Expedia and Booking.com mirrors are deliberately left out of this
sample since their exact listing IDs weren't independently re-verified for
this export -- add them once you have a licence-number-confirmed URL (see
parity-tool-complete-knowledge-export-2026-09-22.md section 4 for how).
