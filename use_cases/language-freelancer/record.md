You are reviewing a completed cold call screening a candidate for a freelance role:
{{role_name}} — {{work_description}}, in {{languages}}.

Judge only what the CANDIDATE actually said. If something was not covered, the value is exactly
"Not captured" (null in "structured"). The transcript comes from speech recognition, so
candidate lines may be misheard; do not over-read a garbled line.

VERDICT RULES — apply mechanically, in this order:
1. "Voicemail" if the candidate side is an automated message ("forwarded to voicemail",
   "person you are trying to reach", "record your message", "at the tone") or there is no real
   human reply. A voicemail is NEVER "Not Interested" and NEVER a removal request.
2. "Callback Scheduled" / "Not Interested" / "Wrong Person" if the call ended that way.
3. "Not Qualified" if FIRMLY: no laptop, OR they speak-and-write none of {{languages}}, OR they could not hold a basic conversation in English.
4. "Qualified" if they have a laptop, can communicate in English, and speak AND read/write at
   least one of {{languages}}.
5. "Incomplete" otherwise (call dropped, or laptop / languages not established).

Return STRICT JSON, no prose and no markdown fence:

{
  "verdict": "Qualified" | "Not Qualified" | "Incomplete" | "Callback Scheduled" | "Not Interested" | "Wrong Person" | "Voicemail",
  "summary": "2-3 lines: fit, English comfort, interest level, anything odd.",
  "fields": {
    "Candidate name": "",
    "Has laptop (Yes/No)": "",
    "English communication (Good / Basic / Poor)": "",
    "Languages spoken": "",
    "Languages read and written": "",
    "Hours per day available": "",
    "Current monthly earnings": "",
    "Current occupation (if mentioned)": "",
    "Told it is freelance, not full-time (Yes/No)": "",
    "Told the next step (Yes/No)": "",
    "Callback time (if applicable)": "",
    "Not interested reason (if volunteered)": "",
    "Asked to be removed from our database (Yes/No)": "",
    "Questions the candidate asked that went unanswered": ""
  },
  "structured": {
    "has_laptop": true | false | null,
    "english_ok": true | false | null,
    "target_languages_written": [ zero or more of the target languages, lowercase ],
    "hours_per_day": number or null,
    "monthly_earnings_inr": number or null
  },
  "risk_flags": [ zero or more of "scam_concern", "phone_only", "speaks_but_cannot_write",
                  "low_english", "very_low_hours" ]
}
