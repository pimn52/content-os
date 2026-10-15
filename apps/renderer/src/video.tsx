import React from 'react';
import {AbsoluteFill, Audio, Img, OffthreadVideo, Sequence, staticFile, useCurrentFrame} from 'remotion';

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
type PresentationCue = {startFrame: number; durationFrames: number; label: string; title: string; treatment: GraphicTreatment; text: string; stepFrames?: number[]};

type VideoSource = {kind: 'video'; src: string; trimBefore: number; trimAfter: number; verticalReframeMode?: 'contain' | 'center_crop' | 'bottom_crop_contain'; portraitPresentation?: 'full_canvas' | 'portrait_panel'; sourceBottomCropRatio?: number; graphicText?: string | null; graphicTreatment?: GraphicTreatment; styleTokens?: StyleTokens; narrationSrc?: string; narrationStartFrame?: number; presentationCues?: PresentationCue[]};
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
              <GraphicCard text={source.text} treatment={source.graphicTreatment} tokens={source.styleTokens} durationFrames={scene.duration_frames} />
            )}
            {source.kind === 'video' && source.portraitPresentation === 'full_canvas' && source.styleTokens?.name === 'content-os-subject-first' && source.presentationCues?.length ? <SparseEmphasis cues={source.presentationCues} tokens={source.styleTokens} /> : null}
            {source.narrationSrc ? <Audio src={staticFile(source.narrationSrc)} startFrom={source.narrationStartFrame ?? 0} volume={1} /> : null}
            {scene.subtitle_treatment !== 'none' && scene.captions?.length ? scene.captions.map((caption) => {
              const startFrame = Math.max(0, Math.round(caption.start_ms * videoSpec.fps.numerator / (1000 * videoSpec.fps.denominator)));
              const endFrame = Math.max(startFrame + 1, Math.round(caption.end_ms * videoSpec.fps.numerator / (1000 * videoSpec.fps.denominator)));
              return <Sequence key={`${caption.start_ms}-${caption.end_ms}-${caption.text}`} from={startFrame} durationInFrames={endFrame - startFrame}><Caption text={caption.text} editorial={'styleTokens' in source && ['content-os-editorial', 'content-os-subject-first'].includes(source.styleTokens?.name ?? '')} /></Sequence>;
            }) : scene.subtitle_treatment !== 'none' && scene.caption && audioMode !== 'source' ? <Caption text={scene.caption} editorial={'styleTokens' in source && ['content-os-editorial', 'content-os-subject-first'].includes(source.styleTokens?.name ?? '')} /> : null}
          </AbsoluteFill>
        </Sequence>
      );
    })}
  </AbsoluteFill>
);

// Opt-in assisted square layout: text occupies only the unused upper margin.
// The source remains centered at its original size; gaps have no callout.
const SparseEmphasis: React.FC<{cues: PresentationCue[]; tokens: StyleTokens}> = ({cues, tokens}) => {
  const frame = useCurrentFrame();
  const cue = cues.find(item => frame >= item.startFrame && frame < item.startFrame + item.durationFrames);
  if (!cue) return null;
  const opacity = Math.min(1, (frame - cue.startFrame + 1) / 8, (cue.startFrame + cue.durationFrames - frame) / 8);
  return <div style={{position: 'absolute', top: 110, left: 72, right: 72, height: 220, display: 'flex', flexDirection: 'column', justifyContent: 'center', opacity, color: '#f8fbff', fontFamily: tokens.font_family, textAlign: 'center'}}>
    <div style={{fontSize: 76, fontWeight: 850, lineHeight: 1.2, whiteSpace: 'pre-line'}}>{cue.title}</div>
    <div style={{width: 72, height: 5, backgroundColor: tokens.accent_color, margin: '24px auto 0'}} />
  </div>;
};

