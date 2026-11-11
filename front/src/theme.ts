export const DARK_THEME_COLOR = '#18181b'

/** One theme for both the application tree and teleported/native surfaces. */
export function applyDarkTheme() {
  document.documentElement.style.colorScheme = 'dark'
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', DARK_THEME_COLOR)
}
