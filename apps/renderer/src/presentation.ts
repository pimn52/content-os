/** Maximize adjacent comparison panes inside the actual caption-safe canvas. */
export function stackedComparisonLayout(width: number, height: number, sourceAspect: number, bottomCrop: number, captionReserve: number) {
  if (![width, height, sourceAspect, bottomCrop, captionReserve].every(Number.isFinite)
      || width <= 0 || height <= 0 || sourceAspect <= 0 || bottomCrop < 0 || bottomCrop > .4
      || captionReserve < 0 || captionReserve >= height) throw new Error('Invalid comparison geometry');
  const halfAspect = sourceAspect / (2 * (1 - bottomCrop));
  const available = height - captionReserve;
  const paneHeight = Math.floor(Math.min(available / 2, width / halfAspect));
  const paneWidth = Math.floor(paneHeight * halfAspect);
  const firstTop = Math.floor((available - 2 * paneHeight) / 2);
  return {paneWidth, paneHeight, firstTop, secondTop: firstTop + paneHeight,
    left: Math.floor((width - paneWidth) / 2), videoHeight: `${100 / (1 - bottomCrop)}%`};
}
