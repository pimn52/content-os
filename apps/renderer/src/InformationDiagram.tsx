import React from 'react';

// Conceptual illustrations only: no sampled measurements, quality scores or
// reconstructed footage. Labels come from the saved visual plan, not ASR.
export const InformationDiagram: React.FC<{
  treatment: string; text: string; frame: number; durationFrames: number;
  background: string; foreground: string; accent: string; font: string;
}> = ({treatment, text, frame, durationFrames, background, foreground, accent, font}) => {
  const labels = text.split('\n');
  const progress = Math.min(.999, Math.max(0, frame / Math.max(1, durationFrames)));
  const active = Math.floor(progress * labels.length);
  const stroke = {stroke: accent, strokeWidth: 5, fill: 'none'};
  return <svg width="100%" height="100%" viewBox="0 0 1080 1920" style={{background, color: foreground, fontFamily: font}}>
    <text x="80" y="180" fill={foreground} fontSize="28">原理示意 · 非实测</text>
    {treatment === 'diagram_flow' ? <>
      {labels.map((label, i) => <g key={i}>
        <rect x="180" y={380 + i * 300} width="720" height="170" rx="24" {...stroke} opacity={i <= active ? 1 : .35} />
        <text x="540" y={480 + i * 300} textAnchor="middle" fill={foreground} fontSize="42">{label}</text>
        {i < labels.length - 1 ? <path d={`M540 ${555 + i * 300} v110 m-18 -18 l18 18 18 -18`} {...stroke} /> : null}
      </g>)}
      {Array.from({length: 6}, (_, i) => {
        const y = 365 + ((progress * 2 + i / 6) % 1) * (labels.length * 300 - 130);
        return <rect key={i} x="120" y={y} width="24" height="24" rx="4" fill={accent} />;
      })}
    </> : treatment === 'diagram_loss' ? <>
      {labels.slice(0, 2).map((label, side) => <g key={side}>
        <text x={285 + side * 510} y="460" textAnchor="middle" fill={foreground} fontSize="38">{label}</text>
        {Array.from({length: 64}, (_, i) => {
          const missing = i % 7 === 0 || i % 11 === 0;
          return <rect key={i} x={80 + side * 510 + i % 8 * 49} y={530 + Math.floor(i / 8) * 49}
            width="43" height="43" fill={missing ? 'none' : accent} stroke={missing ? foreground : accent} opacity={missing ? .3 : .8} />;
        })}
        <text x={285 + side * 510} y="1000" textAnchor="middle" fill={foreground} fontSize="30">空格代表已丢失信息</text>
      </g>)}
      <path d="M470 1200 h140 m-18 -18 l18 18 -18 18" {...stroke} />
      <rect x="180" y="1300" width={200 + 500 * progress} height="36" rx="12" fill={accent} />
      <text x="180" y="1420" fill={foreground} fontSize="36">数据预算增加，空缺仍然存在</text>
    </> : <>
      <circle cx="540" cy="900" r="135" {...stroke} />
      <text x="540" y="915" textAnchor="middle" fill={foreground} fontSize="42">共同影响</text>
      {labels.map((label, i) => {
        const angle = -Math.PI / 2 + i * Math.PI * 2 / labels.length;
        const x = 540 + Math.cos(angle) * 315, y = 900 + Math.sin(angle) * 410;
        return <g key={i} opacity={i <= active ? 1 : .35}>
          <line x1={540 + Math.cos(angle) * 140} y1={900 + Math.sin(angle) * 140} x2={x} y2={y} {...stroke} />
          <rect x={x - 155} y={y - 65} width="310" height="130" rx="22" fill={background} stroke={accent} strokeWidth="5" />
          <text x={x} y={y + 14} textAnchor="middle" fill={foreground} fontSize="38">{label}</text>
          {i === active ? <circle cx={540 + Math.cos(angle) * (155 + (progress * labels.length % 1) * 130)}
            cy={900 + Math.sin(angle) * (155 + (progress * labels.length % 1) * 220)} r="10" fill={accent} /> : null}
        </g>;
      })}
    </>}
  </svg>;
};
