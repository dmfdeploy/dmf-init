import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: 'var(--bg)',
        panel: 'var(--panel)',
        accent: 'var(--accent)',
        accentSoft: 'var(--accent-soft)',
        text: 'var(--text)',
        muted: 'var(--muted)',
        border: 'var(--border)',
      },
      boxShadow: {
        glow: '0 0 0 1px rgba(148, 163, 184, 0.12), 0 30px 80px rgba(4, 12, 24, 0.35)',
      },
    },
  },
  plugins: [],
} satisfies Config
