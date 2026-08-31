import React from 'react';
import {
  AbsoluteFill,
  Img,
  Sequence,
  interpolate,
  staticFile,
  useCurrentFrame,
  Easing,
} from 'remotion';

export const FPS = 15;
// Every step spends HEAD frames animating in (crossfade, ring draw, caption
// slide) and is pixel-static afterwards. The webp assembler relies on that:
// it collapses the identical hold frames into one long-duration frame.
export const HEAD = 12;
const TRANS = 4; // crossfade length, inside HEAD

export const RING = '#e83e8c';

export type Ring =
  | {kind: 'box'; x: number; y: number; w: number; h: number}
  | {kind: 'spot'; x: number; y: number; r: number};

export type Step = {
  img: string | null;
  caption: string;
  ring: Ring | null;
  hold_ms: number;
};

export type ClipProps = {
  steps: Step[];
  src_w: number;
  src_h: number;
};

export const stepFrames = (s: Step) =>
  HEAD + Math.max(1, Math.round((s.hold_ms * FPS) / 1000));

export const clipDuration = (p: ClipProps) =>
  p.steps.reduce((a, s) => a + stepFrames(s), 0);

const clamp = {
  extrapolateLeft: 'clamp',
  extrapolateRight: 'clamp',
} as const;

const ringBox = (ring: Ring) => {
  const PAD = 12;
  if (ring.kind === 'spot') {
    return {
      left: ring.x - ring.r - PAD,
      top: ring.y - ring.r - PAD,
      width: 2 * (ring.r + PAD),
      height: 2 * (ring.r + PAD),
      radius: ring.r + PAD,
    };
  }
  return {
    left: ring.x - PAD,
    top: ring.y - PAD,
    width: ring.w + 2 * PAD,
    height: ring.h + 2 * PAD,
    radius: 16,
  };
};

const RingLayer: React.FC<{ring: Ring}> = ({ring}) => {
  const f = useCurrentFrame();
  const box = ringBox(ring);
  const opacity = interpolate(f, [3, 6], [0, 1], clamp);
  const scale = interpolate(f, [3, 9], [1.35, 1], {
    ...clamp,
    easing: Easing.out(Easing.back(1.6)),
  });
  const haloScale = interpolate(f, [6, 12], [1, 1.22], {
    ...clamp,
    easing: Easing.out(Easing.quad),
  });
  const haloOpacity = interpolate(f, [6, 12], [0.5, 0], clamp);
  const common: React.CSSProperties = {
    position: 'absolute',
    left: box.left,
    top: box.top,
    width: box.width,
    height: box.height,
    borderRadius: box.radius,
    pointerEvents: 'none',
  };
  return (
    <>
      <div
        style={{
          ...common,
          border: `5px solid ${RING}`,
          opacity: haloOpacity,
          transform: `scale(${haloScale})`,
        }}
      />
      <div
        style={{
          ...common,
          border: `5px solid ${RING}`,
          boxShadow: `0 0 22px 0 ${RING}66, inset 0 0 14px 0 ${RING}33`,
          opacity,
          transform: `scale(${scale})`,
        }}
      />
    </>
  );
};

