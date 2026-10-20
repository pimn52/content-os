export const CAPTION_FONT_SIZE = 48;
export const CAPTION_LINE_HEIGHT = 1.15;
export const CAPTION_BOTTOM = 96;
export const CAPTION_SIDE_INSET = 56;
export const CAPTION_VERTICAL_PADDING = 18;
export const CAPTION_HORIZONTAL_PADDING = 24;

const fallbackCaptionMeasure = (text: string) => Array.from(text).reduce((total, character) => {
  if (/\s/.test(character)) return total + CAPTION_FONT_SIZE * 0.34;
  if (/[\u2e80-\u9fff\uac00-\ud7af\u3040-\u30ff\uff00-\uffef]/.test(character)) return total + CAPTION_FONT_SIZE;
  if (/[A-Z0-9]/.test(character)) return total + CAPTION_FONT_SIZE * 0.68;
  if (/[a-z]/.test(character)) return total + CAPTION_FONT_SIZE * 0.58;
  return total + CAPTION_FONT_SIZE * 0.45;
}, 0);

function captionTokens(text: string) {
  return text.match(/[\u2e80-\u9fff\uac00-\ud7af\u3040-\u30ff\uff00-\uffef]|[^\s\u2e80-\u9fff\uac00-\ud7af\u3040-\u30ff\uff00-\uffef]+|\s+/gu) ?? [];
}

function wrapCaption(text: string, width: number, measureText: (value: string) => number) {
  const output: string[] = [];
  for (const paragraph of text.split(/\r?\n/)) {
    let line = '';
    for (const token of captionTokens(paragraph)) {
      if (/^\s+$/.test(token)) {
        if (line && measureText(line + token) <= width) line += token;
        else if (line) { output.push(line.trimEnd()); line = ''; }
        continue;
      }
      if (!line || measureText(line + token) <= width) {
        if (measureText(token) <= width) { line += token; continue; }
      }
      if (line.trim()) output.push(line.trimEnd());
      line = '';
      for (const character of Array.from(token)) {
        if (line && measureText(line + character) > width) { output.push(line); line = ''; }
        line += character;
      }
    }
    output.push(line.trimEnd());
  }
  if (!text.includes('\n') && output.length === 2
      && /[\u2e80-\u9fff\uac00-\ud7af\u3040-\u30ff\uff00-\uffef]/.test(text)
      && !/\s/.test(text)) {
    const characters = Array.from(text);
    let best = output;
    let bestDifference = Math.abs(measureText(output[0]) - measureText(output[1]));
    for (let split = 1; split < characters.length; split++) {
      const candidate = [characters.slice(0, split).join(''), characters.slice(split).join('')];
      if (measureText(candidate[0]) > width || measureText(candidate[1]) > width) continue;
      const difference = Math.abs(measureText(candidate[0]) - measureText(candidate[1]));
      if (difference < bestDifference) { best = candidate; bestDifference = difference; }
    }
    return best.join('\n');
  }
  return output.join('\n');
}

/** Match the displayed caption wrapping and reserve only its measured occupied height. */
export function captionLayout<T extends {start_ms: number; end_ms: number; text: string}>(
  captions: T[], fallbackCaption: string | null | undefined, enabled: boolean,
  fallbackEnabled: boolean, width: number, editorial: boolean,
  measureText: (value: string) => number = fallbackCaptionMeasure,
) {
  const source = captions.length ? captions : fallbackEnabled && fallbackCaption ? [{start_ms: 0, end_ms: 0, text: fallbackCaption} as T] : [];
  if (!enabled || source.length === 0) return {captions: source, fallbackCaption: null, reserve: 0};
  const textWidth = width - CAPTION_SIDE_INSET * 2 - (editorial ? CAPTION_HORIZONTAL_PADDING * 2 : 0);
  if (textWidth <= 0) throw new Error('Invalid caption geometry');
  const wrapped = source.map(caption => ({...caption, text: wrapCaption(caption.text, textWidth, measureText)}));
  const maximumLines = Math.max(1, ...wrapped.map(caption => caption.text.split('\n').length));
  const boxHeight = maximumLines * CAPTION_FONT_SIZE * CAPTION_LINE_HEIGHT
    + (editorial ? CAPTION_VERTICAL_PADDING * 2 : 0);
  return {
    captions: captions.length ? wrapped : [],
    fallbackCaption: captions.length ? null : wrapped[0]?.text ?? null,
    reserve: Math.ceil(CAPTION_BOTTOM + boxHeight),
  };
}

/** Maximize adjacent comparison panes inside the actual caption-safe canvas. */
export function stackedComparisonLayout(width: number, height: number, sourceAspect: number, bottomCrop: number, captionReserve: number) {
  if (![width, height, sourceAspect, bottomCrop, captionReserve].every(Number.isFinite)
      || width <= 0 || height <= 0 || sourceAspect <= 0 || bottomCrop < 0 || bottomCrop > .4
      || captionReserve < 0 || captionReserve >= height) throw new Error('Invalid comparison geometry');
  const halfAspect = sourceAspect / (2 * (1 - bottomCrop));
  const available = height - captionReserve;
  const paneHeight = Math.floor(Math.min(available / 2, width / halfAspect));
  const paneWidth = Math.min(width, Math.round(paneHeight * halfAspect));
  const firstTop = Math.floor((available - 2 * paneHeight) / 2);
  return {paneWidth, paneHeight, firstTop, secondTop: firstTop + paneHeight,
    left: Math.floor((width - paneWidth) / 2), videoHeight: `${100 / (1 - bottomCrop)}%`};
}
