import type { CSSProperties, HTMLAttributes, ReactNode } from 'react';

export function Card({
  children,
  style,
  onClick,
  ...rest
}: {
  children: ReactNode;
  style?: CSSProperties;
  onClick?: () => void;
} & HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      onClick={onClick}
      className={onClick ? 'card-hover' : undefined}
      style={{
        background: '#FFFFFF',
        borderRadius: 22,
        boxShadow: 'var(--shadow-card)',
        cursor: onClick ? 'pointer' : undefined,
        ...style,
      }}
      {...rest}
    >
      {children}
    </div>
  );
}

export function ScreenLayout({ children }: { children: ReactNode }) {
  return (
    <div style={{ padding: '26px 20px 110px', display: 'flex', flexDirection: 'column', gap: 16 }}>
      {children}
    </div>
  );
}
