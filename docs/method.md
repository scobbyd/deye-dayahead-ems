# Method: how a day is scored

The harness answers one question per day: what would the battery have earned
on this day, planned by EMHASS with a given amount of knowledge, settled on
the day that actually happened. This page is the accounting. The code is
`backtest/ladder.py` (the plan and the settlement), `emhasscore/scoring.py`
(`replay_day`), `backtest/hardware.py` (the capacity sweep) and
`backtest/quickcheck.py` (the tables).

## One solve per lane per day

Every lane solves ONE plan at 00:00 local of day D:

- from the measured SOC on the first quarter of D (`soc_pct`), as a fraction;
- over the lane's horizon (`P0` one day, `P1` and every F lane two days, `P2`
  three days), on the 15-minute grid, DST days at their real step count;
- with `soc_final` pinned to 0,5 at the horizon end (`battery.soc_final_target`
  in `plant.json`): every lane hands back the pack at the same level, so a
  day is never credited for emptying it into the next;
- with the real 15-minute day-ahead prices for the whole horizon, through the
  tariff (fee, tax, VAT on buy; fee off sell). Price knowledge is equal across
  lanes: the ladder measures PV and load knowledge only;
- with the plant's own stress costs (the quadratic loss coefficients priced
  at the horizon's mean price) and the relaxed rebalance knobs;
- with the lane's PV column as the LP's curtailable PV and the lane's load
  column minus the microinverter as the LP's load. The microinverter is
  must-take: negative load, never through the hybrid's bridge. After the solve
  it is put back into both PV and load so the rows read gross.

The LP's plant is `plant.json`: capacity, charge and discharge power, the
inverter's AC input and output maximum (12 kW on the reference), the grid
connection (17.250 W), the SOC band (10 % to 100 %), the DC-stage
efficiencies (0,961 charge, 0,957 discharge).

The solver is the EMHASS 0.18.2 library in process, wrapped so it has the
add-on's `solve()` signature and result shape. On an archived reference plan
of 229 steps the library returns the same objective (25,183415 EUR) and the
same `P_batt`, `P_grid`, `SOC_opt` and cost trajectories to 0,000000, in
0,4 s warm. The `backtest.hindsight` reproduction of seven live scoreboard
days matched float for float.

## The settlement: the plan's battery on the measured day

The plan's battery decisions are laid on the day that happened. The
settlement is `scoring.replay_day`, written as a DIFFERENCE from the
measured grid trace:

```
B_plan(t)    = P_load(t) - (P_pv(t) - P_curtail(t)) - P_grid(t)   the plan's battery at the AC node,
                                                                  read off the row balance
B_meas_ac(t) = eta x B_dc(t)  discharging,  B_dc(t) / eta  charging   (eta = the bridge, 0,989)
G_replay(t)  = G_meas(t) - B_plan(t) + B_meas_ac(t) - (PV_pot(t) - PV_meas(t))
```

Why not the absolute form (`G = load - PV - battery`)? The site does not
balance. About 4,2 kWh a day of inverter standby, conversion loss and
unmetered circuits sits between `load - PV - battery` and the meter, and an
absolute reconstruction hands that residual to whichever lane is rebuilt:
up to 0,70 EUR a day, larger than the effect being measured. In the
difference form every unmodelled term appears in both halves and cancels.
The bridge efficiency is the only coefficient needed. Same house, same PV,
same load, same standby on the common path; the plan's battery instead of
the real one (or instead of none).

The last term is the curtailment repair. Where the real array was held back
because the real pack was full, the counterfactual house whose pack had room
would have had that energy, and it leaves through the meter. For every lane
the repair runs on the day's TRUE potential (`pv_pot_main_w + micro_w`),
never on the lane's own PV series: an F lane's PV is a forecast, and a repair
on it would credit every watt of over-forecast as free export. Where
`pv_pot_main_w` equals `pv_main_w` the term is zero.

The day's score is

```
eur = cash + soc_term + loss          EUR out of the house, lower is better
```

- `cash`: `G_replay` priced per step, import at buy, export at sell.
- `soc_term`: `(SOC_start - SOC_end) x capacity x lambda`, with lambda 0,9 x
  the day's mean sell price. Positive when the pack ends lower than it
  started. With `soc_final` pinned and the frame's SOC as the start, this
  term is what the plan chose to carry, priced.
- `loss`: the quadratic stage losses, below.

`quickcheck` flips the sign in its tables so a gain reads positive.

## The 12 kW bridge cap

