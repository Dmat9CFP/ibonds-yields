# ibonds-yields

Public BlackRock iShares iBonds yield data for the Retirement Income Ladder Builder (`ladder.html`). This repository holds that yield file and the script that updates it. It does not hold client data.

## How yield refresh works

`refresh.py` downloads the public BlackRock iBonds ladder feed (one GET, no login) and writes `data/yields.json`. It replaces that file only when the result has fund lists, known tickers, yields in a sane range, and an `asOf` date that parses. A bad fetch leaves the last good file in place.

A GitHub Actions workflow runs the script on weekdays after the US market close, and commits `data/yields.json` only when it changed.

`ladder.html` is the ladder tool. Open that file in a browser. It does not refresh by itself. **Refresh yields** downloads `data/yields.json` from this repository. If that fails, or you are offline, import a BlackRock file from the Yields & data panel. Nothing you type in the tool is uploaded.

## Run a refresh manually

On GitHub: **Actions** → **Refresh iBonds yields** → **Run workflow**.
