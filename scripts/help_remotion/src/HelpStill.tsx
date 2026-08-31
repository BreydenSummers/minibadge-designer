import React from 'react';
import {AbsoluteFill, Img, staticFile} from 'remotion';

export type StillProps = {
  img: string | null;
  caption: string;
  src_w: number;
  src_h: number;
};

// Same framing as HelpClip, held on one state: for legends and final renders.
export const HelpStill: React.FC<StillProps> = ({img, caption}) => {
  return (
    <AbsoluteFill
      style={{
        background:
          'radial-gradient(120% 90% at 18% 0%, #1a2233 0%, #0b0e14 62%)',
      }}
    >
      <AbsoluteFill
        style={{
          borderRadius: 22,
          overflow: 'hidden',
          boxShadow: '0 14px 46px rgba(0,0,0,0.6)',
          border: '1px solid rgba(230,237,243,0.10)',
        }}
      >
        {img ? (
          <Img
            src={staticFile(img)}
            style={{width: '100%', height: '100%', objectFit: 'fill'}}
          />
        ) : (
          <AbsoluteFill style={{background: '#131a26'}} />
        )}
      </AbsoluteFill>
      {caption ? (
        <div
          style={{
            position: 'absolute',
            left: '50%',
            bottom: 26,
            transform: 'translateX(-50%)',
            maxWidth: '86%',
            padding: '16px 28px',
            borderRadius: 18,
            background: 'rgba(11, 14, 20, 0.88)',
            border: '2px solid rgba(232, 62, 140, 0.45)',
            boxShadow: '0 10px 34px rgba(0,0,0,0.55)',
            color: '#e6edf3',
            fontFamily:
              'system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif',
            fontSize: 30,
            fontWeight: 600,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {caption}
        </div>
      ) : null}
    </AbsoluteFill>
  );
};