`replay_day` applies the plan's commanded battery to the measured day as it
stands. A plan that under-forecast the evening sun commands a discharge that,
next to the REAL PV, would put up to 19 kW through a 12 kW bridge. `P1`,
which knows the sun, is bridge-capped inside its own LP; an F lane is not.
So before the day is settled the commanded battery power is clamped per step
against the day's true main-string harvest (ruling 2026-09-11):

```
harvest  = max(0, pv_pot_main - the plan's own curtailment)
b_capped = clip(b, -12 kW - harvest, 12 kW - harvest)          W, + = discharge
```

This is exactly the constraint the LP enforces on its own PV
(`inverter_ac_output_max` on `p_pv - p_pv_curtailment + p_sto`). Main
strings only: the microinverter sits on the gen port and never crosses the
bridge; capping on the gross potential would dock a P lane on every step its
LP legitimately saturated the bridge while the microinverter was producing
(8 to 9 steps a day on the June reference). The grid absorbs the difference,
and the pack keeps the energy that did not move: the SOC shifts by the
withheld discharge through the discharge efficiency and the withheld charge
through the charge efficiency, held inside the SOC band. A P lane is never
capped. On the reference season the F lanes were capped on 2,7 to 3,8 steps
a day, and the cap moved them by +12 to +18 EUR over 146 days. Measured
after the cap: `P1` settles worse than an F lane on 31 of 665 (day, lane)
pairs, all within 0,08 EUR but three `F_da` days (13 March 0,76, 18 August
0,20, 3 May 0,19). The bridge is `inverter.p_nom_kw` at call time, so a
capacity sweep with `--inverter` moves it.

## Quadratic loss pricing

The Deye's stage losses have a linear part (inside the efficiencies) and a
quadratic part. The LP prices the quadratic part as a stress cost; the
settlement charges it on the replayed flows:

```
loss(t) = (Q_PORT x P_batt^2 + Q_BRIDGE x P_hybrid^2) x 0,25 h x price(t)
```

with `P_batt` the plan's battery in kW, `P_hybrid` the true main potential
plus that battery in kW (a charging `P_batt` is negative, so DC-coupled PV
going into the pack never crosses the bridge, which is the asymmetry the two
coefficients exist to price), `price(t)` the slot's mid price
`(buy + sell) / 2`, `Q_PORT` 0,003 kW per kW² and `Q_BRIDGE` 0,0012 kW per
kW² (`inverter.q_port_kw_per_kw2`, `q_bridge_kw_per_kw2`; calibrated on the
12 kW Deye after the star rewire, 0,0037 and a sum of 0,00483 before it).

## The curtailment rule

The reference plant holds the main strings back when the pack is at or
above `pv.curtail_soc_pct` (95 %) AND export is off. In the frame this is
the `suspect` mask and the gap between `pv_pot_main_w` and `pv_main_w`. The
reference frame's rule: hourly SOC maximum at or above 95 % AND a negative
day-ahead price in the hour (the negative price stands in for the export
switch, which only goes off in the negative-price regime). It marks 201
hours over March to 8 September against 744 for a SOC-only mask, and adds
736 kWh of potential against 1.294. The mask is the settlement's repair
witness: on a suspect hour the potential is the plant model's energy in the
model's 15-minute shape, on a clean hour it equals the measurement, so the
repair is zero there.

Inside the LP every lane may curtail its own PV (`P_curtail`), and on a
negative-price step it will: import at a negative price instead of exporting
at one. A house without a battery on this inverter has the same lever under
the zero-export rule, which is why the no-battery baseline gets no repair on
negative-price steps (next section).

## The no-battery lane and `batt_gain`

Per day and lane the ladder also settles the same house with NO battery, at
the day's prices, with no solve:

```
G_nobatt(t) = G_meas(t) + B_meas_ac(t) - repair(t)
repair(t)   = max(0, PV_pot(t) - PV_meas(t))  where sell(t) >= 0, else 0
```

The real pack's AC contribution is removed (through the bridge efficiency,
the same conversion `replay_day` uses), and where the real array was held
back because the real pack was full a battery-less site would have exported
that energy, so the baseline gets it too, or the battery's value would be
biased on every curtailed day. On a negative-price step it gets nothing.

`nobatt_eur` is that trace priced in the lane's tariff. `batt_value_eur` in
the ladder CSV is `eur - nobatt_eur` (negative when the battery lowered money
out); `quickcheck` prints it flipped as `gain EUR` = `nobatt - eur`, positive
when the battery paid. `eur_per_kwh` divides it by the discharged kWh
(undefined below 0,05 kWh); `cycles` is charge plus discharge throughput
over twice the capacity.

