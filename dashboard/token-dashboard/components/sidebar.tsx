'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { LayoutDashboard, Coins, Monitor, Cpu, DollarSign } from 'lucide-react';

const NAV_ITEMS = [
  { href: '/', label: 'Overview', icon: LayoutDashboard },
  { href: '/tokens', label: 'Token Analysis', icon: Coins },
  { href: '/terminals', label: 'Terminals', icon: Monitor },
  { href: '/models', label: 'Models', icon: Cpu },
  { href: '/usage', label: 'Usage & Costs', icon: DollarSign },
];

export default function Sidebar() {
  const pathname = usePathname();

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
          <div
            style={{
              width: 36,
              height: 36,
              borderRadius: 10,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 11,
              fontWeight: 800,
              letterSpacing: '0.05em',
              background: 'linear-gradient(135deg, #f97316 0%, #fb923c 100%)',
              color: '#070b16',
              boxShadow: '0 4px 16px rgba(249, 115, 22, 0.3)',
            }}
          >
            VNX
          </div>
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
      <nav className="flex-1 px-3 py-5" style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
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
              {/* Active indicator bar */}
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
