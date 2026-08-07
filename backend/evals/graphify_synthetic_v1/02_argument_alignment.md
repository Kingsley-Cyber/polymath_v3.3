# Quartzline Storage Notes

Quartzline's archival store, Vaultstone, consumes the Nightly Snapshot Feed. One service, the
Pinion API, uses the Falcon Cache. The Redlark database supports the Glowline Metrics dashboard.
The Falcon Cache layer depends on the Sable Index.

Vaultstone, which Quartzline owns, exposes a retrieval endpoint. A component called Trellis is part
of the Pinion API. The Glowline Metrics dashboard, a Quartzline product, depends on the Redlark
database.

## Batch behavior

The Pinion API produces the Audit Summary Report and sends rollups to Redlark. The archival store
retains snapshots for seven years.

## Corrections

An onboarding document claimed that Trellis is part of Redlark; the diagram was fixed in review.
The Sable Index is depended on by the Falcon Cache layer during warm starts.
