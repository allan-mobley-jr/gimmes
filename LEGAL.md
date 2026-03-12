# Legal Notice

## TL;DR

GIMMES is legal software that trades on a legal exchange. Automated API trading on Kalshi is explicitly permitted by both federal regulators and the exchange itself. The strategy — researching public information and trading on your probability estimates — is normal market activity. There is no insider trading analog because all signals (news, economic data, cross-platform pricing) are public.

That said, you should understand the regulatory landscape before using this software.

---

## Kalshi Is a Regulated Exchange

Kalshi (KalshiEX LLC) is a [CFTC-licensed Designated Contract Market (DCM)](https://www.cftc.gov/PressRoom/PressReleases/8439-21) — the same regulatory category as CME and CBOE. Trading on Kalshi is explicitly legal for US persons. The exchange publishes SDKs, API documentation, and developer tools specifically to support automated trading.

## Automated Trading Is Legal

The CFTC does not require registration for retail algorithmic traders. The proposed "Regulation Automated Trading" (Reg AT), which would have required registration of algorithmic traders, was [withdrawn in June 2020](https://www.davispolk.com/insights/client-update/cftc-withdraws-reg-proposal-proposes-principles-based-electronic-trading) and replaced with principles-based Electronic Trading Risk Principles that place obligations on **exchanges**, not on individual retail traders.

Regulatory bodies including the SEC, CFTC, and FINRA [permit algorithmic trading](https://advancedautotrades.com/is-automated-trading-legal/) provided the systems comply with market integrity rules.

## The One Conduct Rule to Know

**Market manipulation is illegal on regulated exchanges.** This includes spoofing (placing orders you intend to cancel to move prices), wash trading (trading with yourself), and layering. These are federal crimes under the Commodity Exchange Act regardless of whether conducted manually or via automated tools.

At the scale a typical GIMMES user would operate, manipulation is not a realistic concern — you are a price taker in deep markets. The strategy as designed (finding genuine edges via public information and trading them at market prices) has no manipulation element. Just don't place orders designed to artificially move prices or deceive other market participants.

## State-Level Restrictions

Kalshi faces ongoing legal challenges from state gaming regulators, primarily over **sports event contracts**. As of early 2026:

- **Massachusetts** — Sports contracts geofenced following a [January 2026 preliminary injunction](https://www.nbcnews.com/news/us-news/kalshi-cannot-operate-sports-prediction-market-massachusetts-judge-rul-rcna255130)
- **Nevada** — Temporary restraining order issued by federal court following [Gaming Control Board enforcement](https://knpr.org/economy/2026-02-18/administration-backs-kalshi-and-polymarket-as-states-including-nevada-move-to-ban-them)
- **Connecticut** — Cease-and-desist orders issued December 2025

Additional states with active litigation or enforcement include Tennessee, Maryland, New Jersey, and Ohio. Court outcomes are split on whether the Commodity Exchange Act preempts state gaming laws.

**These legal challenges are against Kalshi as the exchange operator — not against traders.** As a retail participant using the platform through its published API, you are not a party to these disputes. If Kalshi becomes restricted in your state, that affects whether you *can* trade, not whether you did anything wrong by trading when it was available.

Non-sports contracts (economics, weather, politics) have generally not been targeted by state regulators — the disputes center on sports event contracts.

**You should verify that Kalshi is accessible and legal in your jurisdiction before trading.** See [Kalshi's trading prohibitions](https://kalshi-public-docs.s3.amazonaws.com/kalshi-source-agency-trading-prohibitions.pdf) for the current list.

## Open-Source Trading Tools

Distributing open-source software that interacts with a public API is not a regulated activity. This project:

- Is **software**, not a financial service
- Does **not** manage other people's money (which would trigger investment adviser registration)
- Does **not** operate a trading platform or exchange
- Does **not** provide personalized financial advice

The developers are not registered with the CFTC, SEC, or any state regulator, and are not required to be.

## Disclaimers

1. **Not financial advice.** This software is an educational and research tool. Nothing in this project constitutes investment advice, financial advice, trading advice, or any other sort of professional advice. Use at your own risk.

2. **No warranty.** This software is provided "as is" without warranty of any kind. See the [MIT License](LICENSE) for full terms. The developers assume no liability for trading losses, missed opportunities, API failures, exchange disputes, or any other damages.

3. **Your responsibility.** Users are solely responsible for:
   - Complying with all applicable federal, state, and local laws
   - Agreeing to Kalshi's [Member Agreement](https://kalshi.com/regulatory/rulebook) and API Developer Agreement independently
   - Understanding the risks of prediction market trading, including the possibility of total loss
   - Proper tax reporting of gains and losses (event contract gains are taxable)
   - Ensuring Kalshi is available and legal in their jurisdiction

4. **Platform risk.** Kalshi retains broad discretion over contract settlement, including the ability to invoke carveouts, freeze markets, extend expirations, and modify terms. The [$54M Khamenei market freeze](https://www.pbs.org/newshour/politics/trump-administration-backs-kalshi-and-polymarket-as-states-move-to-ban-prediction-markets) and [January 2026 NFL settlement errors](https://kalshi.com) demonstrate that winning trades may not always pay out as expected. Position sizing should account for settlement risk.

5. **Regulatory risk.** The prediction market regulatory landscape is actively evolving. Kalshi faces [19 federal lawsuits](https://www.npr.org/2026/01/30/nx-s1-5691837/lawsets-prediction-market-kalshi) from state regulators, consumer class actions, and ongoing jurisdictional disputes. Access to the platform may change at any time.

---

*This notice is informational and does not constitute legal advice. For anything beyond this level of analysis, consult a commodities or fintech attorney in your jurisdiction.*
