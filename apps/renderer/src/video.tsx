import React from 'react';
import {AbsoluteFill, Audio, Img, OffthreadVideo, Sequence, staticFile} from 'remotion';

type VideoScene = {
  scene_id: string;
  start_frame: number;
  duration_frames: number;
  caption?: string | null;
  captions?: Array<{start_ms: number; end_ms: number; text: string}>;
  subtitle_treatment?: 'timed_captions' | 'none';
};

type StyleTokens = {name: string; background_color: string; foreground_color: string; accent_color: string; font_family: string};
type GraphicTreatment = 'none' | 'headline' | 'key_point' | 'contrast';

type VideoSource = {kind: 'video'; src: string; trimBefore: number; trimAfter: number; verticalReframeMode?: 'contain' | 'center_crop'; portraitPresentation?: 'full_canvas' | 'portrait_panel'; sourceBottomCropRatio?: number; graphicText?: string | null; graphicTreatment?: GraphicTreatment; styleTokens?: StyleTokens; narrationSrc?: string; narrationStartFrame?: number};
type SceneSource =
  | VideoSource
  | {kind: 'image'; src: string; narrationSrc?: string; narrationStartFrame?: number}
  | {kind: 'typography'; text: string; graphicTreatment: GraphicTreatment; styleTokens: StyleTokens; narrationSrc?: string; narrationStartFrame?: number};

export type RenderProps = {
  videoSpec: {
    project_id?: string;
    format?: string;
    width: number;
    height: number;
    fps: {numerator: number; denominator: number};
    scenes: VideoScene[];
  };
  sceneSources: Record<string, SceneSource>;
  // Runtime providers are outside this renderer; attached local narration is
  // already persisted and staged by the Python boundary.
  audioMode: 'source' | 'narration' | 'master_narration';
  masterNarration?: {src: string; startFrame?: number} | null;
};

export const ContentOSVideo: React.FC<RenderProps> = ({videoSpec, sceneSources, audioMode, masterNarration}) => (
  <AbsoluteFill style={{backgroundColor: 'black'}}>
    {masterNarration ? <Audio src={staticFile(masterNarration.src)} startFrom={masterNarration.startFrame ?? 0} volume={1} /> : null}
    {videoSpec.scenes.map((scene) => {
      const source = sceneSources[scene.scene_id];
      if (!source) {
        throw new Error(`Missing staged source for scene ${scene.scene_id}`);
      }
      return (
        <Sequence key={scene.scene_id} from={scene.start_frame} durationInFrames={scene.duration_frames}>
          <AbsoluteFill style={{overflow: 'hidden'}}>
            {source.kind === 'video' && source.portraitPresentation === 'portrait_panel' ? (
              <PortraitPanel source={source} audioMode={audioMode} />
            ) : source.kind === 'video' ? (
              <OffthreadVideo
                src={staticFile(source.src)}
                trimBefore={source.trimBefore}
                trimAfter={source.trimAfter}
                volume={audioMode === 'source' ? 1 : 0}
                // Crop only when the persisted VideoSpec names an evidence-
                // backed review decision. The default preserves the full
                // frame, so the renderer never silently crops a face.
                style={{
                  width: '100%', height: '100%',
                  objectFit: source.verticalReframeMode === 'center_crop' ? 'cover' : 'contain',
                  objectPosition: source.sourceBottomCropRatio ? 'center top' : 'center',
                  transform: source.sourceBottomCropRatio ? `scale(${1 / (1 - source.sourceBottomCropRatio)})` : undefined,
                  transformOrigin: 'top center', backgroundColor: '#111',
                }}
              />
            ) : source.kind === 'image' ? (
              <Img src={staticFile(source.src)} style={{width: '100%', height: '100%', objectFit: 'contain'}} />
            ) : (
              <GraphicCard text={source.text} treatment={source.graphicTreatment} tokens={source.styleTokens} />
            )}
            {source.narrationSrc ? <Audio src={staticFile(source.narrationSrc)} startFrom={source.narrationStartFrame ?? 0} volume={1} /> : null}
            {scene.subtitle_treatment !== 'none' && scene.captions?.length ? scene.captions.map((caption) => {
              const startFrame = Math.max(0, Math.round(caption.start_ms * videoSpec.fps.numerator / (1000 * videoSpec.fps.denominator)));
              const endFrame = Math.max(startFrame + 1, Math.round(caption.end_ms * videoSpec.fps.numerator / (1000 * videoSpec.fps.denominator)));
              return <Sequence key={`${caption.start_ms}-${caption.end_ms}-${caption.text}`} from={startFrame} durationInFrames={endFrame - startFrame}><Caption text={caption.text} /></Sequence>;
            }) : scene.subtitle_treatment !== 'none' && scene.caption && audioMode !== 'source' ? <Caption text={scene.caption} /> : null}
          </AbsoluteFill>
        </Sequence>
      );
    })}
  </AbsoluteFill>
);

