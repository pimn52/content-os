# Content OS — Start Here

This is the single navigation entry for humans and implementation agents.

## 1. Understand the whole product first

Read in this order:

1. [CONTENT_OS_EXECUTION_SPEC.md](CONTENT_OS_EXECUTION_SPEC.md) — whole-product map, R1 scope and acceptance gate.
2. [docs/product/README.md](docs/product/README.md) — product module index; open only the module relevant to the task.
3. [docs/architecture/SYSTEM_ARCHITECTURE.md](docs/architecture/SYSTEM_ARCHITECTURE.md) — stable technical layers and first-class object boundaries.
4. [STATUS.md](STATUS.md) — current facts, the one active work package, blockers and short queue.
5. [AGENTS.md](AGENTS.md) — implementation rules, model-cost policy and work-package lifecycle.
6. [DECISIONS.md](DECISIONS.md) — durable choices only, when rationale is needed.
7. [README.md](README.md) — developer setup and repository quick start.

Do not start from old reviews, experiment logs or historical plans.

## 2. Product map

    Creator / IP / Intelligence
                |
          Project / ScenePlan
                |
         Media / Asset System
                |
       Hybrid Asset Router
                |
     Execution / Compute Router
           /           \
        Voice        Talking
           \           /
         Master Narration
          + TalkingRun
                |
             VideoSpec
                |
              Render
                |
      Publication / Feedback

Detailed current behavior belongs in the product module specs, not in this navigation file.

## 3. Current product modules

- [Creator, IP and Intelligence](docs/product/CREATOR_INTELLIGENCE.md)
- [Media, Asset Intelligence and Hybrid Routing](docs/product/MEDIA_ASSET_SYSTEM.md)
- [Creator Voice and Talking](docs/product/VOICE_TALKING.md)
- [Execution Planner and Hybrid Compute](docs/product/EXECUTION_COMPUTE.md)
- [Timeline, Render, Review and Learning](docs/product/TIMELINE_RENDER_LEARNING.md)

## 4. Documentation ownership

| Document | Owns | Must not become |
|---|---|---|
| Execution Spec | product whole, R1 contract, module map | implementation diary |
| Product module specs | current product functions and boundaries | experiment log |
| System Architecture | stable technical layers/objects | current task list |
| STATUS | current truth + one active package + short queue | history archive |
| DECISIONS | durable decisions and rationale | duplicate product spec |
| AGENTS | implementation/process/model policy | product PRD |
| README | setup/contributor entry | governance source |

**Completed is not archived.** When a completed implementation changes the product, update the relevant current product/module spec. The step-by-step implementation history remains in Git/evaluation evidence.

docs/history/ is reserved only for a whole superseded document that still has historical reading value.

## 5. Task handoff

For implementation work:

1. read this file;
2. read the whole-product spec;
3. read STATUS;
4. read only the relevant product module and architecture section;
5. read AGENTS for execution rules;
6. inspect the relevant code/tests.

Do not load every old work package into context.

Before handoff, run:

    python scripts/check_docs.py
