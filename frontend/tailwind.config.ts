import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: 'var(--bg)',
        panel: 'var(--panel)',
        sidebar: 'var(--sidebar)',
        accent: 'var(--accent)',
        accentSoft: 'var(--accent-soft)',
        text: 'var(--text)',
        muted: 'var(--muted)',
        border: 'var(--border)',
      },
    },
  },
  plugins: [],
} satisfies Config
