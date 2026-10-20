import test from 'node:test';
import assert from 'node:assert/strict';
import {captionLayout, stackedComparisonLayout} from '../src/presentation.ts';

test('comparison panes touch, preserve source proportions and fill the canvas height', () => {
  const noCaption=captionLayout([],null,false,false,1080,true);
  assert.equal(noCaption.captions.length,0);
  const oneLine=captionLayout([{start_ms:0,end_ms:1000,text:'很多人以为码率越高越清晰'}],null,true,false,1080,true);
  assert.equal(oneLine.captions[0].text.includes('\n'),false);
  const twoLines=captionLayout([{start_ms:0,end_ms:1000,text:'判断画质还要结合编码方式分辨率和画面内容'}],null,true,false,1080,true);
  assert.equal(twoLines.captions[0].text.split('\n').length,2);
  const split=twoLines.captions[0].text.split('\n');
  assert.ok(Math.abs(Array.from(split[0]).length-Array.from(split[1]).length)<=1);

  for (const [w,h] of [[1080,1920],[720,1280]]) for (const crop of [0,.1,.25]) {
    const p=stackedComparisonLayout(w,h,16/9,crop);
    assert.equal(p.secondTop,p.firstTop+p.paneHeight);
    assert.ok(p.left>=0 && p.left+p.paneWidth<=w);
    assert.ok(p.secondTop+p.paneHeight<=h);
    assert.ok(Math.abs(p.paneWidth/p.paneHeight-(16/9)/(2*(1-crop)))<1/p.paneHeight);
    assert.ok(w-p.paneWidth<2 || h-2*p.paneHeight<2);
  }
  const p=stackedComparisonLayout(1080,1920,16/9,70/720);
  assert.equal(p.paneWidth,945);
  assert.equal(p.paneHeight,960);
  assert.equal(p.left,67);
  assert.equal(p.firstTop,0);
  assert.equal(p.secondTop,960);
  assert.equal(p.secondTop+p.paneHeight,1920);
  assert.equal(1080-p.left-p.paneWidth,68);
  assert.equal(100*2*p.paneWidth*p.paneHeight/(1080*1920),87.5);
  assert.ok(100*2*p.paneWidth*p.paneHeight/(1080*1920)>71.25);
});

test('invalid geometry is rejected before rendering', () => {
  for (const args of [[1080,1920,0,0],[1080,1920,16/9,.8],[1080,0,16/9,0]])
    assert.throws(()=>stackedComparisonLayout(...args));
});