On a frame with no battery (`batt_dc_w` 0), `B_meas_ac` is zero and the
baseline is the meter as recorded: the battery gain is then exactly what the
planned pack would have changed on your real day.

## What the ladder gap means

The same day is planned several times, each lane knowing a different amount,
and every lane is settled with the same accounting. The gap between two lanes
is the value of what one knew and the other did not.

| lane | horizon | PV | load |
|---|---|---|---|
| `P0` | 1 day | true potential | measured |
| `P1` | 2 days | true potential | measured |
| `P2` | 3 days | true potential | measured |
| `F_da` | 2 days | Open-Meteo 24 h lead on D, 48 h on D+1 | 7-day median profile |
| `F_sol` | 2 days | Solcast P50 as last issued | 7-day median profile |
| `F_mix` | 2 days | 0,8 P50 + 0,2 P10 | 7-day median profile |
| `F_om0` | 2 days | Open-Meteo freshest run | 7-day median profile |

`P1` is the reference: two-day perfect foresight of PV and load. `P1` minus
an F lane is the cost of forecasting. `P1` minus `P0` is the value of the
second horizon day. `P2` minus `P1` is the value of the third.

On the reference plant over 146 shared days (14 April to 6 September 2026,
48,2 kWh pack, 12 kW hybrid, the plant's tariff):

| lane | total EUR | vs P1 | battery value EUR | discharged kWh | cycles/day | EUR/kWh |
|---|---|---|---|---|---|---|
| `P2` | -1.225,01 | -0,21 | -1.011,41 | 6.728 | 0,945 | -0,150 |
| `P1` | -1.224,80 | 0,00 | -1.011,21 | 6.727 | 0,946 | -0,150 |
| `F_da` | -1.194,71 | 30,10 | -981,11 | 6.594 | 0,932 | -0,149 |
| `F_sol` | -1.193,15 | 31,66 | -979,55 | 6.629 | 0,942 | -0,148 |
| `F_mix` | -1.193,14 | 31,66 | -979,54 | 6.583 | 0,937 | -0,149 |
| `F_om0` | -1.192,88 | 31,93 | -979,28 | 6.561 | 0,931 | -0,149 |
| `P0` | -1.145,28 | 79,53 | -931,68 | 5.654 | 0,876 | -0,165 |

Readings, on that plant: the four forecast lanes cost 30,1 to 31,9 EUR more
than two-day perfect foresight, about 0,21 to 0,22 EUR a day, and sit 2 EUR
apart from each other. The third horizon day is worth 0,21 EUR over the
season; a one-day horizon with the pack pinned to 50 % at midnight costs
79,5 EUR, 22 EUR of it in June alone. The battery itself is worth about
1.011 EUR over those days against the no-battery baseline, 0,15 EUR per
discharged kWh at 0,93 to 0,95 equivalent cycles a day, and forecast quality
moves that value by about 3 %. The lanes differ in what they knew, not in
how hard they drove the pack. The incumbent supplier EMS's own payout over
the same 146 days was 1.096,85 EUR in, 48 to 128 EUR less than the lanes
bring in, with caveats: its payout includes aFRR volumes and imbalance
settlement the lanes do not model, it is settled in the supplier's tariff,
and neither side carries a bridge cap.

`F_om0` is the historical-forecast API's stitched freshest run at 1 to 3 h
lead, so on the second horizon day it holds forecasts issued long after the
midnight solve. No planner can have it. It is an upper bound on Open-Meteo
skill at short lead, not a lane to rank against `F_da`.

The sanity gate: perfect foresight may not lose to a forecast lane on the
same day. After the bridge cap it fails on 35 of 584 shared-day pairs on the
reference (medians 0,017 to 0,054 EUR), all within 0,09 EUR except eight
pairs on five days, the largest 0,23 EUR. That residue is the LP's soft
costs the settlement does not price (the SOC deficit and surplus costs): `P1`
pays them in its objective and holds back; a forecast lane whose error
happens to push it past them is paid at real prices.

## The capacity sweep

`quickcheck --capacity 24.1,48.2 [--inverter 12]` runs the ladder once per
pack size. `backtest/hardware.py` moves every place the two numbers sit:

| where | what moves |
|---|---|
| the LP (`patch_solver`) | `battery_nominal_energy_capacity` (Wh), `inverter_ac_output_max` and `inverter_ac_input_max` (W), `battery_charge_power_max` and `battery_discharge_power_max` |
| the settlement | `capacity_kwh` passed to `ladder.run`: the SOC term, the SOC shift under the bridge cap, the cycle count |
| the bridge cap and the stress cost | `objective.P_NOM_INV_KW`, read at call time |
| the actuator model | `deye.DEYE_CURRENT_MAX_A`, scaled from its shipped 240 A at 12,5 kW (336 A for a 17 kW unit) |

The battery power rule: the pack's charge and discharge maximum is the
inverter's AC nominal plus 500 W (12 kW gives 12.500 W, as the reference
`config.json` has it).

