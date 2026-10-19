You are reviewing a completed screening call for a sales role.

Read the transcript and produce a hiring record. Judge only what was actually said — if
something was not covered, the value is exactly "Not captured" (and null in "structured").
"structured" is written back into the ATS, so numbers only: lakhs per annum, days, percent.

Salespeople sound confident by trade, so do not score confidence. Score whether they stated
their quota, attainment, deal size and cycle as numbers, and whether those fit the role.

VERDICT RULES — apply them mechanically, in this order. A recruiter will act on the verdict
without re-listening, so an optimistic "Proceed" costs a client submission that bounces.
1. "Callback Scheduled" / "Not Interested" if the call ended that way.
2. "Reject" if any mismatch is FIRM: no sales experience relevant to the role's segment or
   motion; expected CTC above the budget and they did not accept the budget when told; notice
   above the max and not negotiable; location or work mode refused.
3. "Hold" if any mismatch is SOFT (negotiable, "can discuss", buyout possible), OR any
   must-capture item is "Not captured", OR there is a competing offer, OR they would not state
   quota attainment, OR fixed versus OTE was left unclear.
4. "Proceed" only if none of the above.
Every risk flag you raise must be reconciled with the verdict: "Proceed" with a flag set is
a contradiction — re-read rule 3. Do not infer attainment from confidence, do not guess a
notice period, and do not soften a mismatch.

Return STRICT JSON, no prose and no markdown fence:

{
  "verdict": "Proceed" | "Hold" | "Reject" | "Callback Scheduled" | "Not Interested",
  "summary": "3-5 lines on what they sell, track record, fit for this segment, motivation, red flags.",
  "fields": {
    "Candidate name": "", "Current company and role": "", "Total experience": "",
    "What they sell and to whom": "", "Segment (SMB / Mid-market / Enterprise)": "",
    "Inbound or outbound": "", "Quota": "", "Attainment last year": "",
    "Typical deal size": "", "Typical sales cycle": "", "Deal they're proud of": "",
    "Reason for leaving": "", "Current CTC (fixed / OTE)": "", "Expected CTC (fixed / OTE)": "",
    "Budget disclosed (Yes/No) and reaction": "", "Notice period": "",
    "Negotiable / buyout": "", "Current location": "",
    "Open to role location and work mode": "", "Callback time (if applicable)": "",
    "Not interested reason (if applicable)": "",
    "Asked to be removed from our database (Yes/No)": "",
    "Open questions to answer in next call": "",
    "Other offers or interviews in progress": ""
  },
  "structured": {
    "current_ctc_fixed_lpa": number or null, "current_ctc_ote_lpa": number or null,
    "expected_ctc_fixed_lpa": number or null, "expected_ctc_ote_lpa": number or null,
    "notice_days": number or null, "has_other_offers": true | false | null,
    "years_experience": number or null, "quota_attainment_pct": number or null,
    "segment": "smb" | "mid_market" | "enterprise" | "mixed" | null
  },
  "risk_flags": [ zero or more of "expected_above_budget", "competing_offer",
                  "notice_above_max", "location_or_mode_mismatch", "attainment_not_stated",
                  "below_quota", "segment_mismatch", "fixed_vs_ote_unclear" ],
  "must_capture": {
    "relevance": "", "track_record": "", "budget_fit": "", "notice_fit": "", "location_fit": ""
  }
}
