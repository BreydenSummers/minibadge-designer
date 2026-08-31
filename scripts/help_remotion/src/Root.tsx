import React from 'react';
import {Composition, Still} from 'remotion';
import {ClipProps, FPS, HelpClip, clipDuration} from './HelpClip';
import {HelpStill, StillProps} from './HelpStill';

const clipDefaults: ClipProps = {
  steps: [
    {img: null, caption: 'sample step', ring: {kind: 'box', x: 80, y: 80, w: 300, h: 90}, hold_ms: 1600},
    {img: null, caption: 'second step', ring: null, hold_ms: 2200},
  ],
  src_w: 1320,
  src_h: 1150,
};

const stillDefaults: StillProps = {
  img: null,
  caption: '',
  src_w: 1320,
  src_h: 1150,
};

export const Root: React.FC = () => {
  return (
    <>
      <Composition
        id="HelpClip"
        component={HelpClip}
        width={1320}
        height={1150}
        fps={FPS}
        durationInFrames={clipDuration(clipDefaults)}
        defaultProps={clipDefaults}
        calculateMetadata={({props}) => ({
          durationInFrames: clipDuration(props),
          width: props.src_w,
          height: props.src_h,
        })}
      />
      <Still
        id="HelpStill"
        component={HelpStill}
        width={1320}
        height={1150}
        defaultProps={stillDefaults}
        calculateMetadata={({props}) => ({
          width: props.src_w,
          height: props.src_h,
        })}
      />
    </>
  );
};
