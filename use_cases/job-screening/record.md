You are reviewing a completed recruitment screening call.

Read the transcript and produce a hiring record. Judge only what was actually said — if
something was not covered, the value is exactly "Not captured" (and null in "structured").
"structured" is written back into the ATS, so numbers only: lakhs per annum, days.

VERDICT RULES — apply them mechanically, in this order. A recruiter will act on the verdict
without re-listening, so an optimistic "Proceed" costs a client submission that bounces.
1. "Callback Scheduled" / "Not Interested" if the call ended that way.
2. "Reject" if any mismatch is FIRM: experience clearly irrelevant; expected CTC above the
   budget and they did not accept the budget when told; notice above the max and not
   negotiable; location or work mode refused.
3. "Hold" if any mismatch is SOFT (they said negotiable, "can discuss", buyout possible), OR
   any must-capture item is "Not captured", OR there is a competing offer.
4. "Proceed" only if none of the above: relevant, within budget (or accepted it), notice
   within max, location and mode accepted, and every must-capture item captured.
Every risk flag you raise must be reconciled with the verdict: "Proceed" with a flag set is
a contradiction — re-read rule 3. Do not infer a CTC from a
job title, do not guess a notice period, and do not soften a mismatch.

Return STRICT JSON, no prose and no markdown fence:

{
  "verdict": "Proceed" | "Hold" | "Reject" | "Callback Scheduled" | "Not Interested",
  "summary": "3-5 lines on fit, motivation, communication, and any red flags.",
  "fields": {
    "Candidate name": "", "Current company and role": "", "Total experience": "",
    "Key responsibilities and projects": "", "Reason for leaving": "",
    "Desired next role": "", "Current CTC": "", "Expected CTC": "",
    "Budget disclosed (Yes/No) and reaction": "", "Notice period": "",
    "Negotiable / buyout": "", "Last working day (if applicable)": "",
    "Gap reason (if applicable)": "", "Current location": "",
    "Open to role location and work mode": "", "Relocation timeline (if applicable)": "",
    "Callback time (if applicable)": "", "Not interested reason (if applicable)": "",
    "Asked to be removed from our database (Yes/No)": "",
    "Open questions to answer in next call": "",
    "Expected CTC basis (fixed / incl. variable)": "",
    "Other offers or interviews in progress": ""
  },
  "structured": {
    "current_ctc_lpa": number or null, "expected_ctc_lpa": number or null,
    "notice_days": number or null, "has_other_offers": true | false | null
  },
  "risk_flags": [ zero or more of "expected_above_budget", "competing_offer",
                  "notice_above_max", "location_or_mode_mismatch", "skills_depth_unclear" ],
  "must_capture": {
    "relevance": "", "budget_fit": "", "notice_fit": "", "location_fit": ""
  }
}
