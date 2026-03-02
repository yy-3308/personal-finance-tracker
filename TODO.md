# TODO

## Plaid Integration
- Sign up at plaid.com/dashboard for API keys (free tier: 200 API calls, Pay-as-you-go for individuals)
- Add Plaid Link widget — "Connect Bank" button on Import page for secure bank login
- Store `access_token` per linked account in DB
- Pull transactions, balances, and investment holdings automatically
- Replace manual CSV imports for connected accounts
- Reference: https://www.reddit.com/r/fintech/comments/1qmjhtj/is_plaid_available_for_personal_use/

## Credit Card Points Tracking
- Track points/miles balance per card (Chase UR, Amex MR, Citi ThankYou, Capital One Miles, etc.)
- Category multipliers (e.g., 4x dining on Amex Gold, 3x travel on CSR)
- Points earned per transaction based on card + category
- Points valuation with configurable cents-per-point rates
- Redemption history

## Card Perks & Benefits Tracker
- Per-card annual checklist of credits and perks
- Track used vs unused vs expiring soon
- Monthly credits (e.g., $10/mo Uber, $15/mo streaming) with auto-reset
- Annual credits (e.g., $300 travel credit, $200 airline fee, Global Entry)
- Show annual fee vs total value received — are you getting your money's worth?
- Reminders for expiring credits
