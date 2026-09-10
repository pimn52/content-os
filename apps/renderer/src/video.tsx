import React from 'react';
import {AbsoluteFill, Audio, Img, OffthreadVideo, Sequence, staticFile} from 'remotion';

type VideoScene = {
  scene_id: string;
  start_frame: number;
  duration_frames: number;
  caption?: string | null;
  captions?: Array<{start_ms: number; end_ms: number; text: string}>;
};

type SceneSource =
  | {kind: 'video'; src: string; trimBefore: number; trimAfter: number; narrationSrc?: string; narrationStartFrame?: number}
  | {kind: 'image'; src: string; narrationSrc?: string; narrationStartFrame?: number}
  | {kind: 'typography'; text: string; narrationSrc?: string; narrationStartFrame?: number};

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
  audioMode: 'source' | 'narration';
};

export const ContentOSVideo: React.FC<RenderProps> = ({videoSpec, sceneSources, audioMode}) => (
  <AbsoluteFill style={{backgroundColor: 'black'}}>
    {videoSpec.scenes.map((scene) => {
      const source = sceneSources[scene.scene_id];
      if (!source) {
        throw new Error(`Missing staged source for scene ${scene.scene_id}`);
      }
      return (
        <Sequence key={scene.scene_id} from={scene.start_frame} durationInFrames={scene.duration_frames}>
          <AbsoluteFill>
            {source.kind === 'video' ? (
              <OffthreadVideo
                src={staticFile(source.src)}
                trimBefore={source.trimBefore}
                trimAfter={source.trimAfter}
                volume={audioMode === 'narration' && source.narrationSrc ? 0 : 1}
                // Creator footage is a first-class source. Preserve the full
                // frame when adapting horizontal media to the vertical
                // composition; a deliberate letterbox is safer than silently
                // cropping the speaker or the visual context.
                style={{width: '100%', height: '100%', objectFit: 'contain', backgroundColor: '#111'}}
              />
            ) : source.kind === 'image' ? (
              <Img src={staticFile(source.src)} style={{width: '100%', height: '100%', objectFit: 'contain'}} />
            ) : (
              <AbsoluteFill style={{backgroundColor: '#172033', alignItems: 'center', justifyContent: 'center', padding: 90}}>
                <div style={{color: 'white', fontSize: 64, fontWeight: 700, lineHeight: 1.2, textAlign: 'center'}}>{source.text}</div>
              </AbsoluteFill>
            )}
            {source.narrationSrc ? <Audio src={staticFile(source.narrationSrc)} startFrom={source.narrationStartFrame ?? 0} volume={1} /> : null}
            {scene.captions?.length ? scene.captions.map((caption) => {
              const startFrame = Math.max(0, Math.round(caption.start_ms * videoSpec.fps.numerator / (1000 * videoSpec.fps.denominator)));
              const endFrame = Math.max(startFrame + 1, Math.round(caption.end_ms * videoSpec.fps.numerator / (1000 * videoSpec.fps.denominator)));
              return <Sequence key={`${caption.start_ms}-${caption.end_ms}-${caption.text}`} from={startFrame} durationInFrames={endFrame - startFrame}><Caption text={caption.text} /></Sequence>;
            }) : scene.caption && audioMode === 'narration' ? <Caption text={scene.caption} /> : null}
          </AbsoluteFill>
        </Sequence>
      );
    })}
  </AbsoluteFill>
);

const Caption: React.FC<{text: string}> = ({text}) => (
  <div style={{position: 'absolute', bottom: 96, left: 56, right: 56, color: 'white', fontSize: 48, fontWeight: 700, textAlign: 'center', textShadow: '0 2px 8px black'}}>
    {text}
  </div>
);
