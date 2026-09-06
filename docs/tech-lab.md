# Tech Lab

Tech Lab is the main product offering. It helps a player choose deck changes to
test. The League remains the competitive context. This pass reuses the existing
pages and calculation engines.

- `/ev-lab`: analyze a submitted list with the experimental model.
- `/tech`: scout observed card inclusion and matchup results for an archetype.

The two pages share local navigation. The main navigation and a League invitation
lead to Tech Lab. Do not assign competitive ranks to cards or promise win-rate
improvement from an observational comparison.

## Analytical presentation

Keep the source, baseline, units, sample counts, and uncertainty near the result.
Keep these columns available on mobile through local table scrolling. A manual
archetype selection is not 100% detection confidence. A matchup display filter
does not change the field weights used by the estimate.

Scouting reports observed win-rate differences. It does not report Bayesian EV.
For its `both` option, inclusion combines sources but matchup evidence is online.
The list model uses different aggregation rules: inclusion rates are averaged
between sources, while matchup counts are pooled.

## Model validation still required

Track the data and model work in [issue #2](https://github.com/haidmoham/indigo-circuit/issues/2).

The source review on 2026-09-06 does not certify production data or model accuracy.
The following issues prevent presenting the estimate as calibrated win odds:

1. The card and copy split marts do not require a known player decklist before
   classifying a missing card row as card absence. Missing lists may enter the
   without-card denominator. Check all four online and major split marts.
2. Online split marts accept any non-null winner and count a different winner as
   a loss. Check ties and sentinel values against the source result convention.
3. `dashboard/ev.py` adds card variances without baseline variance or cross-card
   covariance. Its interval is a conditional approximation. The additive estimate
   is not constrained to the probability range. No card evidence can produce a
   zero-width interval. The interface must not present that as certainty.
4. Model coverage is the maximum field weight covered by a single non-core card.
   It is not coverage of the whole list. Some cards with limited coverage still
   contribute to the headline.
5. Online matchup history can include older events. Field weights and major
   marts use the application's April cutoff. The sources are not identical
   populations.

Resolve denominator and result classification rules with focused fixtures. Then
validate a rebuilt shadow database through the existing production gate before
promotion. Measure production impact and model calibration separately. Until
then, label the model experimental and use its output to plan playtesting.
