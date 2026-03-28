# Drawdown Monitor

## Projektbeschreibung

Ein GitHub-Actions-basierter Monitor, der täglich den Drawdown eines ETFs vom All-Time High (ATH) berechnet und bei konfigurierbaren Schwellenwerten Benachrichtigungen versendet. Ziel: Antizyklisches Investieren nach dem GPO-Prinzip (Global Portfolio One) automatisiert unterstützen.

## Architektur

```
GitHub Actions (Cron, Mo-Fr nach Xetra-Schluss)
  → Kurs abrufen (yfinance, Ticker aus config.json, lookback_days Zeitfenster)
  → ATH aus state.json laden, ggf. aktualisieren
  → Drawdown berechnen: (kurs / ath - 1) × 100
  → Bei Schwellenwert-Durchbruch (noch nicht triggered) → Telegram-Notification senden
  → state.json updaten und zurück ins Repo committen
```

## Repo-Struktur

```
drawdown-monitor/
├── .github/
│   └── workflows/
│       └── monitor.yml          # GitHub Actions Workflow
├── monitor.py                   # Hauptlogik
├── state.json                   # Persistenter State (wird automatisch aktualisiert)
├── config.json                  # Konfiguration (Ticker, Schwellenwerte)
├── pyproject.toml               # Abhängigkeiten (uv)
├── uv.lock                      # Lockfile (auto-generiert)
└── CLAUDE.md                    # Diese Datei
```

## Komponenten im Detail

### 1. `config.json`

```json
{
  "ticker": "IMIE.DE",
  "thresholds": [-20, -30, -40],
  "lookback_days": 365
}
```

- `thresholds` ist eine Liste von Integer-Werten (Drawdown-Prozentwerte, negativ)
- `lookback_days` bestimmt das Zeitfenster für das ATH-Lookup via yfinance
- Kein `ticker_name` oder `notification`-Block — alles im Code verdrahtet

### 2. `state.json`

```json
{
  "ath": null,
  "ath_date": null,
  "last_price": null,
  "last_check": null,
  "triggered_thresholds": []
}
```

- Wird bei jedem Run aktualisiert und zurück ins Repo committed
- `triggered_thresholds` ist eine Liste bereits ausgelöster Schwellenwerte (z.B. `[-20, -30]`)
- Bei neuem ATH wird `triggered_thresholds` zurückgesetzt (`[]`), damit Schwellenwerte erneut feuern können

### 3. `monitor.py` — Hauptlogik

Schritte:

1. **Config laden** aus `config.json`
2. **State laden** aus `state.json`
3. **Kurs abrufen** via `yfinance.download()` — `lookback_days`-Zeitfenster, letzter Schlusskurs + Periodenhoch
4. **ATH aktualisieren**: Wenn `period_high > stored_ath` → neues ATH setzen, `triggered_thresholds` zurücksetzen
5. **Drawdown berechnen**: `(current_price / ath - 1) * 100`
6. **Alert-Logik**: Für jeden Threshold (von -20 → -30 → -40): wenn `drawdown_pct <= threshold` und Threshold noch nicht in `triggered_thresholds` → Alert senden + in Liste eintragen
7. **Notification senden** via Telegram
8. **State aktualisieren** und `state.json` schreiben

Kein Recovery-Alert (kein Zurücksetzen wenn Markt sich erholt — nur ATH-Reset setzt Thresholds zurück).

#### Error Handling

- Wenn `yfinance` keine Daten liefert: `ValueError` → wird in `main()` gefangen, Exit-Code 1, kein State-Update
- Telegram-Fehler: `raise_for_status()` → Exception propagiert

#### Telegram-Notification

- Bot Token: aus Environment Variable `TELEGRAM_TOKEN`
- Chat ID: aus Environment Variable `TELEGRAM_CHAT_ID`
- HTTP POST an `https://api.telegram.org/bot{token}/sendMessage` mit `parse_mode: Markdown`
- Nachrichtenformat:

```
⚠️ *Drawdown Alert: IMIE.DE*
Drawdown: *-30.72%* (threshold: -30%)
Current price: 148.5000
ATH: 214.3500 (2025-02-19)
Checked: 2025-03-28
```

### 4. GitHub Actions Workflow `.github/workflows/monitor.yml`

```yaml
name: Drawdown Monitor

on:
  schedule:
    - cron: "0 18 * * 1-5"  # Mo-Fr 18:00 UTC (nach Xetra-Schluss)
  workflow_dispatch:

permissions:
  contents: write

jobs:
  monitor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: uv sync
      - name: Run drawdown monitor
        env:
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: uv run python monitor.py
      - name: Commit updated state
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add state.json
          git diff --staged --quiet || git commit -m "chore: update drawdown state [skip ci]"
          git push
```

### 5. `pyproject.toml`

```toml
[project]
name = "drawdown-monitor"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "yfinance>=0.2.40",
    "requests>=2.31.0",
]
```

Abhängigkeiten verwalten mit `uv`. Lockfile `uv.lock` wird von uv auto-generiert.

## GitHub Secrets (manuell konfigurieren)

| Secret | Beschreibung |
|---|---|
| `TELEGRAM_TOKEN` | Token vom Telegram BotFather |
| `TELEGRAM_CHAT_ID` | Chat-ID für die Benachrichtigungen |

## Designentscheidungen

- **yfinance** statt kostenpflichtiger API: Kein API-Key nötig, reicht für einen täglichen Abruf
- **state.json im Repo**: Einfachste Persistenz ohne externe Datenbank. Git-History liefert zusätzlich ein Audit-Log aller Drawdown-Veränderungen
- **triggered_thresholds-Liste statt Regime-Strings**: Jeder Schwellenwert wird einzeln getrackt. Einfacher als Regime-Strings, kein Recovery-Alert
- **ATH-Reset setzt Thresholds zurück**: Wenn ein neues ATH erreicht wird, können alle Schwellenwerte erneut feuern
- **`lookback_days` statt "echtem" historischen ATH**: Praktischer Kompromiss — yfinance liefert zuverlässig Daten für ein rollierendes Fenster
- **Public Repo empfohlen**: GitHub Actions ist bei öffentlichen Repos kostenlos und unlimitiert. Keine sensitiven Daten im Repo (Secrets sind verschlüsselt)

## Erweiterungsideen (nicht Teil des MVP)

- Recovery-Alert: Wenn Markt sich von Krisenregime erholt, separate Benachrichtigung
- Weekly Summary: Jeden Sonntag eine Status-Nachricht mit aktuellem Drawdown
- Multi-Ticker: Mehrere ETFs parallel überwachen
- ntfy.sh als Notification-Alternative (ein curl-Aufruf, kein Bot nötig)
- Historischer Drawdown-Chart als SVG generieren
- Tests: `tests/test_monitor.py` mit pytest für Drawdown-Berechnung, ATH-Update, Alert-Logik
