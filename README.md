# Deux salles, de cinéma

One page with the coming showings at two Toulouse cinemas:

- American Cosmograph: https://www.american-cosmograph.fr/les-horaires.html
- ABC: https://abc-toulouse.fr/horaires.html

Each showing has a "+ Agenda" link. It opens Google Calendar with the event
prefilled (title, start, end, address, room, version, film link, booking link
for ABC). You then press Save.

Each showing also has a "Vu" button. It marks the film as seen and hides all
its showings, at both cinemas. The "Films vus" button in the filter bar opens
a side panel that lists the seen films, with a "Retirer" button to unmark
one. The list lives in the browser's local storage, so it is per browser and
survives a rebuild of the page.

## Run

    python3 cine.py

Writes `seances.html` and `cine-state.json` in the current directory.
Python 3.9 or later. Pillow is optional: with it, each film poster is
downloaded once and embedded as a small thumbnail; without it, the
thumbnails point to the posters on the cinema sites.

Exit code: 0 when both sites answered, 2 when one site failed and the page
was built from the stored data, 1 when the page has no showing at all.

Options:

    python3 cine.py -o site/index.html      # write elsewhere; the state file follows
    python3 cine.py --state /path/to/state.json
    python3 cine.py --no-state              # ignore and do not write the state file
    python3 cine.py --no-thumbs             # skip the poster downloads
    python3 cine.py -q                      # no progress on stderr
    python3 cine.py --from-files cosmo.html abc.html   # parse saved pages, no network

## Safe to run often

The script is meant to run on a schedule, twice a day or more. Each run
merges what the sites publish into `cine-state.json`:

- A day a site returned replaces the stored showings of that site for that
  day. A showing the site removed or moved disappears from the page.
- A day a site did not return keeps its stored showings. ABC hides past
  showings of the current day; those are already gone from the page anyway.
- A site that fails, or answers with no showing at all, changes nothing of
  its own. The page keeps the last good data and the footer says when each
  site was last read.
- Showings that already started are dropped.
- Poster thumbnails are cached in the state file and downloaded once per
  poster.

So two runs in a row give the same page and never a double.

## What it fetches

- Cosmo: the showtimes page, which carries 7 days, plus the "semaine
  suivante" page. Cosmo usually publishes the next week a week ahead. One or
  two requests.
- ABC: the showtimes page for today, one page per other day of the week,
  then the days after the week until two days in a row come back empty. ABC
  usually publishes the next week on Monday; the probe picks it up as soon
  as the pages exist. About 8 to 15 requests.
- One request per new poster.

## GitHub Pages

The workflow in `.github/workflows/pages.yml` builds the page on GitHub's
runners and deploys it to GitHub Pages. It runs twice a day, on each push
that changes the script, and on demand from the Actions tab.

The state file is deployed next to the page. Each run first downloads the
previous `cine-state.json` from the live site, so the merge rules above
hold across runs without a commit and without a cache.

One-time setup:

1. Create a public repository on GitHub. Pages is free on public
   repositories only.
2. Push this directory to its `main` branch.
3. The first push runs the workflow, which enables Pages by itself. If the
   workflow fails on the "configure-pages" step, open Settings, Pages, and
   set the source to "GitHub Actions", then rerun the workflow.
4. The page is then at `https://<user>.github.io/<repo>/`.

Things to know:

- The schedule is in UTC and GitHub may delay a scheduled run by some
  minutes, or more at busy hours.
- GitHub disables the schedule on a public repository after 60 days without
  a commit. It sends an email before. A push, or a manual run from the
  Actions tab, turns it back on.
- The deployed state file is public. It holds only showtimes and poster
  thumbnails.

## Known limits

- The page is static. Filters and the seen list run in the browser; the
  showings are baked in at build time.
- Google offers no link that saves an event without confirmation. One tap
  on Save per showing is the floor.
- The seen list is per browser. Another phone or browser starts empty.
