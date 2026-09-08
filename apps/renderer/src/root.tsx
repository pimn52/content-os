import {Composition} from 'remotion';
import {ContentOSVideo, type RenderProps} from './video';

const defaultProps: RenderProps = {
  videoSpec: {
    project_id: '00000000-0000-0000-0000-000000000000',
    format: 'vertical',
    width: 1080,
    height: 1920,
    fps: {numerator: 30, denominator: 1},
    scenes: [],
  },
  sceneSources: {},
  audioMode: 'source',
};

export const ContentOSRoot = () => (
  <Composition
    id="ContentOSVideo"
    component={ContentOSVideo}
    defaultProps={defaultProps}
    durationInFrames={1}
    fps={30}
    width={1080}
    height={1920}
    calculateMetadata={({props}) => ({
      durationInFrames: Math.max(1, ...props.videoSpec.scenes.map((scene) => scene.start_frame + scene.duration_frames)),
      fps: props.videoSpec.fps.numerator / props.videoSpec.fps.denominator,
      width: props.videoSpec.width,
      height: props.videoSpec.height,
    })}
  />
);