const Chip: React.FC<{
  caption: string;
  index: number;
  total: number;
  atTop: boolean;
  srcW: number;
}> = ({caption, index, total, atTop, srcW}) => {
  const f = useCurrentFrame();
  const opacity = interpolate(f, [1, 6], [0, 1], clamp);
  const dy = interpolate(f, [1, 7], [atTop ? -20 : 20, 0], {
    ...clamp,
    easing: Easing.out(Easing.cubic),
  });
  // Bottom chips stay clear of the app's own toasts (fixed bottom-right,
  // ~300px wide) - the confirming toast must stay readable. Top chips sit
  // below the brand bar and the FRONT/BACK/BOTH tab row.
  const left = atTop ? srcW / 2 : (srcW - 310) / 2;
  return (
    <div
      style={{
        position: 'absolute',
        left,
        [atTop ? 'top' : 'bottom']: atTop ? 64 : 26,
        transform: `translateX(-50%) translateY(${dy}px)`,
        opacity,
        display: 'flex',
        alignItems: 'center',
        gap: 20,
        maxWidth: atTop ? '86%' : srcW - 340,
        padding: '16px 28px',
        borderRadius: 18,
        background: 'rgba(11, 14, 20, 0.88)',
        border: '2px solid rgba(232, 62, 140, 0.45)',
        boxShadow: '0 10px 34px rgba(0,0,0,0.55)',
        backdropFilter: 'blur(10px)',
      }}
    >
      <div style={{display: 'flex', gap: 9, flexShrink: 0}}>
        {Array.from({length: total}, (_, i) => (
          <div
            key={i}
            style={{
              width: 13,
              height: 13,
              borderRadius: 7,
              background: i === index ? RING : 'rgba(230,237,243,0.28)',
              boxShadow: i === index ? `0 0 10px ${RING}aa` : 'none',
            }}
          />
        ))}
      </div>
      <div
        style={{
          color: '#e6edf3',
          fontFamily:
            'system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif',
          fontSize: 30,
          fontWeight: 600,
          lineHeight: 1.25,
          letterSpacing: 0.2,
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
      >
        {caption}
      </div>
    </div>
  );
};

const StepLayer: React.FC<{
  step: Step;
  index: number;
  total: number;
  fadeIn: boolean;
  srcH: number;
  srcW: number;
}> = ({step, index, total, fadeIn, srcH, srcW}) => {
  const f = useCurrentFrame();
  const opacity = fadeIn ? interpolate(f, [0, TRANS], [0, 1], clamp) : 1;
  // Keep the chip clear of a ring that sits in the bottom band.
  const ringBottom =
    step.ring == null
      ? 0
      : step.ring.kind === 'spot'
        ? step.ring.y + step.ring.r
        : step.ring.y + step.ring.h;
  const chipAtTop = step.ring != null && ringBottom > srcH * 0.76;
  return (
    <AbsoluteFill style={{opacity}}>
      <AbsoluteFill
        style={{
          borderRadius: 22,
          overflow: 'hidden',
          boxShadow: '0 14px 46px rgba(0,0,0,0.6)',
          border: '1px solid rgba(230,237,243,0.10)',
        }}
      >
        {step.img ? (
          <Img
            src={staticFile(step.img)}
            style={{width: '100%', height: '100%', objectFit: 'fill'}}
          />
        ) : (
          <AbsoluteFill style={{background: '#131a26'}} />
        )}
      </AbsoluteFill>
      {step.ring ? <RingLayer ring={step.ring} /> : null}
      {step.caption ? (
        <Chip
          caption={step.caption}
          index={index}
          total={total}
          atTop={chipAtTop}
          srcW={srcW}
        />
      ) : null}
    </AbsoluteFill>
  );
};

export const HelpClip: React.FC<ClipProps> = (props) => {
  const {steps} = props;
  const starts: number[] = [];
  let acc = 0;
  for (const s of steps) {
    starts.push(acc);
    acc += stepFrames(s);
  }
  return (
    <AbsoluteFill
      style={{
        background:
          'radial-gradient(120% 90% at 18% 0%, #1a2233 0%, #0b0e14 62%)',
      }}
    >
      {steps.map((s, i) => (
        <Sequence
          key={i}
          from={starts[i]}
          // Stay mounted TRANS frames into the next step so the incoming
          // layer crossfades over this one's final (static) state.
          durationInFrames={stepFrames(s) + (i < steps.length - 1 ? TRANS : 0)}
        >
          <StepLayer
            step={s}
            index={i}
            total={steps.length}
            fadeIn={i > 0}
            srcH={props.src_h}
            srcW={props.src_w}
          />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
