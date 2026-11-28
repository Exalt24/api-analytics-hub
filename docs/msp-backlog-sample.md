# MSP backlog analysis

As of 2026-11-29T12:00:00Z, window 2026-10-31 to 2026-11-30 (end not included). Read: 400 tickets, 710 time entries, 45 alerts, 30 devices, from autotask, connectwise, ninjaone.

## Ticket drivers

393 tickets opened in the window, in 8 driver groups. Repeats are tickets after the first for the same company.

| Category | Issue type | Title | Tickets | Companies | Repeats | Repeat % |
|---|---|---|---|---|---|---|
| Service Desk | Password reset | password reset for user # | 108 | 12 | 94 | 87 |
| RMM Alerts | Storage | disk space low on srv # | 86 | 12 | 73 | 84 |
| Service Desk | Printing | printer offline at site # | 54 | 12 | 42 | 77 |
| RMM Alerts | Backup | backup failed on srv # | 39 | 11 | 25 | 64 |
| Network | Connectivity | vpn not connecting for user # | 33 | 10 | 23 | 69 |

By category

| Category | Issue type | Tickets | Share % | Repeats |
|---|---|---|---|---|
| Service Desk | Password reset | 108 | 27 | 94 |
| RMM Alerts | Storage | 86 | 21 | 73 |
| Service Desk | Printing | 54 | 13 | 42 |
| RMM Alerts | Backup | 39 | 9 | 25 |
| Service Desk | Email | 37 | 9 | 22 |

## Alert drivers

45 active alerts on 14 devices (30 devices listed, 2 offline).

| Source type | Source | Alerts | Devices | Repeats | Share % |
|---|---|---|---|---|---|
| CONDITION_AGENT_DISK_FREE_SPACE | Disk free space low | 24 | 10 | 14 | 53 |
| AGENT_OFFLINE | Agent offline | 10 | 7 | 3 | 22 |
| CONDITION_AGENT_CPU | CPU over 90 percent | 5 | 3 | 2 | 11 |
| CONDITION_AGENT_MEMORY | Memory over 90 percent | 3 | 3 | 0 | 6 |
| CONDITION_PING | Ping lost | 3 | 3 | 0 | 6 |

Noisiest devices

| Device | Alerts | Sources |
|---|---|---|
| SRV-04 | 12 | 4 |
| SRV-01 | 8 | 3 |
| SRV-02 | 6 | 4 |
| SRV-03 | 4 | 2 |
| SRV-08 | 3 | 2 |

## SLA risk

28 open tickets at 2026-11-29T12:00:00Z: 16 breached, 1 at risk within 4 hours, 11 fine, 0 with no goal; 2 first responses overdue.

| Category | Priority | Open | Breached | At risk | OK | No goal | Response overdue | Oldest (hours) |
|---|---|---|---|---|---|---|---|---|
| Service Desk | P4 Low | 6 | 3 | 0 | 3 | 0 | 1 | 419 |
| RMM Alerts | P2 High | 3 | 3 | 0 | 0 | 0 | 0 | 571 |
| Service Desk | P3 Normal | 5 | 2 | 0 | 3 | 0 | 0 | 65 |
| Onboarding | P3 Normal | 4 | 2 | 0 | 2 | 0 | 0 | 51 |
| Network | P4 Low | 3 | 2 | 0 | 1 | 0 | 1 | 581 |

Flow in the window: 54 of 356 tickets finished in the window with a goal finished late (15%).

| Category | Opened | Finished | Net |
|---|---|---|---|
| Service Desk | 215 | 204 | 11 |
| RMM Alerts | 125 | 118 | 7 |
| Onboarding | 20 | 16 | 4 |
| Network | 33 | 30 | 3 |

## Technician touch time

348.91 hours in 705 entries in the window: 341.91 placed on 4 categories, 7.00 on no ticket, 0.00 on a ticket not in the ledger.

| Category | Hours | Entries | Tickets touched | Mean hours per ticket | Most time (ticket, hours) | Technicians (hours) |
|---|---|---|---|---|---|---|
| RMM Alerts | 167.66 | 224 | 108 | 1.55 | 1163, 5.00 | Technician C 41.50; Technician B 38.50; Technician A 30.83; Technician D 30.16; Technician E 26.66 |
| Service Desk | 141.58 | 381 | 172 | 0.82 | 1167, 2.08 | Technician B 33.00; Technician A 28.58; Technician D 28.16; Technician C 27.50; Technician E 24.33 |
| Network | 20.16 | 57 | 27 | 0.74 | 1136, 1.83 | Technician C 6.50; Technician B 5.00; Technician D 3.58; Technician E 3.25; Technician A 1.83 |
| Onboarding | 12.50 | 34 | 15 | 0.83 | 1362, 2.16 | Technician A 3.08; Technician B 3.08; Technician C 3.08; Technician E 1.66; Technician D 1.58 |

## What this does not see

- Ticket drivers count the tickets opened in the window; repeats are counted per company, so a ticket with no company is never a repeat.
- At risk is a count of open tickets whose goal falls inside the horizon, not a forecast; priorities are not ranked against each other.
- Time that cannot be placed on a ticket in the ledger is reported as non_ticket_seconds and unmatched_seconds, never spread over the categories.
- ConnectWise: the goals of an open ticket are what the ticket list gave, which its schema says may not be current unless the sync used refresh_open_sla.
- Autotask: a ticket is closed when it has a completed date, and the resolution goal is the field the connector was told to read.
- NinjaOne: only ACTIVE alerts are listed, so an alert reset before the sync is not here and the noisy sources describe what is still open.
