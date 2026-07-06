// Static display config. These were editable "Tweaks" in the original design
// prototype; there is no settings UI in scope for this build, so they're
// plain constants. Change here if needed.
export const CONFIG = {
  showSeconds: true,
  statusQuote: '想把今晚的月亮读给你听。',
  togetherSince: '2023-12-24',
  /** used only if /api/ledger/budget is unreachable */
  fallbackBudget: 3000,
};

// Jilin City, per the design brief (吉林省吉林市)
export const WEATHER_COORDS = { latitude: 43.8378, longitude: 126.5494 };
