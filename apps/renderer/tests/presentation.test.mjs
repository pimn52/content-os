import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {captionLayout, stackedComparisonLayout} from '../src/presentation.ts';

const renderer = readFileSync(new URL('../src/video.tsx', import.meta.url), 'utf8');

test('comparison panes maximize the canvas, preserve the accepted crop ratio, and center as one group', () => {
  for (const [width, height] of [[1080, 1920], [720, 1280]]) for (const crop of [0, 70 / 720, .25]) {
    const paneAspect = (16 / 9) / (2 * (1 - crop));
    const layout = stackedComparisonLayout(width, height, 16 / 9, crop);
    assert.equal(layout.secondTop, layout.firstTop + layout.paneHeight);
    assert.equal(layout.paneHeight, Math.min(height / 2, width / paneAspect));
    assert.ok(Math.abs(layout.paneWidth / layout.paneHeight - paneAspect) < 1e-12);
    assert.equal(layout.firstTop, (height - 2 * layout.paneHeight) / 2);
    assert.equal(layout.left, (width - layout.paneWidth) / 2);
    assert.equal(layout.videoHeight, `${100 / (1 - crop)}%`);
  }

  const acceptedCrop = stackedComparisonLayout(1080, 1920, 16 / 9, 70 / 720);
  assert.ok(Math.abs(acceptedCrop.paneWidth - 945.2307692307693) < 1e-9);
  assert.equal(acceptedCrop.paneHeight, 960);
  assert.equal(acceptedCrop.firstTop, 0);
  assert.equal(acceptedCrop.secondTop, 960);
  assert.ok(Math.abs(acceptedCrop.left - 67.38461538461536) < 1e-9);
  assert.equal(1920 - acceptedCrop.secondTop - acceptedCrop.paneHeight, 0);
});

test('caption length does not participate in video geometry and remains independent from layout', () => {
  const short = captionLayout([{start_ms: 0, end_ms: 1000, text: '短字幕'}], null, true, false, 1080, true);
  const long = captionLayout([{start_ms: 0, end_ms: 1000, text: '判断画质还要结合编码方式分辨率和画面内容以及实际画面内容变化'}], null, true, false, 1080, true);
  const disabled = captionLayout([{start_ms: 0, end_ms: 1000, text: '长字幕'.repeat(30)}], null, false, false, 1080, true);
  assert.equal(short.captions.length, 1);
  assert.ok(long.captions[0].text.includes('\n'));
  assert.equal(disabled.captions[0].text, '长字幕'.repeat(30));

  const picture = stackedComparisonLayout(1080, 1920, 16 / 9, 70 / 720);
  assert.deepEqual(picture, stackedComparisonLayout(1080, 1920, 16 / 9, 70 / 720));
  assert.match(renderer, /stackedComparisonLayout\(\s*width, height, source\.sourceAspectRatio \?\? 16 \/ 9, source\.sourceBottomCropRatio \?\? 0\)/);
  assert.doesNotMatch(renderer, /captionHeight/);
  assert.match(renderer, /source\.portraitPresentation !== 'stacked_comparison'[\s\S]*: false/);
  assert.match(renderer, /scene\.subtitle_treatment !== 'none' && captionPlan\.captions\.length/);
});

test('ordinary portrait-panel footage uses centered full-canvas contain on black', () => {
  const panel = renderer.split('const PortraitPanel:', 2)[1].split('const PanelText:', 1)[0];
  assert.match(panel, /backgroundColor: '#000000'/);
  assert.match(panel, /<FullCanvasVideo source=\{source\} audioMode=\{audioMode\} \/>/);
  assert.doesNotMatch(panel, /top:\s*70|height:\s*640|borderRadius/);
  const fullCanvas = renderer.split('const FullCanvasVideo:', 2)[1].split('const SparseEmphasis:', 1)[0];
  assert.match(fullCanvas, /width: '100%', height: '100%'/);
  assert.match(fullCanvas, /objectFit: source\.verticalReframeMode === 'center_crop' \? 'cover' : 'contain'/);
  assert.match(fullCanvas, /backgroundColor: '#000000'/);

  const sourceWidth = 1280;
  const sourceHeight = 720;
  const canvasWidth = 1080;
  const canvasHeight = 1920;
  const scale = Math.min(canvasWidth / sourceWidth, canvasHeight / sourceHeight);
  const visibleWidth = sourceWidth * scale;
  const visibleHeight = sourceHeight * scale;
  const left = (canvasWidth - visibleWidth) / 2;
  const top = (canvasHeight - visibleHeight) / 2;
  assert.deepEqual({left, top, width: visibleWidth, height: visibleHeight, centerY: top + visibleHeight / 2}, {
    left: 0, top: 656.25, width: 1080, height: 607.5, centerY: 960,
  });
});

test('invalid comparison geometry is rejected before rendering', () => {
  for (const args of [[1080, 1920, 0, 0], [1080, 1920, 16 / 9, .8], [1080, 0, 16 / 9, 0]])
    assert.throws(() => stackedComparisonLayout(...args));
});
