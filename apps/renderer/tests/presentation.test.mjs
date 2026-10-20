import test from 'node:test';
import assert from 'node:assert/strict';
import {captionLayout, stackedComparisonLayout} from '../src/presentation.ts';

test('comparison panes touch, preserve source proportions and center in the measured caption-safe area', () => {
  const noCaption=captionLayout([],null,false,false,1080,true);
  assert.equal(noCaption.reserve,0);
  const oneLine=captionLayout([{start_ms:0,end_ms:1000,text:'很多人以为码率越高越清晰'}],null,true,false,1080,true);
  assert.equal(oneLine.captions[0].text.includes('\n'),false);
  assert.equal(oneLine.reserve,188);
  const twoLines=captionLayout([{start_ms:0,end_ms:1000,text:'判断画质还要结合编码方式分辨率和画面内容'}],null,true,false,1080,true);
  assert.equal(twoLines.captions[0].text.split('\n').length,2);
  const split=twoLines.captions[0].text.split('\n');
  assert.ok(Math.abs(Array.from(split[0]).length-Array.from(split[1]).length)<=1);
  assert.equal(twoLines.reserve,243);

  for (const [w,h] of [[1080,1920],[720,1280]]) for (const crop of [0,.1,.25]) {
    const reserve=captionLayout([{start_ms:0,end_ms:1000,text:'字幕'}],null,true,false,w,true).reserve;
    const p=stackedComparisonLayout(w,h,16/9,crop,reserve);
    assert.equal(p.secondTop,p.firstTop+p.paneHeight);
    assert.ok(p.left>=0 && p.left+p.paneWidth<=w);
    assert.ok(p.secondTop+p.paneHeight<=h-reserve);
    assert.ok(Math.abs(p.paneWidth/p.paneHeight-(16/9)/(2*(1-crop)))<1/p.paneHeight);
    assert.ok(w-p.paneWidth<2 || h-reserve-2*p.paneHeight<2);
  }
  const p=stackedComparisonLayout(1080,1920,16/9,70/720,oneLine.reserve);
  assert.equal(p.paneWidth,853);
  assert.equal(p.paneHeight,866);
  assert.equal(p.left,113);
  assert.equal(p.firstTop,0);
  assert.equal(p.secondTop,866);
  assert.ok(2*p.paneWidth*p.paneHeight>2*831*844);
  const uncaptained=stackedComparisonLayout(1080,1920,16/9,70/720,noCaption.reserve);
  assert.equal(uncaptained.paneHeight,960);
  assert.equal(uncaptained.firstTop,0);
  assert.equal(uncaptained.secondTop+uncaptained.paneHeight,1920);
});

test('invalid geometry is rejected before rendering', () => {
  for (const args of [[1080,1920,0,0,0],[1080,1920,16/9,.8,0],[1080,1920,16/9,0,1920]])
    assert.throws(()=>stackedComparisonLayout(...args));
});
