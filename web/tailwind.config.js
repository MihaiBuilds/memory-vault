/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: 'var(--bg)',
        bg2: 'var(--bg2)',
        bg3: 'var(--bg3)',
        border: 'var(--border)',
        text: 'var(--text)',
        text2: 'var(--text2)',
        accent: 'var(--accent)',
        success: 'var(--green)',
        danger: 'var(--red)',
        warning: 'var(--orange)',
        purple: 'var(--purple)',
      },
    },
  },
  plugins: [],
}
