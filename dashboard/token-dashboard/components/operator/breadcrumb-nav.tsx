'use client';

import Link from 'next/link';
import { ChevronRight } from 'lucide-react';

interface BreadcrumbItem {
  label: string;
  href?: string;
}

interface BreadcrumbNavProps {
  /** Explicit item list — use when you want full control */
  items?: BreadcrumbItem[];
  /** Shorthand: sessionId, dispatchId, reportFile build the trail automatically */
  sessionId?: string;
  dispatchId?: string;
  reportFile?: string;
}

function buildItems(props: BreadcrumbNavProps): BreadcrumbItem[] {
  if (props.items) return props.items;

  const trail: BreadcrumbItem[] = [{ label: 'Operator', href: '/operator' }];

  if (props.reportFile) {
    trail.push({ label: 'Reports', href: '/operator/reports' });
    // Short label: last segment of filename without extension
    const slug = props.reportFile.split('/').pop()?.replace(/\.[^.]+$/, '') ?? props.reportFile;
    trail.push({ label: slug });
  } else if (props.dispatchId) {
    trail.push({ label: 'Reports', href: '/operator/reports' });
    trail.push({ label: props.dispatchId });
  } else if (props.sessionId) {
    trail.push({ label: 'Conversations', href: '/conversations' });
    trail.push({ label: props.sessionId.slice(0, 12) + '…' });
  }

  return trail;
}

export default function BreadcrumbNav(props: BreadcrumbNavProps) {
  const items = buildItems(props);

  return (
    <nav
      aria-label="Breadcrumb"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 4,
        marginBottom: 20,
        flexWrap: 'wrap',
      }}
    >
      {items.map((item, i) => {
        const isLast = i === items.length - 1;
        return (
          <span key={i} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            {i > 0 && (
              <ChevronRight
                size={12}
                style={{ color: 'var(--color-muted)', flexShrink: 0 }}
              />
            )}
            {item.href && !isLast ? (
              <Link
                href={item.href}
                style={{
                  fontSize: 12,
                  color: 'var(--color-muted)',
                  textDecoration: 'none',
                  transition: 'color 0.15s',
                }}
                onMouseEnter={(e) => {
                  (e.currentTarget as HTMLAnchorElement).style.color = 'var(--color-foreground)';
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLAnchorElement).style.color = 'var(--color-muted)';
                }}
              >
                {item.label}
              </Link>
            ) : (
              <span
                style={{
                  fontSize: 12,
                  color: isLast ? 'var(--color-foreground)' : 'var(--color-muted)',
                  fontWeight: isLast ? 500 : 400,
                  maxWidth: 260,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
                title={item.label}
              >
                {item.label}
              </span>
            )}
          </span>
        );
      })}
    </nav>
  );
}
