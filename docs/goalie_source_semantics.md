# Goalie source semantics

No authorized timestamped pregame goalie-report source or credential was found. NHL boxscores provide starter truth after the event and cannot be relabelled as pregame confirmation.

The existing report contract retains game/team, goalie id, internal state, source publication time when supplied, fetch time/provenance, and optional confidence. External words map conservatively: projected→`PROJECTED`, probable/likely→`PROBABLE`, expected→`EXPECTED`, and only explicit confirmed/starting language→`CONFIRMED`. Multiple source observations are retained; conflicts follow the existing lineage/governance path.

File-provider capture remains available for an authorized feed. No site was scraped. Verdict: **REAL TIMESTAMPED GOALIE SOURCE BLOCKED**.
