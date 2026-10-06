# PLC/HMI reliability changes - superseded

This document describes the earlier three-source transition design. The final
two-physical-scale migration, including the TIA Portal build order and matching
Raspberry Pi transition, is documented in:

`../../docs/TIA_PORTAL_TWO_SCALE_MIGRATION_GUIDE.md`

Do not use the older recommendation below to keep Scale 2 and ET as separate
logical PLC readers. It is retained only as historical context until the
migration is commissioned.

The supplied TIA Portal V16 PDF confirms the current handshake:

- `0` - idle
- `1` - snapshot ready for the Pi
- `2` - Pi has committed the snapshot to SQLite

That sequence is correct and should remain. The following TIA changes are
required before the system can make a strict no-loss/no-duplicate guarantee.

## 1. Give every source a retained event sequence

Add a `DInt` event sequence to Scale 1, Scale 2, and ET. Increment it once when
the stable-weight capture network fires. Freeze it with the product snapshot.

The sequence and frozen snapshot must be retentive across PLC power loss.
Never reset the sequence during ordinary startup. Only a protected maintenance
operation may reset it.

The ERP identity should ultimately be:

```text
gatewayId + source + eventSequence
```

The current Pi adds a crash-safe local identity, but only a PLC-owned retained
sequence can remove the final ambiguity when the Pi is offline during a complete
PLC `2 -> 0 -> 1` cycle.

The `PLC_NEW` non-optimized layout has now been integrated into the Pi reader:

```text
DB1/DB7: state=0, weight=118, sequence=132, product=136,
         size=138, density=140, temperature=142, quality=144.0
DB12:    state=0, sequence=10, product=14, size=16, density=18,
         temperature=20, quality=22.0, weight=24
```

Because these standard/non-optimized DBs expose retentivity at whole-block
granularity in this TIA/CPU configuration, retaining the full DB is accepted.
The capture pulse is overwritten before its dependent networks on every
executed scan, and a retained non-idle handshake prevents a duplicate capture
after restart.

## 2. Freeze parameters separately for each source

DB9 currently has one shared set of `RecordProductCode`, `RecordSizeCode`,
`RecordDensity`, and `RecordTemperature`. Both scale function blocks call the
same copy function. A near-simultaneous second capture can overwrite the first
scale's frozen parameters.

Add these fields inside DB1 and DB7:

- `Gw_EventSequence`
- `Gw_ProductCode`
- `Gw_SizeCode`
- `Gw_Density`
- `Gw_Temperature`
- `Gw_QualityOk`

Add the equivalent ET fields inside DB12, including its own captured weight.
Copy every field into the source-specific DB before setting its ready state to
`1`. Set the ready state last.

## 3. Derive quality from the physical lane

- Scale 1: good/OK (`true`)
- Scale 2: rejected (`false`)
- ET: rejected/ET inventory (`false`)

Do not allow an HMI switch to alter quality after capture. If operator override
is required, freeze it into the source snapshot and audit the operator action.

## 4. Preserve the ACK rule

The PLC may change `1 -> 2` only when the Pi writes the acknowledgement. The PLC
may change `2 -> 0` only after the product is removed and live weight is below
the minimum threshold.

Do not use internet/API success as the PLC acknowledgement. SQLite persistence
is the acknowledgement boundary, so production continues while offline.

## 5. Correct the HMI tag

In the supplied export, HMI tag `Weight_Stable_2` is connected to
`DB_Weighing_1.LiveStable1`. Connect it to the correct Scale 2 stability tag.

Also make NodeJS state read-only on the HMI. Display:

- 0: Ready
- 1: Waiting for gateway persistence
- 2: Saved locally; remove product

Add alarms for a source remaining in state `1` or `2` beyond a configurable
duration.

Scale 2 and ET share one physical scale and one positive-edge memory. Their FB
calls must remain mutually exclusive. Do not automatically set `ET_Activate`
when the scale is idle; it is an operator-selected mode. Block mode changes
while either NodeJS state is nonzero or the scale is not empty.

## 6. Add gateway health signals

Add a Pi heartbeat counter and last-seen indicator in a dedicated gateway DB.
The HMI should alarm if the counter stops changing. This is monitoring only; it
must never clear a pending product automatically.

## 7. Commissioning tests

Run these tests with uniquely labelled physical products:

1. Disconnect internet for one hour while weighing on both lanes.
2. Restart the Pi after PLC ready state `1`, before acknowledgement.
3. Restart the Pi immediately after acknowledgement `2`.
4. Power-cycle the PLC with one unacknowledged captured product.
5. Capture on both scales as close together as practical.
6. Return HTTP 500 and timeout responses from a test backend.
7. Deliver the same batch repeatedly.
8. Verify physical count = SQLite distinct event count = Mongo distinct event
   count, and inventory count matches only eligible products.

Do not declare commissioning complete until every test reconciles exactly.