const GraphicCard: React.FC<{text: string; treatment: GraphicTreatment; tokens: StyleTokens; durationFrames: number}> = ({text, treatment, tokens, durationFrames}) => {
  if (treatment === 'none' && !text) return <AbsoluteFill style={{backgroundColor: tokens.background_color}} />;
  if (tokens.name === 'content-os-editorial-ending') return <EditorialFrame tokens={tokens} label="04 / 留下判断"><div style={{position: 'absolute', top: 190, left: 64, right: 64, fontSize: 83, fontWeight: 900, lineHeight: 1.22, whiteSpace: 'pre-line'}}>让观众带走{'\n'}完整判断</div><PanelBody text={text} treatment="contrast" tokens={tokens} frame={0} durationFrames={durationFrames} /></EditorialFrame>;
  if (tokens.name === 'content-os-editorial') return <EditorialGraphic text={text} treatment={treatment} tokens={tokens} durationFrames={durationFrames} />;
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
  if (tokens.name === 'content-os-subject-first') return <SubjectFirstPanel source={source} audioMode={audioMode} tokens={tokens} />;
  if (tokens.name === 'content-os-editorial') return <EditorialPanel source={source} audioMode={audioMode} tokens={tokens} />;
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

// A tall viewport accommodates independently cropped9:12/9:14 footage. Contain
// preserves its actual aspect; this layout never invents a safe crop or tracker.
const SubjectFirstPanel: React.FC<{source: VideoSource; audioMode: RenderProps['audioMode']; tokens: StyleTokens}> = ({source, audioMode, tokens}) => (
  <AbsoluteFill style={{backgroundColor: tokens.background_color, color: tokens.foreground_color, fontFamily: tokens.font_family}}>
    {source.graphicText && source.graphicTreatment !== 'none' ? <div style={{position: 'absolute', top: 48, left: 64, right: 64, fontSize: 52, fontWeight: 750, lineHeight: 1.25, whiteSpace: 'pre-line'}}>{source.graphicText}</div> : null}
    <div style={{position: 'absolute', top: 220, left: 40, right: 40, height: 1400, overflow: 'hidden', borderRadius: 18}}>
      <OffthreadVideo src={staticFile(source.src)} trimBefore={source.trimBefore} trimAfter={source.trimAfter} volume={audioMode === 'source' ? 1 : 0} style={{width: '100%', height: '100%', objectFit: 'contain'}} />
    </div>
  </AbsoluteFill>
);

const Caption: React.FC<{text: string; editorial?: boolean}> = ({text, editorial}) => (
  <div style={{position: 'absolute', bottom: 96, left: 56, right: 56, color: 'white', ...(editorial ? {backgroundColor: '#172033', borderRadius: 14, padding: '18px 24px'} : {}), fontSize: 48, fontWeight: 700, textAlign: 'center', textShadow: '0 2px 8px black'}}>
    {text}
  </div>
);

// Explicit editorial profile: three semantic templates share the same rhythm.
// Provider calls and face detection remain outside this composition.
const EditorialFrame: React.FC<{tokens: StyleTokens; label: string; children: React.ReactNode}> = ({tokens, label, children}) => (
  <AbsoluteFill style={{backgroundColor: tokens.background_color, color: tokens.foreground_color, fontFamily: tokens.font_family}}>
    <div style={{position: 'absolute', top: 64, left: 64, right: 64, display: 'flex', justifyContent: 'space-between', fontSize: 25, letterSpacing: 4, fontWeight: 700}}><span>剪辑的判断</span><span style={{color: tokens.accent_color}}>{label}</span></div>
    <div style={{position: 'absolute', left: 64, right: 64, top: 120, height: 2, background: tokens.foreground_color, opacity: .2}} />
    {children}
    <div style={{position: 'absolute', bottom: 44, left: 64, fontSize: 24, letterSpacing: 3, opacity: .55}}>短视频 · 长观点</div>
  </AbsoluteFill>
);

const EditorialPanel: React.FC<{source: VideoSource; audioMode: RenderProps['audioMode']; tokens: StyleTokens}> = ({source, audioMode, tokens}) => {
  const frame = useCurrentFrame();
  const cue = source.presentationCues?.find((item) => frame >= item.startFrame && frame < item.startFrame + item.durationFrames);
  const cueFrame = frame - (cue?.startFrame ?? 0);
  const progress = Math.min(1, cueFrame / 12);
  const endFrame = source.presentationCues?.at(-1);
  const videoOpacity = endFrame ? Math.min(1, Math.max(0, (endFrame.startFrame + endFrame.durationFrames - frame - 1) / 8)) : 1;
  return <EditorialFrame tokens={tokens} label={cue?.label ?? (source.trimBefore === 0 ? '01 / 保住原意' : '03 / 对内容负责')}>
    <div style={{position: 'absolute', top: 190, left: 64, right: 64, fontSize: 83, fontWeight: 900, lineHeight: 1.22, whiteSpace: 'pre-line', opacity: progress, transform: `translateY(${(1 - progress) * 20}px)`}}>{cue?.title ?? source.graphicText}</div>
    <div style={{position: 'absolute', top: 500, left: 40, right: 40, height: 478, borderRadius: 22, overflow: 'hidden', background: '#111', boxShadow: '0 20px 45px #17203325', opacity: videoOpacity}}>
      <OffthreadVideo src={staticFile(source.src)} trimBefore={source.trimBefore} trimAfter={source.trimAfter} volume={audioMode === 'source' ? 1 : 0} style={{width: '100%', height: '100%', objectFit: 'contain'}} />
    </div>
    {cue ? <PanelBody text={cue.text} treatment={cue.treatment} tokens={tokens} frame={cueFrame} durationFrames={cue.durationFrames} stepFrames={cue.stepFrames} /> : <>
    <div style={{position: 'absolute', top: 1040, left: 64, right: 64, borderLeft: `7px solid ${tokens.accent_color}`, paddingLeft: 30}}>
      <div style={{fontSize: 27, color: tokens.accent_color, letterSpacing: 4, marginBottom: 25}}>剪辑原则</div>
      <div style={{fontSize: 51, fontWeight: 700, lineHeight: 1.55}}>观点独立成立<br />证据留在语境里</div>
    </div>
    <div style={{position: 'absolute', top: 1360, left: 64, right: 64, display: 'flex', gap: 18}}>{['问题', '证据', '语境'].map((word, i) => <div key={word} style={{flex: 1, padding: '28px 12px', borderRadius: 16, background: '#e6e0d5', textAlign: 'center', fontSize: 36}}><span style={{color: tokens.accent_color, fontSize: 22}}>0{i + 1}　</span>{word}</div>)}</div></>}
  </EditorialFrame>;
};

const PanelBody: React.FC<{text: string; treatment: GraphicTreatment; tokens: StyleTokens; frame: number; durationFrames: number; stepFrames?: number[]}> = ({text, treatment, tokens, frame, durationFrames, stepFrames}) => {
  if (treatment === 'key_point') {
    const words = text.split('·').map((word) => word.trim());
    const active = stepFrames ? Math.max(0, stepFrames.filter((start) => start <= frame).length - 1) : Math.min(words.length - 1, Math.floor(frame / Math.max(1, durationFrames / words.length)));
    return <div style={{position: 'absolute', top: 1040, left: 64, right: 64}}>{words.map((word, i) => <div key={i} style={{display: 'flex', alignItems: 'center', gap: 30, marginBottom: 22, padding: '24px 32px', borderRadius: 16, background: active === i ? tokens.foreground_color : '#e6e0d5', color: active === i ? tokens.background_color : tokens.foreground_color}}><span style={{fontSize: 31, color: tokens.accent_color, fontWeight: 800}}>0{i + 1}</span><span style={{fontSize: 49, fontWeight: 800}}>{word}</span><span style={{marginLeft: 'auto', fontSize: 26, opacity: .7}}>{['抛给观众', '支撑答案', '交代背景'][i] ?? ''}</span></div>)}</div>;
  }
  if (treatment === 'contrast') {
    const parts = text.split('\n');
    return <div style={{position: 'absolute', top: 1070, left: 64, right: 64, display: 'flex', gap: 24}}>{parts.map((part, i) => <div key={i} style={{flex: 1, padding: '48px 24px', borderRadius: 20, background: i === 0 ? tokens.foreground_color : '#e6e0d5', color: i === 0 ? tokens.background_color : tokens.foreground_color}}><div style={{fontSize: 27, color: tokens.accent_color, marginBottom: 28}}>{i === 0 ? '应该留下' : '而不是'}</div><div style={{fontSize: 62, fontWeight: 900, lineHeight: 1.35}}>{part}</div></div>)}</div>;
  }
  return <div style={{position: 'absolute', top: 1060, left: 64, right: 64, borderLeft: `7px solid ${tokens.accent_color}`, paddingLeft: 32}}><div style={{fontSize: 27, color: tokens.accent_color, letterSpacing: 4, marginBottom: 30}}>剪辑原则</div><div style={{fontSize: 57, fontWeight: 700, lineHeight: 1.65, whiteSpace: 'pre-line'}}>{text}</div><div style={{marginTop: 60, fontSize: 30, opacity: .6}}>让一句话，仍然代表原来的意思。</div></div>;
};

const EditorialGraphic: React.FC<{text: string; treatment: GraphicTreatment; tokens: StyleTokens; durationFrames: number}> = ({text, treatment, tokens, durationFrames}) => {
  const frame = useCurrentFrame();
  // Sequence duration is provided by the owning scene, so progression stays
  // local and deterministic rather than depending on wall-clock time.
  const active = Math.min(2, Math.floor(frame / Math.max(1, durationFrames / 3)));
  if (treatment === 'key_point') {
    const labels = text.split('·').map((part) => part.trim());
    const details = ['让观众知道在回答什么', '只保留最有力的支撑', '交代观点从哪里来'];
    return <EditorialFrame tokens={tokens} label="02 / 三步结构">
      <div style={{position: 'absolute', top: 215, left: 64, fontSize: 84, fontWeight: 900, lineHeight: 1.3}}>一句话很炸？<br /><span style={{color: tokens.accent_color}}>先让答案成立。</span></div>
      <div style={{position: 'absolute', top: 575, left: 64, right: 64}}>{labels.map((label, i) => <div key={i} style={{display: 'flex', alignItems: 'center', gap: 30, padding: '45px 30px', marginBottom: 28, borderRadius: 18, background: i === active ? tokens.foreground_color : '#e6e0d5', color: i === active ? tokens.background_color : tokens.foreground_color}}><div style={{fontSize: 61, color: tokens.accent_color, fontWeight: 800}}>0{i + 1}</div><div><div style={{fontSize: 58, fontWeight: 800}}>{label}</div><div style={{fontSize: 30, marginTop: 12, opacity: .8}}>{details[i] ?? ''}</div></div></div>)}</div>
    </EditorialFrame>;
  }
  if (treatment === 'contrast') {
    const parts = text.split('\n');
    return <EditorialFrame tokens={tokens} label="04 / 留下判断">
      <div style={{position: 'absolute', top: 220, left: 64, fontSize: 68, fontWeight: 800}}>观众应该带走什么？</div>
      <div style={{position: 'absolute', top: 450, left: 64, right: 64, padding: 55, background: tokens.foreground_color, color: tokens.background_color, borderRadius: 24}}><div style={{fontSize: 30, color: tokens.accent_color, marginBottom: 30}}>留下</div><div style={{fontSize: 98, fontWeight: 900, lineHeight: 1.3}}>{parts[0]}</div><div style={{fontSize: 34, marginTop: 35, opacity: .8}}>有问题 · 有依据 · 有语境</div></div>
      <div style={{position: 'absolute', top: 1090, left: 64, right: 64, borderTop: '2px solid #17203330', paddingTop: 45}}><div style={{fontSize: 29, opacity: .55}}>而不是</div><div style={{fontSize: 73, fontWeight: 700, marginTop: 25, opacity: .55}}>{parts.slice(1).join('\n')}</div></div>
    </EditorialFrame>;
  }
  return <EditorialFrame tokens={tokens} label="观点"><div style={{position: 'absolute', top: 480, left: 64, right: 64, fontSize: 100, fontWeight: 900, lineHeight: 1.25, whiteSpace: 'pre-line'}}>{text}</div></EditorialFrame>;
};
