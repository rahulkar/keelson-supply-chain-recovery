import Link from "next/link";
import type { ReactNode } from "react";

export type Crumb = { label: string; href?: string };

export function PageHeader({
  title,
  subtitle,
  crumbs,
  actions,
  badge,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  crumbs?: Crumb[];
  actions?: ReactNode;
  badge?: ReactNode;
}) {
  return (
    <header className="mx-page-header">
      <div className="mx-page-header__text">
        {crumbs && crumbs.length > 0 && (
          <nav aria-label="Breadcrumb" className="mx-breadcrumb">
            {crumbs.map((c, i) => (
              <span key={`${c.label}-${i}`}>
                {c.href ? <Link href={c.href}>{c.label}</Link> : c.label}
                {i < crumbs.length - 1 && <span aria-hidden="true"> /</span>}
              </span>
            ))}
          </nav>
        )}
        <h1 className="mx-page-title">
          {title}
          {badge}
        </h1>
        {subtitle && <p className="mx-page-subtitle">{subtitle}</p>}
      </div>
      {actions && <div className="mx-page-actions">{actions}</div>}
    </header>
  );
}
