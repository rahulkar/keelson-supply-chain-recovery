# DC-to-DC transfer SOP

> Synthetic demo document. "Demo Retail", its distribution centres, lanes, times and figures in this
> document are fictional and exist only to demonstrate Keelson. Not a real operating procedure.

## 1. Purpose

This standard operating procedure (SOP) explains how stock is moved between Demo Retail's European
distribution centres to rebalance inventory when one DC runs short and another holds surplus.
It is the first lever on the peak trading lever ladder.

## 2. Network

| From | To | Road transit | Notes |
|---|---|---|---|
| DE1 Erfurt | NL1 Venlo | 2 days | Daily trailer |
| DE1 Erfurt | UK1 Thurrock | 4 days | Customs documents required |
| PL1 Poznań | NL1 Venlo | 3 days | Three trailers a week |
| PL1 Poznań | DE1 Erfurt | 2 days | Daily trailer |
| NL1 Venlo | UK1 Thurrock | 2 days | Customs documents required |

Weekly lane capacities and per-unit costs are kept in the tenant network configuration and are the
numbers the planning tools use.

## 3. When to transfer

Transfer stock when all of these are true:

- The receiving DC is forecast to run short of a SKU within the next 4 weeks.
- The sending DC keeps at least **3 weeks of cover** for that SKU after the transfer, including
  its own planned store allocations.
- The units arrive at least 2 days before the receiving DC's first short week.

Never transfer stock that is already allocated to a store wave that has been released for picking.

## 4. Steps

1. **Propose.** The planner proposes a transfer order per lane with SKU-level quantities. Planning
   tools may draft it; a human reviews it.
2. **Check.** The receiving DC confirms dock slots; the sending DC confirms the stock is free (not
   on hold, not in quality quarantine).
3. **Approve.** Transfers up to 2,000 units per lane per week can be approved by the planner on
   duty. Larger transfers need the Supply Planning Manager.
4. **Execute.** The transfer order is released in the ERP; the sending DC picks and ships within
   24 hours of release.
5. **Confirm.** The receiving DC books the receipt; the planner checks that the ERP shows the stock
   at the new location.

## 5. Peak rules

- From W46, transfers out of a DC are limited to stock above 3 weeks of cover **and** above the
  presentation minimum of every cluster the DC serves.
- UK-bound transfers need customs paperwork prepared 24 hours before departure.

## 6. Rollback

A transfer order that has not been picked can be cancelled in the ERP. Once picked, the stock
travels and is rebalanced again later if needed; it is never sent back automatically.
