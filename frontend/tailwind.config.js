/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        white: '#f5f4f1',
        bg: '#050506',
        card: '#0d0d0f',
        raised: '#121214',
        control: '#161618',
        border: '#29292d',
        field: '#66666f',
        muted: '#a0a0a7',
        accent: '#f5f4f1',
        'accent-ink': '#080809',
        'accent-2': '#c7c7cc',
        teal: '#0d9488',
        mint: '#5eead4',
        ok: '#10b981',
        amber: '#fbbf24',
        warn: '#f59e0b',
        danger: '#ef4444',
        info: '#3b82f6',
      },
      fontFamily: {
        sans: ['Manrope Variable', 'ui-sans-serif', 'system-ui', '-apple-system', 'sans-serif'],
      },
      borderRadius: { lg: '0.75rem', xl: '1rem', '2xl': '1.125rem' },
    },
  },
  plugins: [],
};
