import React from 'react';
import {AbsoluteFill, OffthreadVideo, Sequence, staticFile} from 'remotion';

type VideoScene = {
  scene_id: string;
  start_frame: number;
  duration_frames: number;
  caption?: string | null;
};

export type RenderProps = {
  videoSpec: {
    project_id?: string;
    format?: string;
    width: number;
    height: number;
    fps: {numerator: number; denominator: number};
    scenes: VideoScene[];
  };
  sceneSources: Record<string, {src: string; trimBefore: number; trimAfter: number}>;
  // There is no narration provider in Task 014. Keep real source audio only.
  audioMode: 'source';
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
            <OffthreadVideo
              src={staticFile(source.src)}
              trimBefore={source.trimBefore}
              trimAfter={source.trimAfter}
              volume={audioMode === 'source' ? 1 : 0}
              style={{width: '100%', height: '100%', objectFit: 'cover'}}
            />
            {scene.caption ? (
              <div style={{position: 'absolute', bottom: 96, left: 56, right: 56, color: 'white', fontSize: 48, fontWeight: 700, textAlign: 'center', textShadow: '0 2px 8px black'}}>
                {scene.caption}
              </div>
            ) : null}
          </AbsoluteFill>
        </Sequence>
      );
    })}
  </AbsoluteFill>
);
