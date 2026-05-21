import { Suspense } from 'react';
import type { Metadata } from 'next';
import { Inter } from 'next/font/google';
import Sidebar from '@/components/sidebar';
import './globals.css';

const inter = Inter({ subsets: ['latin'] });

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
    <html lang="nl" className={inter.className}>
      <body>
        <Suspense>
          <Sidebar />
        </Suspense>
        <main
          className="min-h-screen page-enter"
          style={{
            marginLeft: 240,
            padding: '32px 40px',
            backgroundColor: 'var(--color-background)',
          }}
        >
          {children}
        </main>
      </body>
    </html>
  );
}
