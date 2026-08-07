import type { CSSProperties } from 'react';
import { FONT_CN, FONT_DISPLAY, sectionLabelStyle } from '../lib/typography';

type MixedSectionLabelProps = {
  cn: string;
  en: string;
  style?: CSSProperties;
};

/** Section kicker with CN serif + Latin display halves (e.g. 游戏室 · GAMES). */
export function MixedSectionLabel({ cn, en, style }: MixedSectionLabelProps) {
  return (
    <div style={{ ...sectionLabelStyle, padding: '0 2px', ...style }}>
      <span style={{ fontFamily: FONT_CN }}>{cn}</span>
      <span style={{ fontFamily: FONT_CN }}> · </span>
      <span style={{ fontFamily: FONT_DISPLAY }}>{en}</span>
    </div>
  );
}
