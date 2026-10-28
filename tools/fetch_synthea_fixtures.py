"""Fetch Synthea-generated patients from the public SMART sandbox with hl7bridge's own FHIR client
and save them as offline fixtures (samples/synthea/*.json.gz).

Usage: python tools/fetch_synthea_fixtures.py [--base https://r4.smarthealthit.org] [--count 4]
Picks the patients with the longest records (most observations) that also have medications,
plus one patient whose record includes a clinical note. All data is synthetic.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hl7bridge.fhir_client import FhirClient  # noqa: E402
from hl7bridge.fhir_import import save_fixture  # noqa: E402


def count(client, rtype, pid):
    return client.get(rtype, {"patient": pid, "_summary": "count"}).get("total", 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://r4.smarthealthit.org")
    ap.add_argument("--count", type=int, default=4)
    ap.add_argument("--scan", type=int, default=60, help="how many patients to consider")
    ap.add_argument("--max-observations", type=int, default=2500, help="keep fixtures a reasonable size")
    args = ap.parse_args()
    client = FhirClient(args.base, page_size=250)
    ids = [p["id"] for p in client.get("Patient", {"_count": args.scan, "_elements": "id"}).get("entry", [])
           for p in [p["resource"]]]
    sized = []
    for pid in ids:
        obs, meds = count(client, "Observation", pid), count(client, "MedicationRequest", pid)
        if meds and obs <= args.max_observations:
            sized.append((obs, meds, pid))
    sized.sort(reverse=True)
    chosen = [pid for _, _, pid in sized[: args.count - 1]]
    noted = {d["resource"]["subject"]["reference"].split("/")[-1]
             for d in client.get("DocumentReference", {"_count": 20}).get("entry", [])}
    chosen += [pid for pid in sorted(noted) if pid not in chosen][:1]
    out = ROOT / "samples" / "synthea"
    out.mkdir(parents=True, exist_ok=True)
    for pid in chosen:
        warnings = []
        resources = client.patient_record(pid, on_warning=warnings.append)
        path = out / f"{pid}.json.gz"
        save_fixture(path, args.base, resources)
        kinds = {}
        for r in resources:
            kinds[r["resourceType"]] = kinds.get(r["resourceType"], 0) + 1
        print(f"{path.name}: {len(resources)} resources {kinds} {path.stat().st_size // 1024} KB"
              + (f" warnings={warnings}" if warnings else ""))
    print(f"{client.requests} requests")


if __name__ == "__main__":
    main()
