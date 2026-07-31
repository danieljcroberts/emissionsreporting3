#!/usr/bin/env python3
"""Draft emissions CSV checker.

Compares a draft quarter against all previously finalised quarters and emits
data-quality flags. Standard library only -- no pandas, no pip install.

Usage:
    python3 emissions_check.py "2026-Q1 DRAFT.csv"          # human summary + JSON
    python3 emissions_check.py "2026-Q1 DRAFT.csv" --json   # JSON only

Finalised quarters are discovered automatically: every "*FINAL.csv" in the same
directory as the draft. The most recent one (by quarter label) is "last
quarter"; all of them together form "history".

Checks performed
----------------
1. INVOICED_JUMP / INVOICED_COLLAPSE
   Total Invoiced Amount MNOK vs the same plant last finalised quarter.
   warn outside x0.5-x2.0, high outside x0.25-x4.0.
2. INTENSITY_OUTLIER
   Emissions per GWh vs that plant's own mean across history.
   warn outside x0.6-x1.67, high outside x0.4-x2.5.
   Plants with no history are compared to the draft's cross-plant median.
3. NEW_PLANT / MISSING_PLANT
   Plant names not seen in any finalised quarter, and plants present last
   quarter but absent from the draft (these usually pair up as a rename/typo).
4. PLACEHOLDER_SUSPECT
   Values that look hand-entered rather than measured: exact whole numbers in
   a column that is otherwise high-precision floats (e.g. 99), or zero.

Thresholds live in THRESHOLDS below; change them there, not inline.
"""

import csv
import json
import os
import statistics
import sys
from collections import defaultdict

THRESHOLDS = {
    "invoiced_warn_low": 0.5,
    "invoiced_warn_high": 2.0,
    "invoiced_high_low": 0.25,
    "invoiced_high_high": 4.0,
    "intensity_warn_low": 0.6,
    "intensity_warn_high": 1.67,
    "intensity_high_low": 0.4,
    "intensity_high_high": 2.5,
}

PLANT_COL = "Plant"
DATE_COL = "Date"
INVOICED_COL = "Total Invoiced Amount MNOK"
ENERGY_COL = "Energy Generated in Gigawatts"
EMISSIONS_COL = "Draft Reports Emissions"