const GraphicCard: React.FC<{text: string; treatment: GraphicTreatment; tokens: StyleTokens}> = ({text, treatment, tokens}) => {
  const base: React.CSSProperties = {
    backgroundColor: tokens.background_color, color: tokens.foreground_color, fontFamily: tokens.font_family,
    padding: 90, display: 'flex', minHeight: '100%', whiteSpace: 'pre-line', overflowWrap: 'anywhere',
  };
  if (treatment === 'headline') {
    return <AbsoluteFill style={{...base, alignItems: 'flex-end', justifyContent: 'flex-start'}}><div style={{borderLeft: `14px solid ${tokens.accent_color}`, paddingLeft: 28, fontSize: 84, fontWeight: 800, lineHeight: 1.04, maxWidth: '92%'}}>{text}</div></AbsoluteFill>;
  }
  if (treatment === 'contrast') {
    return <AbsoluteFill style={{...base, alignItems: 'center', justifyContent: 'center'}}><div style={{borderTop: `5px solid ${tokens.accent_color}`, borderBottom: `5px solid ${tokens.accent_color}`, padding: '36px 0', fontSize: 64, fontWeight: 750, lineHeight: 1.2, textAlign: 'center', width: '100%'}}>{text}</div></AbsoluteFill>;
  }
  return <AbsoluteFill style={{...base, alignItems: 'center', justifyContent: 'center'}}><div style={{color: tokens.accent_color, fontSize: 70, fontWeight: 750, lineHeight: 1.16, textAlign: 'center', maxWidth: '92%'}}>{text}</div></AbsoluteFill>;
};

const PortraitPanel: React.FC<{source: VideoSource; audioMode: RenderProps['audioMode']}> = ({source, audioMode}) => {
  const tokens = source.styleTokens ?? {name: 'content-os-dark', background_color: '#172033', foreground_color: '#f8fbff', accent_color: '#65d3b4', font_family: 'Inter, ui-sans-serif, system-ui'};
  return <AbsoluteFill style={{backgroundColor: tokens.background_color, color: tokens.foreground_color, fontFamily: tokens.font_family, padding: 44}}>
    <div style={{position: 'absolute', top: 70, left: 44, right: 44, height: 640, backgroundColor: '#05070c', border: `2px solid ${tokens.accent_color}`, borderRadius: 24, overflow: 'hidden'}}>
      <OffthreadVideo
        src={staticFile(source.src)} trimBefore={source.trimBefore} trimAfter={source.trimAfter}
        volume={audioMode === 'source' ? 1 : 0}
        style={{width: '100%', height: '100%', objectFit: 'contain', backgroundColor: '#05070c'}}
      />
    </div>
    {source.graphicText && source.graphicTreatment !== 'none' ? <PanelText text={source.graphicText} treatment={source.graphicTreatment} tokens={tokens} /> : null}
  </AbsoluteFill>;
};

const PanelText: React.FC<{text: string; treatment: GraphicTreatment; tokens: StyleTokens}> = ({text, treatment, tokens}) => {
  const base: React.CSSProperties = {position: 'absolute', top: 810, left: 70, right: 70, fontFamily: tokens.font_family};
  if (treatment === 'headline') return <div style={{...base, borderLeft: `12px solid ${tokens.accent_color}`, paddingLeft: 24, fontSize: 78, fontWeight: 800, lineHeight: 1.06}}>{text}</div>;
  if (treatment === 'contrast') return <div style={{...base, borderTop: `4px solid ${tokens.accent_color}`, borderBottom: `4px solid ${tokens.accent_color}`, padding: '28px 0', textAlign: 'center', fontSize: 58, fontWeight: 750, lineHeight: 1.2}}>{text}</div>;
  return <div style={{...base, color: tokens.accent_color, textAlign: 'center', fontSize: 64, fontWeight: 750, lineHeight: 1.16}}>{text}</div>;
};

const Caption: React.FC<{text: string}> = ({text}) => (
  <div style={{position: 'absolute', bottom: 96, left: 56, right: 56, color: 'white', fontSize: 48, fontWeight: 700, textAlign: 'center', textShadow: '0 2px 8px black'}}>
    {text}
  </div>
);
