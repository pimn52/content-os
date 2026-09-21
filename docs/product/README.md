# Content OS — Product Module Map

This directory contains **current product specifications by module**. These files describe what the product is, what each module owns, the user-visible behavior, stable boundaries and current capability state.

They are **not** implementation journals.

## How to read

- Start with [../../CONTENT_OS_EXECUTION_SPEC.md](../../CONTENT_OS_EXECUTION_SPEC.md) for the whole-product map and R1 gate.
- Read [../../STATUS.md](../../STATUS.md) for the one active work package and current blockers.
- Open only the module spec relevant to the task:
  - [CREATOR_INTELLIGENCE.md](CREATOR_INTELLIGENCE.md)
  - [MEDIA_ASSET_SYSTEM.md](MEDIA_ASSET_SYSTEM.md)
  - [VOICE_TALKING.md](VOICE_TALKING.md)
  - [EXECUTION_COMPUTE.md](EXECUTION_COMPUTE.md)
  - [TIMELINE_RENDER_LEARNING.md](TIMELINE_RENDER_LEARNING.md)

## Content rule

A product-module spec may contain:

- product responsibility and user outcome;
- core objects and stable terminology;
- supported flows and quality gates;
- interfaces with adjacent modules;
- current capability state and known product gaps.

It must not contain:

- work-package histories;
- per-run filenames, timestamps or experiment logs;
- implementation-agent model tiers;
- long debugging narratives;
- superseded plans.

When implementation completes, update the relevant capability/state here if the product behavior changed. Keep the implementation story in Git history and evaluation evidence.

## Archive rule

**Completed is not archived.** A completed capability remains represented in the current product spec.

`docs/history/` is only for a whole document that has been superseded but still has historical reading value. It is not a dumping ground for completed tasks or experiment logs.
