'use client';

import Image from 'next/image';
import Link from 'next/link';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { LayoutDashboard, Coins, Monitor, Cpu, DollarSign, MessageSquare, Radio, AlertTriangle, Kanban, ShieldAlert, Activity, FileText } from 'lucide-react';

const DOMAINS = ['All', 'Coding', 'Analytics'] as const;
type Domain = typeof DOMAINS[number];

const OPERATOR_NAV = [
  { href: '/operator', label: 'Control Surface', icon: Radio },
  { href: '/operator/open-items', label: 'Open Items', icon: AlertTriangle },
  { href: '/operator/kanban', label: 'Kanban Board', icon: Kanban },
  { href: '/operator/governance', label: 'Governance', icon: ShieldAlert },
  { href: '/operator/reports', label: 'Reports', icon: FileText },
  { href: '/agent-stream', label: 'Agent Stream', icon: Activity },
];

const NAV_ITEMS = [
  { href: '/', label: 'Overview', icon: LayoutDashboard },
  { href: '/conversations', label: 'Conversations', icon: MessageSquare },
  { href: '/tokens', label: 'Token Analysis', icon: Coins },
  { href: '/terminals', label: 'Terminals', icon: Monitor },
  { href: '/models', label: 'Models', icon: Cpu },
  { href: '/usage', label: 'Usage & Costs', icon: DollarSign },
];

function showOperator(domain: Domain | undefined): boolean {
  return !domain || domain === 'All' || domain === 'Coding';
}

function showAnalytics(domain: Domain | undefined): boolean {
  return !domain || domain === 'All' || domain === 'Analytics';
}

