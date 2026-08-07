import type { ReactNode } from 'react';

export type PageHeaderProps = {
  title: string;
  subtitle?: string;
  onBack?: () => void;
  backLabel?: string;
  aside?: ReactNode;
};

/**
 * Canonical React page header — Chrome78-safe spacing via sibling margins, not flex gap.
 */
export function PageHeader({ title, subtitle, onBack, backLabel = '返回', aside }: PageHeaderProps) {
  return (
    <header className="page-header">
      {onBack ? (
        <button type="button" className="page-header__back" onClick={onBack} aria-label={backLabel}>
          ‹
        </button>
      ) : null}
      <h1 className="page-header__title">{title}</h1>
      {subtitle ? <span className="page-header__subtitle">{subtitle}</span> : null}
      {aside ? <div className="page-header__aside">{aside}</div> : null}
    </header>
  );
}
