import { Suspense } from 'react';
import type { Metadata } from 'next';
import Sidebar from '@/components/sidebar';
import './globals.css';

export const metadata: Metadata = {
  title: 'VNX Token Dashboard',
  description: 'Claude Code session analytics dashboard',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body>
        <Suspense>
          <Sidebar />
        </Suspense>
        <main
          className="min-h-screen page-enter"
          style={{
            marginLeft: 260,
            padding: '32px 40px',
            backgroundColor: 'var(--color-background)',
            backgroundImage: 'radial-gradient(ellipse at 20% 0%, rgba(249, 115, 22, 0.03) 0%, transparent 60%)',
          }}
        >
          {children}
        </main>
      </body>
    </html>
  );
}