def read_csv(path):
    """Return a list of row dicts with whitespace-stripped keys and a BOM-safe read."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = []
        for raw in reader:
            row = {}
            for key, value in raw.items():
                if key is None:
                    continue
                row[key.strip()] = (value or "").strip()
            if row.get(PLANT_COL):
                rows.append(row)
    return rows


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def quarter_of(rows, fallback):
    for row in rows:
        if row.get(DATE_COL):
            return row[DATE_COL]
    return fallback


def find_final_files(directory):
    """All finalised quarter CSVs, sorted oldest -> newest by quarter label."""
    finals = []
    for name in os.listdir(directory):
        upper = name.upper()
        if upper.endswith(".CSV") and "FINAL" in upper:
            finals.append(os.path.join(directory, name))
    return sorted(finals, key=lambda p: os.path.basename(p))


def intensity(row):
    energy = to_float(row.get(ENERGY_COL))
    emissions = to_float(row.get(EMISSIONS_COL))
    if energy is None or emissions is None or energy == 0:
        return None
    return emissions / energy


def ratio_text(value):
    if value is None:
        return "n/a"
    if value >= 1:
        return "x%.2f" % value
    return "x%.2f (-%.0f%%)" % (value, (1 - value) * 100)


def looks_like_placeholder(value_str, value, column_values):
    """True when a value is a suspiciously round entry in a high-precision column."""
    if value is None:
        return False
    if value == 0:
        return True
    if "." in value_str:
        return False
    # Whole number in a column where most values carry >=3 decimal places.
    precise = sum(1 for v in column_values if "." in v and len(v.split(".")[1]) >= 3)
    return precise >= max(3, len(column_values) // 2)


def analyse(draft_path):
    directory = os.path.dirname(os.path.abspath(draft_path)) or "."
    draft_rows = read_csv(draft_path)
    draft_quarter = quarter_of(draft_rows, os.path.basename(draft_path))

    final_paths = find_final_files(directory)
    history = []  # [(quarter, {plant: row})]
    for path in final_paths:
        rows = read_csv(path)
        history.append(
            (
                quarter_of(rows, os.path.basename(path)),
                {r[PLANT_COL]: r for r in rows},
                os.path.basename(path),
            )
        )

    last_quarter, last_rows, last_file = history[-1] if history else (None, {}, None)
    known_plants = set()
    for _, rows_by_plant, _ in history:
        known_plants.update(rows_by_plant)

    # Per-plant historical intensity across every finalised quarter.
    hist_intensity = defaultdict(list)
    for _, rows_by_plant, _ in history:
        for plant, row in rows_by_plant.items():
            value = intensity(row)
            if value is not None:
                hist_intensity[plant].append(value)

    draft_intensities = [i for i in (intensity(r) for r in draft_rows) if i is not None]
    draft_median_intensity = statistics.median(draft_intensities) if draft_intensities else None

    invoiced_strings = [r.get(INVOICED_COL, "") for r in draft_rows]
    emissions_strings = [r.get(EMISSIONS_COL, "") for r in draft_rows]

    flags = []

    def add(severity, kind, plant, message, **extra):
        flag = {"severity": severity, "type": kind, "plant": plant, "message": message}
        flag.update(extra)
        flags.append(flag)

    draft_plants = [r[PLANT_COL] for r in draft_rows]

    for row in draft_rows:
        plant = row[PLANT_COL]
        invoiced_str = row.get(INVOICED_COL, "")
        invoiced = to_float(invoiced_str)
        emissions_str = row.get(EMISSIONS_COL, "")
        emissions = to_float(emissions_str)
        energy = to_float(row.get(ENERGY_COL))
        this_intensity = intensity(row)

        # --- 3. unknown plant name -------------------------------------------------
        if plant not in known_plants:
            add(
                "high",
                "NEW_PLANT",
                plant,
                "Plant name never appears in any finalised quarter (%s). "
                "Likely a rename or typo -- confirm before finalising."
                % ", ".join(q for q, _, _ in history),
            )

        # --- 1. invoiced vs last finalised quarter --------------------------------
        prev = last_rows.get(plant)
        prev_invoiced = to_float(prev.get(INVOICED_COL)) if prev else None
        if invoiced is not None and prev_invoiced not in (None, 0):
            ratio = invoiced / prev_invoiced
            t = THRESHOLDS
            if ratio >= t["invoiced_high_high"] or ratio <= t["invoiced_high_low"]:
                severity = "high"
            elif ratio >= t["invoiced_warn_high"] or ratio <= t["invoiced_warn_low"]:
                severity = "warn"
            else:
                severity = None
            if severity:
                kind = "INVOICED_JUMP" if ratio > 1 else "INVOICED_COLLAPSE"
                add(
                    severity,
                    kind,
                    plant,
                    "Invoiced %.4f MNOK vs %.4f in %s (%s)."
                    % (invoiced, prev_invoiced, last_quarter, ratio_text(ratio)),
                    ratio=round(ratio, 4),
                    current=invoiced,
                    previous=prev_invoiced,
                )

        # --- 2. emissions intensity vs own history --------------------------------
        if this_intensity is not None:
            baseline = None
            baseline_label = None
            if hist_intensity.get(plant):
                baseline = statistics.fmean(hist_intensity[plant])
                baseline_label = "own history mean"
            elif draft_median_intensity:
                baseline = draft_median_intensity
                baseline_label = "draft cross-plant median (no history for this plant)"
            if baseline:
                ratio = this_intensity / baseline
                t = THRESHOLDS
                if ratio >= t["intensity_high_high"] or ratio <= t["intensity_high_low"]:
                    severity = "high"
                elif ratio >= t["intensity_warn_high"] or ratio <= t["intensity_warn_low"]:
                    severity = "warn"
                else:
                    severity = None
                if severity:
                    add(
                        severity,
                        "INTENSITY_OUTLIER",
                        plant,
                        "Intensity %.2f t/GWh vs %.2f %s (%s). Emissions %s over %s GWh."
                        % (
                            this_intensity,
                            baseline,
                            baseline_label,
                            ratio_text(ratio),
                            emissions_str,
                            energy,
                        ),
                        ratio=round(ratio, 4),
                        current=round(this_intensity, 4),
                        baseline=round(baseline, 4),
                    )

        # --- 4. placeholder-looking values ----------------------------------------
        if looks_like_placeholder(invoiced_str, invoiced, invoiced_strings):
            add(
                "high",
                "PLACEHOLDER_SUSPECT",
                plant,
                "Invoiced amount is the whole number %s in a column of high-precision "
                "figures -- looks like a placeholder, not a measurement." % invoiced_str,
                column=INVOICED_COL,
                value=invoiced_str,
            )
        if looks_like_placeholder(emissions_str, emissions, emissions_strings):
            add(
                "high",
                "PLACEHOLDER_SUSPECT",
                plant,
                "Emissions is the whole number %s in a column of high-precision figures "
                "-- confirm this is a real figure and not a placeholder." % emissions_str,
                column=EMISSIONS_COL,
                value=emissions_str,
            )

    # --- 3b. plants that vanished since last quarter ------------------------------
    for plant in sorted(set(last_rows) - set(draft_plants)):
        add(
            "high",
            "MISSING_PLANT",
            plant,
            "Present in %s but absent from the draft. Check whether it was renamed."
            % last_quarter,
        )

    order = {"high": 0, "warn": 1}
    flags.sort(key=lambda f: (order.get(f["severity"], 2), f["plant"]))

    return {
        "draft_file": os.path.basename(draft_path),
        "draft_quarter": draft_quarter,
        "plant_count": len(draft_rows),
        "compared_against": [q for q, _, _ in history],
        "last_finalised_quarter": last_quarter,
        "last_finalised_file": last_file,
        "flag_count": len(flags),
        "high_count": sum(1 for f in flags if f["severity"] == "high"),
        "flags": flags,
    }


def render(result):
    lines = []
    lines.append(
        "%s draft (%s) -- %d plants, compared against %s."
        % (
            result["draft_quarter"],
            result["draft_file"],
            result["plant_count"],
            ", ".join(result["compared_against"]) or "no finalised quarters",
        )
    )
    if not result["flags"]:
        lines.append("")
        lines.append("No data-quality flags raised.")
        return "\n".join(lines)
    lines.append("")
    lines.append(
        "%d flag(s), %d high severity:" % (result["flag_count"], result["high_count"])
    )
    for flag in result["flags"]:
        lines.append(
            "  [%s] %s -- %s: %s"
            % (flag["severity"].upper(), flag["plant"], flag["type"], flag["message"])
        )
    return "\n".join(lines)


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    result = analyse(args[0])
    if "--json" in argv:
        print(json.dumps(result, indent=2))
    else:
        print(render(result))
        print()
        print("--- JSON ---")
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
