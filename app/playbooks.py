"""Routing layer: per-trigger-kind guidance appended to the composer prompt.

Each entry: (angle the message should take, preferred compulsion levers, default cta)
"""

PLAYBOOKS: dict[str, tuple[str, str, str]] = {
    # --- external / knowledge ---
    "research_digest": (
        "Share the referenced research item as a colleague would: title-level finding, the key number, the source citation "
        "exactly as given. Tie it to the merchant's own cohort if the data supports it. Offer to pull the abstract or draft "
        "shareable patient/customer content.",
        "specificity, curiosity, reciprocity, effort externalization", "open_ended"),
    "regulation_change": (
        "Compliance alert. State what changed, the exact deadline, and who is affected, citing the source exactly. "
        "Offer a concrete low-effort help (checklist / SOP note). Calm, precise, no fear-mongering.",
        "loss aversion (deadline), specificity, effort externalization", "binary_yes_no"),
    "cde_opportunity": (
        "Professional-development invite: event title, date/time, credits and fee from the item. Offer to block the slot or send details.",
        "specificity, reciprocity, low-friction yes", "binary_yes_no"),
    "category_trend_movement": (
        "Share the trend number and what it means for this merchant's offer/profile. Suggest one concrete positioning move.",
        "curiosity, social proof, specificity", "binary_yes_no"),
    "category_seasonal": (
        "Seasonal demand shift: name the 2-3 biggest movements from the payload and suggest one shelf/offer action.",
        "specificity, loss aversion, effort externalization", "binary_yes_no"),
    "festival_upcoming": (
        "Festival is the why-now. Use days_until/date from payload. If the festival is far away or the category is not in "
        "category_relevance, be honest: frame it as early planning or a light heads-up, not urgency. Propose one "
        "service+price offer from the catalog/active offers.",
        "loss aversion (window), effort externalization", "binary_yes_no"),
    "ipl_match_today": (
        "Match-day moment: match, venue/time from payload. Use judgment: if it is a weekend or the merchant's data suggests "
        "dine-in is already strong, say so honestly and suggest the smarter play (e.g., delivery combo). One offer, service+price.",
        "timeliness, specificity, effort externalization", "binary_yes_no"),
    "weather_heatwave": (
        "Weather moment from the payload; suggest one relevant service/product action for this category.",
        "timeliness, effort externalization", "binary_yes_no"),
    "local_news_event": (
        "Local event from the payload; connect it to footfall/delivery for this merchant with one suggested action.",
        "timeliness, curiosity", "open_ended"),
    "competitor_opened": (
        "A competitor opened nearby (name, distance, their offer, date from payload ONLY). Compare calmly to the merchant's "
        "own active offer/rating. Suggest one differentiation move, not a price war by default.",
        "loss aversion, social proof, specificity", "binary_yes_no"),
    "supply_alert": (
        "Batch/product recall or supply alert: molecule, batch numbers, manufacturer from payload/item exactly. "
        "Offer to draft the customer notice or the shelf-check list.",
        "urgency, specificity, effort externalization", "binary_yes_no"),
    # --- internal / performance ---
    "perf_spike": (
        "Good news with numbers (metric, % change, window, likely driver from payload). Suggest doubling down on the driver.",
        "reciprocity, curiosity, effort externalization", "binary_yes_no"),
    "perf_dip": (
        "Metric dropped (metric, % change, window, baseline). Be direct, not alarmist. Compare to peer stats if available. "
        "Offer one concrete fix you can do for them.",
        "loss aversion, specificity, effort externalization", "binary_yes_no"),
    "seasonal_perf_dip": (
        "Metric dipped but it is EXPECTED seasonally (payload says so). Reassure with the reason, then suggest the one move "
        "that works in this season. Contrarian, calm, data-informed.",
        "reassurance + curiosity, effort externalization", "binary_yes_no"),
    "milestone_reached": (
        "Milestone (current value vs milestone value from payload). If imminent, frame the small gap and a way to close it "
        "(e.g., ask happy customers for reviews).",
        "progress/goal-gradient, effort externalization", "binary_yes_no"),
    "review_theme_emerged": (
        "Review theme (theme, count, trend, a short paraphrase of the common quote). Acknowledge, suggest one operational fix "
        "and a reply template.",
        "loss aversion, specificity, effort externalization", "binary_yes_no"),
    "renewal_due": (
        "Subscription renewal (days remaining, plan, amount from payload). Show value delivered using their own performance numbers first, then the renewal ask.",
        "loss aversion, specificity", "binary_yes_no"),
    "winback_eligible": (
        "Lapsed subscriber: days since expiry and what changed since (perf dip %, lapsed customers added). Loss-framed but respectful.",
        "loss aversion, specificity", "binary_yes_no"),
    "trial_ending_soon": (
        "Trial ending: what they got during the trial (use performance numbers) and a simple yes to continue.",
        "loss aversion, specificity", "binary_yes_no"),
    "dormant_with_vera": (
        "Merchant silent for N days (payload). Don't guilt-trip. Re-open with one genuinely useful, specific observation from their data.",
        "reciprocity, curiosity", "open_ended"),
    "gbp_unverified": (
        "Google profile unverified: verification path and estimated uplift from payload. Offer to walk them through it in minutes.",
        "loss aversion, effort externalization", "binary_yes_no"),
    "curious_ask_due": (
        "Ask the merchant a genuine, easy question about their business this week (e.g., which service is most asked for), "
        "and promise something useful in return (a post/offer draft based on their answer).",
        "asking the merchant, reciprocity", "open_ended"),
    "active_planning_intent": (
        "Merchant ALREADY expressed intent (see merchant_last_message). Do not qualify again. Deliver a concrete first draft "
        "of what they asked for (structure, price points from catalog if relevant, timing) and ask for a go-ahead.",
        "effort externalization, momentum", "binary_yes_no"),
    "scheduled_recurring": (
        "Routine check-in: one fresh, specific observation or question — never generic.",
        "curiosity, asking the merchant", "open_ended"),
    # --- customer-facing ---
    "recall_due": (
        "Customer-facing, sent on behalf of the merchant. Name the due service and time since last visit, offer the exact "
        "slots from the payload (label them 1/2), include the relevant price only if it exists in merchant offers.",
        "convenience, specificity", "slot_choice"),
    "appointment_tomorrow": (
        "Customer-facing reminder of tomorrow's appointment with time; easy confirm/reschedule.",
        "convenience", "binary_yes_no"),
    "customer_lapsed_soft": (
        "Customer-facing gentle win-back referencing their last visit/service; one easy offer and one easy reply.",
        "reciprocity, convenience", "binary_yes_no"),
    "customer_lapsed_hard": (
        "Customer-facing win-back after a long gap: acknowledge the gap without guilt, reference their previous focus, "
        "one low-commitment way back (e.g., a single session/slot).",
        "reciprocity, low-friction yes", "binary_yes_no"),
    "trial_followup": (
        "Customer-facing follow-up after a trial: reference trial date, offer the next session option(s) from payload.",
        "momentum, convenience", "slot_choice"),
    "chronic_refill_due": (
        "Customer-facing refill reminder: medicines from payload, run-out date, delivery if address saved. Precise and trustworthy; "
        "no medical advice.",
        "convenience, loss aversion (running out)", "binary_yes_no"),
    "wedding_package_followup": (
        "Customer-facing bridal follow-up: wedding date / days to go and the next-step program from payload.",
        "timeliness, effort externalization", "binary_yes_no"),
}

DEFAULT_PLAYBOOK = (
    "Explain clearly why you are messaging now using the trigger payload, anchor on one concrete fact from the contexts, "
    "and make one low-friction ask.",
    "specificity, curiosity, effort externalization", "binary_yes_no")


def get_playbook(kind: str | None) -> tuple[str, str, str]:
    return PLAYBOOKS.get(kind or "", DEFAULT_PLAYBOOK)
