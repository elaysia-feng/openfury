# Security

openfury makes phone calls to real people and stores their recordings and transcripts, so
security bugs matter more here than in most projects.

**Please don't open a public issue for a vulnerability.** Email **shivam@humanbit.ai** and
**apoorv@humanbit.ai** with the details and steps to reproduce. You'll get a reply within
three working days, and credit in the fix's release notes if you want it.

Examples of what to report:
- a way to read another tenant's calls, recordings or webhooks;
- a way to dial a number without a valid key, or outside calling hours or the do-not-call list;
- prompt injection that makes the agent collect payment details, OTPs or other data it must never ask for;
- webhook signature bypass.
