# Lasair TVS target tool — CVs & pulsating variables for the 1 m CDK

A small local web app that queries the Lasair broker for cataclysmic variables
in outburst and pulsating-variable candidates within your instrument's reach,
and lets you filter the results by date/time and magnitude in the browser.

## Why it runs locally
Your Lasair API token must stay private (Lasair explicitly asks you not to put
it in shared or browser-side code), and Lasair's API can't be called directly
from a browser. So the token lives in an environment variable, the Python
backend talks to Lasair, and the page only ever sees filtered results.

## Setup
```
pip install flask lasair
export LASAIR_TOKEN=your_token      # lasair.lsst.ac.uk -> sign in -> My Profile
python app.py
```
Open http://127.0.0.1:5000

Try the interface first without a token:
```
python app.py --demo
```

## Files
- `app.py` — backend + single-page UI. Filters (CVs, pulsators) are defined
  near the top as Lasair (selected, tables, conditions) triples.
- `filters.md` — the same filters as raw SQL to paste into the Lasair filter
  builder, plus watchlist instructions and the reachability assumptions.

## Calibrate to your rig
The magnitude band (12.5–18.5) and declination floor (-20) are starting
guesses from your aperture and latitude, not measurements. Edit `DEFAULTS` in
`app.py` (and the cuts in `filters.md`) once you've measured your real limiting
and few-mmag-precision magnitudes from standard-field frames.

## Two caveats worth checking before you rely on it
- Confirm Lasair's live Rubin-stream coverage — it currently serves ZTF and is
  bringing LSST online as the survey ramps, so object IDs will be ZTF-prefixed
  for now.
- Sherlock's `'VS'` tag is the broad variable-star bucket and doesn't separate
  pulsators from eclipsers. For pulsators specifically, build a watchlist from
  a pulsator catalogue (see filters.md, Filter 3).