export default function Sidebar() {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const activeDomain = (searchParams.get('domain') ?? undefined) as Domain | undefined;

  function setDomain(domain: string | undefined) {
    const params = new URLSearchParams(searchParams.toString());
    if (domain) {
      params.set('domain', domain);
    } else {
      params.delete('domain');
    }
    router.push(`${pathname}?${params.toString()}`);
  }

  return (
    <aside
      className="fixed left-0 top-0 h-screen flex flex-col"
      style={{
        width: 260,
        background: 'linear-gradient(180deg, #0c1638 0%, #080e24 50%, #070b16 100%)',
        borderRight: '1px solid rgba(255, 255, 255, 0.06)',
      }}
    >
      {/* Logo area */}
      <div
        className="px-6 py-7"
        style={{
          borderBottom: '1px solid rgba(255, 255, 255, 0.06)',
        }}
      >
        <div className="flex items-center gap-3">
          <Image
            src="/logo.png"
            alt="VNX"
            width={36}
            height={36}
            style={{
              borderRadius: 10,
              boxShadow: '0 4px 16px rgba(249, 115, 22, 0.3)',
            }}
          />
          <div>
            <h1
              className="text-sm font-semibold"
              style={{ color: 'var(--color-foreground)', letterSpacing: '-0.01em' }}
            >
              Token Dashboard
            </h1>
            <p className="text-xs" style={{ color: 'var(--color-muted)', marginTop: 2 }}>
              Session Analytics
            </p>
          </div>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-3 py-5" style={{ display: 'flex', flexDirection: 'column', gap: 2, overflowY: 'auto' }}>

        {/* Domain filter tabs */}
        <div
          data-testid="domain-tabs"
          style={{
            display: 'flex',
            flexWrap: 'wrap',
            gap: 6,
            padding: '4px 14px 12px',
          }}
        >
          {DOMAINS.map((d) => {
            const value = d === 'All' ? undefined : d;
            const isActive = activeDomain === value;
            return (
              <button
                key={d}
                data-testid={`domain-tab-${d.toLowerCase()}`}
                onClick={() => setDomain(value)}
                style={{
                  padding: '4px 12px',
                  borderRadius: 20,
                  fontSize: 11,
                  fontWeight: isActive ? 600 : 400,
                  background: isActive ? 'rgba(249, 115, 22, 0.15)' : 'rgba(255,255,255,0.04)',
                  border: `1px solid ${isActive ? 'rgba(249, 115, 22, 0.4)' : 'rgba(255,255,255,0.08)'}`,
                  color: isActive ? 'var(--color-accent)' : 'var(--color-muted)',
                  cursor: 'pointer',
                }}
              >
                {d}
              </button>
            );
          })}
        </div>

        {/* Divider */}
        <div style={{ height: 1, background: 'rgba(255,255,255,0.05)', margin: '0 14px 8px' }} />

        {/* Operator section */}
        {showOperator(activeDomain) && (
          <div style={{ marginBottom: 4, marginTop: 2 }}>
            <p
              style={{
                fontSize: 10,
                fontWeight: 700,
                letterSpacing: '0.08em',
                color: 'rgba(249, 115, 22, 0.7)',
                textTransform: 'uppercase',
                padding: '4px 14px 6px',
              }}
            >
              Operator
            </p>
            {OPERATOR_NAV.map(({ href, label, icon: Icon }) => {
              const isActive = pathname === href;
              return (
                <Link
                  key={href}
                  href={href}
                  className="flex items-center gap-3 text-sm transition-all"
                  style={{
                    padding: '10px 14px',
                    borderRadius: 10,
                    backgroundColor: isActive ? 'rgba(249, 115, 22, 0.1)' : 'transparent',
                    color: isActive ? 'var(--color-accent)' : 'var(--color-muted)',
                    fontWeight: isActive ? 600 : 400,
                    position: 'relative',
                    textDecoration: 'none',
                  }}
                >
                  {isActive && (
                    <div
                      style={{
                        position: 'absolute',
                        left: 0,
                        top: '50%',
                        transform: 'translateY(-50%)',
                        width: 3,
                        height: 20,
                        borderRadius: '0 3px 3px 0',
                        background: 'linear-gradient(180deg, #f97316, #fb923c)',
                        boxShadow: '0 0 8px rgba(249, 115, 22, 0.4)',
                      }}
                    />
                  )}
                  <Icon size={18} strokeWidth={isActive ? 2.2 : 1.8} />
                  <span>{label}</span>
                </Link>
              );
            })}
          </div>
        )}

        {/* Divider — only when both sections visible */}
        {showOperator(activeDomain) && showAnalytics(activeDomain) && (
          <div style={{ height: 1, background: 'rgba(255,255,255,0.05)', margin: '4px 14px 8px' }} />
        )}

        {/* Analytics section */}
        {showAnalytics(activeDomain) && (
          <>
            <p
              style={{
                fontSize: 10,
                fontWeight: 700,
                letterSpacing: '0.08em',
                color: 'rgba(244, 244, 249, 0.35)',
                textTransform: 'uppercase',
                padding: '4px 14px 6px',
              }}
            >
              Analytics
            </p>
            {NAV_ITEMS.map(({ href, label, icon: Icon }) => {
              const isActive = pathname === href;
              return (
                <Link
                  key={href}
                  href={href}
                  className="flex items-center gap-3 text-sm transition-all"
                  style={{
                    padding: '10px 14px',
                    borderRadius: 10,
                    backgroundColor: isActive ? 'rgba(249, 115, 22, 0.1)' : 'transparent',
                    color: isActive ? 'var(--color-accent)' : 'var(--color-muted)',
                    fontWeight: isActive ? 600 : 400,
                    position: 'relative',
                    textDecoration: 'none',
                  }}
                >
                  {isActive && (
                    <div
                      style={{
                        position: 'absolute',
                        left: 0,
                        top: '50%',
                        transform: 'translateY(-50%)',
                        width: 3,
                        height: 20,
                        borderRadius: '0 3px 3px 0',
                        background: 'linear-gradient(180deg, #f97316, #fb923c)',
                        boxShadow: '0 0 8px rgba(249, 115, 22, 0.4)',
                      }}
                    />
                  )}
                  <Icon size={18} strokeWidth={isActive ? 2.2 : 1.8} />
                  <span>{label}</span>
                </Link>
              );
            })}
          </>
        )}
      </nav>

      {/* Footer */}
      <div
        className="px-6 py-4 text-xs"
        style={{
          borderTop: '1px solid rgba(255, 255, 255, 0.06)',
          color: 'var(--color-muted)',
          opacity: 0.7,
        }}
      >
        Claude Code Analytics
      </div>
    </aside>
  );
}
