# Northline Advisory

Weekly delivery brief · 2026-10-10 · Audience: Delivery director

Source: canonical · 25 work items · rules 2026-10-11 · result e53f514aa6

**Approval: pending.** A person still has to check this brief before it goes to a customer.

As of 10 Oct 2026, 1 of 4 projects is on track (Aster HR — Payroll export). This brief cites rows in the approved export. It does not forecast that a future date will be missed.

## Delivery pulse

| Project | State |
| --- | --- |
| Aster HR — Payroll export | On track |
| Cedar Clinics — Patient portal | Needs attention |
| Forge Logistics — Warehouse API | Blocked |
| Lumen Bank — Mobile onboarding | Needs attention |

## Top risks

### FOR-12 is Highest priority, open, and has no assignee.

FOR-12 (Rotate carrier credentials) is In Progress, due 8 Oct 2026 (before this report), and the assignee cell is empty. The item comment says "Unassigned after Alex Rahman went on leave. Due yesterday."

Confidence: fact. Severity: 90.

Evidence: work_items:FOR-12.assignee=(blank), work_items:FOR-12.priority=Highest, work_items:FOR-12.status=In Progress, work_items:FOR-12.due=2026-10-08, work_items:FOR-12.comment=Unassigned after Alex Rahman went on leave. Due yesterday.

### LUM-4 is Highest priority, open, and has no assignee.

LUM-4 (KYC document upload) is To Do, due 6 Oct 2026 (before this report), and the assignee cell is empty. The item comment says "Critical path. No owner after the previous owner left the squad."

Confidence: fact. Severity: 90.

Evidence: work_items:LUM-4.assignee=(blank), work_items:LUM-4.priority=Highest, work_items:LUM-4.status=To Do, work_items:LUM-4.due=2026-10-06, work_items:LUM-4.comment=Critical path. No owner after the previous owner left the squad.

### Cedar Clinics — Patient portal is described as on track, but the export disagrees.

N-1 on 9 Oct 2026 says the work is on track. CED-2 and CED-3 are already overdue. The newest update among these open items is 20 Aug 2026. CED-2 due 30 Sep 2026, updated 18 Aug 2026, CED-3 due 2 Oct 2026, updated 20 Aug 2026.

Confidence: inference. Severity: 86.

Evidence: notes:N-1.text=Weekly status: Cedar is green and on track. No risks to raise. The team is heads down on the portal., notes:N-1.date=2026-10-09, work_items:CED-2.status=In Progress, work_items:CED-2.updated=2026-08-18, work_items:CED-2.due=2026-09-30, work_items:CED-3.status=In Progress, work_items:CED-3.updated=2026-08-20, work_items:CED-3.due=2026-10-02

### FOR-20 is blocked by FOR-12, which is overdue (8 Oct 2026) and unassigned.

FOR-20 (Sandbox returns 401 for pick tickets) is Blocked by FOR-12 (Rotate carrier credentials). FOR-12 is In Progress, overdue (8 Oct 2026) and unassigned. Carrier cutover (MS-FOR) is due 15 Oct 2026.

Confidence: fact. Severity: 85.

Evidence: work_items:FOR-20.status=Blocked, work_items:FOR-20.blocked_by=FOR-12, work_items:FOR-12.status=In Progress, work_items:FOR-12.assignee=(blank), work_items:FOR-12.due=2026-10-08, dependencies:DEP-CUT.successor=MS-FOR, milestones:MS-FOR.due=2026-10-15

### Lumen Bank — Mobile onboarding reports 85% complete; 8 of 37 story points are done.

LUM-1 percent_complete is 85. Child items with story points total 37, of which 8 are Done (22%). Still open at High or Highest: LUM-4 (KYC document upload), LUM-5 (Sanctions screening integration), LUM-6 (Biometric capture). N-2 says "Leadership slide says onboarding is 85% complete and the only open item is copy for the app store."

Confidence: fact. Severity: 80.

Evidence: work_items:LUM-1.percent_complete=85, work_items:LUM-2.status=Done, work_items:LUM-2.story_points=5, work_items:LUM-3.status=Done, work_items:LUM-3.story_points=3, work_items:LUM-4.status=To Do, work_items:LUM-4.story_points=8, work_items:LUM-5.status=To Do, work_items:LUM-5.story_points=8, work_items:LUM-6.status=To Do, work_items:LUM-6.story_points=5, work_items:LUM-7.status=To Do, work_items:LUM-7.story_points=3, work_items:LUM-8.status=To Do, work_items:LUM-8.story_points=3, work_items:LUM-10.status=To Do, work_items:LUM-10.story_points=2, notes:N-2.text=Leadership slide says onboarding is 85% complete and the only open item is copy for the app store.

## Changes since the previous export

- FOR-12 (Rotate carrier credentials): assignee: Alex Rahman → blank.
- LUM-3 (Email verification): status: In Progress → Done, percent_complete: 60 → 100.

## Decisions needed

- LUM-9 (Approve KYC vendor contract): owner missing, due 3 Oct 2026. Overdue. Unknown: owner. Successor: LUM-4.

## Set aside

- DEP-LEG says MS-LEG precedes FOR-3. Legacy data extract (MS-LEG) is overdue, and FOR-3 is Done. N-5 says "We no longer need the legacy data extract. FOR-3 was finished from the snapshot feed in July."

## Also noted

- LUM-5 is Highest priority, open, and has no assignee.

## Questions for the delivery lead

- Who owns FOR-12 (Rotate carrier credentials)?
- Who owns LUM-4 (KYC document upload)?
- What changed on Cedar Clinics — Patient portal after 20 Aug 2026? N-1 and the work-item export disagree.
- Does the dependent milestone still hold while FOR-20 is blocked by FOR-12?
- Which completion figure should the steering pack use: 85% or 8 of 37 story points?
- Who can close LUM-9 (Approve KYC vendor contract)? No owner is named. Recorded successor: LUM-4.

## Proposed actions

The questions above are the proposed actions. Nothing has been sent, and nothing in a project system changes until a person approves it.


## How this brief was produced

- Stale means an In Progress, Blocked, or Waiting item with no update in 14 days.
- A completion figure is treated as misleading when an epic reports at least 70% and at most half of its child story points are Done.
- An overdue predecessor is set aside when its successor is already Done.
- Jira Blocker counts as Highest and Major counts as High. Three or more unassigned items of one priority are reported as a count.
- Stale and overdue items are raised without a notes file. A note that says the work is on track is still compared with the export.
- Optimistic notes are an inference. The dates and statuses they are compared with are facts.
- These thresholds are operating rules for the sample, not a validated predictor of delay.

Synthetic sample.
