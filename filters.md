# Lasair (LSST) filters — CVs and pulsating variables for the 1 m CDK

> **Schema note (important):** these are written for the **LSST/Rubin** Lasair
> at **lasair-lsst.lsst.ac.uk**, whose `objects` table differs from the older
> ZTF schema. Key differences baked in below:
> - object id is **`diaObjectId`** (not `objectId`)
> - position columns are **`ra`** and **`decl`** (not `ramean`/`decmean`)
> - brightness is stored as **flux in nJy** (`g_psfFlux`, `r_psfFlux`); convert
>   to magnitudes with the **`flux2mag()`** function
> - outbursts are caught via **positive difference flux** counts
>   (`nPosDiaSources`, `nPosDiaSourcesNights`), not a `dmdt` sign
> - the latest-detection time column is **`lastDiaSourceMjdTai`** (note the
>   casing: `MjdTai`). It is selectable and sortable. There are also per-band
>   latest times: `g_latestMJD`, `r_latestMJD`, etc.
> - a strong outburst signal is **`jump1`** ("largest sigma jump of recent flux
>   from the previous −70 to −10 days") — better than counting positive
>   detections. `objects_ext.g_psfFluxMaxSlope` gives a rise rate in nJy/day.
>
> Always confirm a column exists in the schema browser (right-hand panel of the
> filter builder) before relying on it — the schema is still evolving.

Reachability assumptions for the cuts (calibrate to your measured performance):
- useful magnitude band ~ **12.5 to 18.5** (bright end = saturation guard for
  the 1 m; faint end = few-mmag precision floor). Push faint end to ~21 for
  detection-only work.
- declination floor **decl > -20** for sensible elevation from ~ +42° latitude;
  tighten to `decl > 0` if you only want comfortable altitudes.

Because magnitude lives in flux, the band is applied with `flux2mag()` in the
WHERE clause, or equivalently on the `mag` alias.

--------------------------------------------------------------------------

## Filter 1 — CVs in outburst (your "explosive + variable" sweet spot)

Sherlock tags the sky-context class; `nPosDiaSources >= 1` keeps real
brightenings (positive difference flux) rather than objects that merely went
*negative* against a bright template.

```sql
SELECT
  objects.diaObjectId,
  objects.ra, objects.decl,
  flux2mag(objects.g_psfFlux) AS gmag,
  flux2mag(objects.r_psfFlux) AS rmag,
  objects.nPosDiaSources, objects.nPosDiaSourcesNights,
  objects.jump1, objects.lastDiaSourceMjdTai,
  objects.tns_name,
  objects.g_psfFlux, objects_ext.g_psfFluxSigma,
  sherlock_classifications.classification
FROM
  objects, objects_ext, sherlock_classifications
WHERE
  sherlock_classifications.classification = 'CV'
  AND flux2mag(objects.g_psfFlux) BETWEEN 12.5 AND 18.5
  AND objects.decl > -20
  AND objects.nPosDiaSources >= 1
ORDER BY lastDiaSourceMjdTai DESC
```

Tighten the outburst cut by requiring the brightening persists across nights,
e.g. add `AND objects.nPosDiaSourcesNights >= 2`.

Two added columns and what they're for:
- `g_psfFlux` with `objects_ext.g_psfFluxSigma` lets you compute a
  signal-to-noise ratio (flux ÷ sigma). High SNR = a real detection well above
  its error bar; SNR below ~5 is likely noise. (`g_psfFluxSigma` lives in the
  `objects_ext` table, so the query joins it on `diaObjectId`.) You can even cut
  on it directly, e.g. `AND objects.g_psfFlux > 5 * objects_ext.g_psfFluxSigma`.
- `tns_name` is non-NULL when the object is already on the IAU Transient Name
  Server — i.e. someone has reported it and it's likely being followed. Use it
  to deprioritize already-claimed targets, or add
  `AND objects.tns_name IS NULL` to see only un-reported ones.

## Filter 2 — Pulsating-variable candidates in band

Periodic, not explosive — so no flux-change cut, just class + reach. Build a
standing list to phase-fold over many nights.

```sql
SELECT
  objects.diaObjectId,
  objects.ra, objects.decl,
  flux2mag(objects.g_psfFlux) AS gmag,
  flux2mag(objects.r_psfFlux) AS rmag,
  objects.lastDiaSourceMjdTai,
  objects.tns_name,
  objects.g_psfFlux, objects_ext.g_psfFluxSigma,
  sherlock_classifications.classification,objects.jump1,
objects.nPosDiaSources
FROM
  objects, objects_ext, sherlock_classifications
WHERE
  sherlock_classifications.classification = 'VS'
  AND flux2mag(objects.g_psfFlux) BETWEEN 12.5 AND 18.5
  AND objects.decl > -20
ORDER BY lastDiaSourceMjdTai DESC
```

`'VS'` is Sherlock's broad variable-star bucket; it does not separate pulsators
from eclipsers. For pulsators specifically, use the watchlist route (Filter 3).

## Filter 3 — Cross-match against your own pulsator/CV watchlist

Build a watchlist from a catalogue (e.g. RR Lyrae from Gaia DR3, or CVs from
Ritter–Kolb), note its numeric ID from the URL, and join on `watchlist_hits`.
The LSST join key is `diaObjectId`. Replace `wl_id=1` with your watchlist's ID:

```sql
SELECT
  objects.diaObjectId,
  objects.ra, objects.decl,
  flux2mag(objects.g_psfFlux) AS gmag,
  watchlist_hits.name, watchlist_hits.arcsec
FROM
  objects, watchlist_hits
WHERE
  objects.diaObjectId = watchlist_hits.diaObjectId
  AND watchlist_hits.wl_id = 1
  AND flux2mag(objects.g_psfFlux) BETWEEN 12.5 AND 18.5
  AND objects.decl > -20
```

--------------------------------------------------------------------------

## Using these
1. Sign in at https://lasair-lsst.lsst.ac.uk, open the filter builder.
2. Paste the SELECT lines and the WHERE lines into their boxes (the builder has
   autocomplete — let it confirm each column name against the live schema).
3. Run, then Save. **Heed the save warning:** saving clears the Kafka queue and
   reseeds 10 examples. To experiment without losing the queue, use **Duplicate
   Filter**.
4. To stream to your tool, set the filter's notification to a **kafka stream**
   option (lite lightcurve is the practical choice) and note the topic name.

## Calibrate the cuts to your rig
The 12.5–18.5 band and decl > -20 are starting guesses from your aperture and
latitude, not measurements. Shoot a standard field, measure your real limiting
magnitude and the magnitude where scatter reaches a few mmag, and edit the
numbers so every surfaced target is one you can actually image well.
