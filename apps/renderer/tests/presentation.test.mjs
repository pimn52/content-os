import test from 'node:test';
import assert from 'node:assert/strict';
import {stackedComparisonLayout} from '../src/presentation.ts';

test('comparison panes touch, preserve source proportions and maximize caption-safe space', () => {
  for (const [w,h] of [[1080,1920],[720,1280]]) for (const crop of [0,.1,.25]) {
    const reserve=h*.12;
    const p=stackedComparisonLayout(w,h,16/9,crop,reserve);
    assert.equal(p.secondTop,p.firstTop+p.paneHeight);
    assert.ok(p.left>=0 && p.left+p.paneWidth<=w);
    assert.ok(p.secondTop+p.paneHeight<=h-reserve);
    assert.ok(Math.abs(p.paneWidth/p.paneHeight-(16/9)/(2*(1-crop)))<1/p.paneHeight);
    assert.ok(w-p.paneWidth<2 || h-reserve-2*p.paneHeight<2);
  }
  const p=stackedComparisonLayout(1080,1920,16/9,.1,1920*.12);
  assert.ok(2*p.paneWidth*p.paneHeight>2*737*760);
});

test('invalid geometry is rejected before rendering', () => {
  for (const args of [[1080,1920,0,0,0],[1080,1920,16/9,.8,0],[1080,1920,16/9,0,1920]])
    assert.throws(()=>stackedComparisonLayout(...args));
});
