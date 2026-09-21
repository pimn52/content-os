# Media, Asset Intelligence and Hybrid Asset Routing

## Product responsibility

Turn creator-owned media into reusable, searchable production assets and choose the lowest-cost adequate visual route for each content need.

## User outcome

The creator uploads or imports media once. Content OS should preserve the original media, derive reusable clips and metadata, and avoid forcing repeated manual cutting for every new video.

## Core objects

- Asset
- Clip
- AudioAsset
- analysis / transcript evidence
- Candidate
- ShootTask
- Asset usage evidence

## Media model

Original video remains continuous. Clips are first-class playback intervals. Keyframes and derived observations support understanding/search; they do not replace the source video.

Where supported, one import may derive:

- metadata / ffprobe evidence;
- audio;
- transcript;
- clip boundaries;
- keyframes / visual observations;
- search/index inputs.

## Hybrid Asset Router

The Asset Router answers **what visual/content route should satisfy a scene**. It does not choose the compute provider.

### TALKING preference

1. original creator Talking when the original words already match;
2. authorized generated/lip-synced creator Talking;
3. authorized digital twin/avatar only as explicit fallback;
4. capture gap when a small new recording is cheaper or safer.

### B-roll preference

1. creator / historical production-authorized media;
2. low-friction capture;
3. screenshot / static / typography;
4. stock when supported;
5. AI image/video only when justified, supported and budgeted.

## Capture / Shoot List

A missing visual may create an optional, concrete capture task. Capture is part of the asset strategy, not a failure mode.

## Product boundaries

- Asset ranking must retain provenance.
- Unknown price is not treated as zero.
- Real creator media is preferred when it clears the quality threshold.
- Generated Talking assets must pass required QA/human gates before normal assembly.
- A future provider change must not require changing ScenePlan semantics.

## Current capability

Local import, hash dedupe, continuous Clips, transcript/analysis boundaries, candidate routing, optional Shoot Tasks, usage records and real-media-first selection are implemented foundations.

Current productization work is making reviewed generated Talking runs become normal first-class Assets/Clips that the same router and VideoSpec pipeline can consume.
