# Content OS — Product Module Map

This directory contains the current product specifications by module. These files describe what the product is, what each module owns, the user-visible behavior, stable boundaries and current capability state.

They are not implementation journals.

## 1. Whole-product functional map

| Module | Core capabilities | Specification |
|---|---|---|
| Creator / IP / Intelligence | IP Profile, revisions, sourced topics, historical account/content evidence, feedback loop | CREATOR_INTELLIGENCE.md |
| Media / Asset Intelligence | import, continuous Clips, transcript/analysis evidence, Hybrid Asset Router, Shoot Tasks, usage evidence | MEDIA_ASSET_SYSTEM.md |
| Voice / Talking | Voice Profile, Voice QA, MasterNarration creation, Talking Profile, short-slice execution, continuous TalkingRun, terminal closeout | VOICE_TALKING.md |
| Execution / Hybrid Compute | capability profiles, provider/runtime/machine evidence, parameter resolution, Advanced Settings, local resource guards, future remote routing | EXECUTION_COMPUTE.md |
| Timeline / Render / Learning | MasterNarration timeline use, EditPlan/visual direction, VideoSpec, subtitles, render, review, publication and feedback | TIMELINE_RENDER_LEARNING.md |

## 2. Product flow

    Creator context + topic + authorized media
              ↓
    Editable copy / ScenePlan / preliminary EditPlan
              ↓
    Suitability / asset routing / execution and cost preflight
              ↓
    Required production → MasterNarration / TalkingRun / Assets
              ↓
    Measured timing → resolved EditPlan → VideoSpec
              ↓
    Render / technical QA / bounded repair / human review
              ↓
    Explicit publication / feedback → next planning input

This is the target flow. Module sections distinguish implemented contracts, runtime evidence and missing normal-flow automation. A presentation default is not automatic visual planning.

## 3. How to read

- Start with ../../CONTENT_OS_EXECUTION_SPEC.md for the whole-product R1 contract.
- Read ../../STATUS.md only for current progress and the one active package.
- Open the module spec relevant to the task.
- Read ../architecture/SYSTEM_ARCHITECTURE.md when object/layer ownership matters.
- Read ../../AGENTS.md only when implementing.
- Read ../implementation/ROADMAP.md for the selected future package contract; STATUS alone owns live package state.

## 4. What belongs in a module spec

A product-module spec may contain:

- product responsibility and user outcome;
- feature/function taxonomy;
- core product objects and stable terminology;
- supported product flows;
- quality/admission gates;
- interfaces with adjacent modules;
- current capability state and known product gaps.

It must not contain:

- work-package histories;
- per-run filenames, timestamps or debug transcripts;
- implementation-agent model tiers;
- step-by-step implementation notes;
- superseded plans.

## 5. Completion and archive semantics

**Completed is not archived.**

When implementation completes:

- if the product behavior changed, update the relevant module spec so the current product remains discoverable;
- remove the closed task from STATUS;
- keep detailed implementation history in Git and local evaluation evidence.

docs/history/ is only for a whole document that has been superseded but still has historical reading value. It is not a completed-task warehouse.

## 6. Change routing

Use this rule when deciding where a change belongs:

- product scope / R1 acceptance changed → CONTENT_OS_EXECUTION_SPEC.md;
- module behavior / function changed → the relevant docs/product spec;
- technical layer/object ownership changed → SYSTEM_ARCHITECTURE.md;
- durable cross-package rationale changed → DECISIONS.md;
- current task/progress changed → STATUS.md;
- future package dependencies/acceptance changed → docs/implementation/ROADMAP.md;
- implementation/model/process rule changed → AGENTS.md.
