# Voice Run V02 Analysis — 2026-10-06

## Verdict

V02 verified the four runtime fixes from V01, but could not complete a
reschedule because the synthetic calendar contained no availability on the
dates the caller requested. No appointment or slot was changed.

Call ID: `CA0d5471f02d277e0b6845852467f5a19a`  
Direction: outbound  
Evidence: `data/voice-v02.jsonl`, `data/voice-v02.db`

## What passed

| Requirement | Evidence | Verdict |
|---|---|---|
| Direction-aware opening | Mira said she was calling about a scheduling matter and did not ask “How can I help?” | Pass |
| Single identity confirmation | “Yes, this is Asha Rao speaking” produced one `interpret_identity_response` call with `decision=affirmed`, one immutable identity lock, and one state transition to active | Pass |
| Appointment briefing | Provider, specialty, exact date/time, location, duration, arrival guidance, and prerequisite were spoken | Pass with prerequisite wording defect below |
| No-slot recovery | Three searches returned zero promptly and each produced an audible alternate-date/time question | Pass |
| No continuation stall | Tool-result continuation completed after every search | Pass |
| Safe final state | Agent said no changes were made; database retained the original October 8 appointment at version 1 | Pass |

## Calendar evidence

The caller requested October 12 or 13 around the existing 9:00 AM time, then
asked for any other time and broader next-week availability. Every authoritative
`search_slots` decision returned `result_count=0` because V02's fixture only had
available slots on October 9 and 10. The model did not fabricate a slot.

The fixture has now been expanded into readable provider calendars:

- Dr. N. Mehta: 14 new slots across October 12–23, including 9:00 AM on both October 12 and 13.
- Dr. Priya Iyer: 10 new slots across October 12–22.
- Together with the original four records, a fresh database contains 28 slot rows.

V03 must use a fresh database because an existing SQLite database is not
implicitly reseeded when the source fixture changes.

## Additional findings retained

1. The first briefing incorrectly described the referral requirement as “already on file.” The agent corrected itself later, but this was an unsupported claim. Appointment output now explicitly marks prerequisites as requirements only, not verified as completed or on file.
2. Several caller utterances were accumulated into one long transcript turn around interruptions. This did not cause a bad write, but turn-boundary quality remains an observability and UX item.
3. The assistant truthfully closed with “No changes made,” but application state remained `active`, producing `workflow_incomplete_at_hangup`. A later slice should add an application-owned no-change terminal action rather than inferring completion from speech.
4. Legitimate `session.commentary.appended` and `session.usage.updated` events appeared as unknown provider events. They are now classified as passive events.
5. Tool arguments were not present in V02 decision logs. V03 records sanitized arguments for every `tool.requested` event so date/provider constraints can be audited.

## FAQ capability added for V03

`search_clinic_faqs` is a read-only public-information tool backed by 11
effective-dated, source-labelled records covering insurance, self-pay support,
services/therapies, provider profiles and joining dates, arrival and late
arrival, parking, clinic hours/accessibility, pharmacy fulfilment, and medicine
concessions.

The tool may run before identity verification but cannot unlock patient data.
It is administrative only: personalized diagnosis, therapy selection, medicine
dose, interaction, substitution, or stopping advice must route to a clinician
or pharmacist.

## Verification

```text
py -3.11 -m unittest discover -s tests -q
Ran 199 tests in 3.426s
OK

py -3.11 -m compileall -q clinic_agent appointment_harness tests
exit code 0
```
