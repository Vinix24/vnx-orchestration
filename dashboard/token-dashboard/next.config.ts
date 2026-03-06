import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  transpilePackages: ['recharts', 'recharts-scale', 'd3-scale', 'd3-shape', 'd3-path', 'd3-interpolate', 'd3-color', 'd3-format', 'd3-time', 'd3-time-format', 'd3-array'],
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: 'http://localhost:4173/api/:path*',
      },
      {
        source: '/state/:path*',
        destination: 'http://localhost:4173/state/:path*',
      },
    ];
  },
};

export default nextConfig;
