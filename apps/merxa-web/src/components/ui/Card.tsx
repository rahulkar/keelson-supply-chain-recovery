import type { ReactNode } from "react";

export function Card({
  title,
  meta,
  actions,
  children,
  flush = false,
  id,
}: {
  title?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
  id?: string;
}) {
  return (
    <section className="mx-card" id={id} aria-label={typeof title === "string" ? title : undefined}>
      {(title || meta || actions) && (
        <div className="mx-card__header">
          {title && <h2 className="mx-card__title">{title}</h2>}
          {meta && <span className="mx-card__meta">{meta}</span>}
          {actions}
        </div>
      )}
      <div className={flush ? "mx-card__body mx-card__body--flush" : "mx-card__body"}>{children}</div>
    </section>
  );
}

export function KeyValues({ items }: { items: { label: string; value: ReactNode }[] }) {
  return (
    <dl className="mx-kv">
      {items.map((it) => (
        <div className="mx-kv__item" key={it.label}>
          <dt>{it.label}</dt>
          <dd>{it.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="mx-empty" role="status">
      <strong>{title}</strong>
      {children && <span>{children}</span>}
    </div>
  );
}
