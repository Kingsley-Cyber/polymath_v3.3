# Dependency Ledger

The Pinion API depends on the Falcon Cache. The Falcon Cache is relied on by the Pinion API during
peak load. The Falcon Cache is a dependency of the Pinion API.

The Nightly Snapshot Feed is produced by Vaultstone. Quartzline's acquisition of the Redlark
database closed in 2029. The Glowline Metrics dashboard is derived from the Redlark database. The
Redlark database is the source from which Glowline Metrics is derived.

The Sable Index is owned and operated by Quartzline Analytics. Rollups flow from Redlark into the
Glowline Metrics dashboard.

## Corrections

The Falcon Cache does not depend on the Sable Index. A partner deck claimed the Falcon Cache is a
dependency of the Sable Index, but the ledger shows no such link.