The start SOC does not scale. Each day's plan starts from the frame's
measured `soc_pct`, a percentage of the REAL pack, and the swept pack starts
at the same percentage: a sweep on a frame from a house with a battery
re-scales the measured start SOC onto the swept size (50 % of 48,2 kWh
becomes 50 % of 24,1 kWh). With `soc_final` pinned at the same percentage the
day's carry-over is still zero on average, but the energy the swept pack
holds at midnight is the swept pack's, not the measured one's. For a house
without a battery `soc_pct` is the constant 50 and this is a non-issue. What does NOT scale: `Q_PORT` and `Q_BRIDGE`
(calibrated on the 12 kW Deye; a 17 kW unit would have its own), the grid
connection (17.250 W), the BMS current per module. A run with a bigger
inverter carries the 12 kW unit's loss shape.

Each capacity writes its own ladder CSV under `--out`
(`ladder_c24.1_i12.csv`; the plant's own size is `ladder_plant.csv`) and the
run is resumable per (day, lane). Read the yearly table across capacities to
see where the marginal kWh stops paying: on the synthetic example the pack
gain per kWh cycled falls from 0,190 at 24,1 kWh to 0,157 at 48,2 kWh.

## Known limits

- **DST days.** The library loses steps across the spring change (a
  189-step horizon from 23:45 the night before came back with 188 rows).
  `backtest/solver.py` pads the request by four steps and trims the answer,
  so the day solves, but the LP sees one extra hour at its far end. Two days
  a year. The synthetic example avoids the transition windows.
- **Soft costs.** The settlement prices the quadratic losses and the bridge
  cap; it does not price the LP's SOC deficit and surplus costs. That is the
  sanity-gate residue above.
- **The potential must be the strings' potential.** If `pv_pot_main_w` is
  the metered PV, curtailment is invisible: the settlement credits no repair,
  the no-battery baseline gets none either, and a house that throttled its
  array at midday looks like it had less sun. The HA statistics builder
  writes `pv_pot_main_w = pv_main_w`; a plant model (`backtest/pvmodel.py`,
  `frames.potential`) is what fills the gap. The model reads
  `pv.strings` in `plant.json`: tilt, azimuth and kWp per string, and
  `scale`, a fitted correction on the pvlib output (1,0 = nameplate). The
  reference roof is in `plant.example.json`: two 9,8 kWp strings, ESE at
  34° tilt and 109° azimuth, WNW at 39° and 288°, both at scale 1,0. A new
  plant starts at 1,0 per string and fits its own on clean hours.
- **Open loop.** The ladder solves once a day at midnight with static knobs
  and settles the whole plan. The live controller re-solves every 30 minutes
  with the dynamic rebalance fade. The ladder measures knowledge, not the
  live controller. The closed-loop replay in `backtest/replay.py` walks the
  frame tick by tick at the live cadence (local :13 and :43 every hour plus
  the 13:00 price publish), chains a virtual pack from one settled day to the
  next, and is the honest twin of the live loop; it is about 2.150 solves a
  month and is not what `quickcheck` runs.
- **The seven-day load profile leaks a little.** On the second horizon day
  the profile is the median over D-6 to D and so includes the scored day's
  own load. A mild leak into the F lanes' second day.
- **The no-battery baseline carries no bridge loss** on PV-only export while
  the lanes do: a few EUR a season against the battery in the gain.
- **Filled recorder gaps** are not marked in the ladder CSV. The reference
  frame marks the quarters in its `src_*` columns; a frame built from
  statistics has none.
- **Hourly statistics held flat** (the HA builder's fallback for anything
  older than the recorder's short-term window) net the flow inside the hour.
  On the reference, moving the battery and meter from hourly means to
  counter-scaled quarters raised the pack's throughput by 6 % and the lanes
  by 36 to 41 EUR over 146 days. The settlement prices real flows through
  the bridge each way and the meter through the buy/sell spread, so
  throughput an hourly mean netted away costs money once a witness sees it.
  Five-minute statistics are the better source where the recorder still has
  them.
