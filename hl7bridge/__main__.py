"""hl7bridge command line.

    python -m hl7bridge serve [--seed]        HTTP :8080 + MLLP :2575 (+ load sample feeds)
    python -m hl7bridge convert FILE.hl7      print the FHIR transaction Bundle (dry run)
    python -m hl7bridge send FILE.hl7 ...     send over MLLP, print ACKs
    python -m hl7bridge ask "question"        ask the assistant about the sample Veteran
    python -m hl7bridge eval                  run the evaluation harness
    python -m hl7bridge mcp [--seed]          MCP server on stdio for AI agents
    python -m hl7bridge import-fhir --base URL --patient ID [--save FILE.json.gz]
                                              pull one patient's record from a FHIR R4 server
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from .ai.access import AccessDenied
from .ai.evaluate import run_eval, to_markdown
from .ai.providers import make_provider
from .ccda import ingest as ingest_ccda
from .fhir_client import FhirClient, FhirError
from .fhir_import import import_resources, load_fixture, save_fixture
from .config import BridgeConfig
from .mcp import McpServer
from .mllp import MLLPServer, send
from .pipeline import Pipeline
from .server import SAMPLES, App, serve_http
from .store import FhirStore

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_ICN = "urn:oid:2.16.840.1.113883.4.349|1013579246V135792"


def _seed(app: App, quiet: bool = False, synthea: bool = True) -> None:
    for f in sorted(SAMPLES.glob("*.hl7")):
        r = app.pipeline.process(f.read_text())
        if not quiet:
            print(f"  {f.name:40} {r.status:9} {r.ack.split(chr(13))[1]}")
    for f in sorted((SAMPLES / "hie").glob("*.xml")):
        r = ingest_ccda(app.store, f.read_text(), app.config, on_commit=app.assistant.reindex)
        if not quiet:
            print(f"  hie/{f.name:36} {r['status']:9} C-CDA {r.get('control_id')}")
    for f in sorted((SAMPLES / "synthea").glob("*.json.gz")) if synthea else []:
        base, resources = load_fixture(f)
        r = import_resources(app.store, resources, base, config=app.config, on_commit=app.assistant.reindex)
        if not quiet:
            print(f"  synthea/{f.name[:8]}…{'':25} {r['status']:9} FHIR R4 {sum(r['counts'].values())} resources")


def _provider(args):
    return make_provider(args.provider, args.model)


def _add_provider_args(p) -> None:
    p.add_argument("--provider", default="auto", choices=["auto", "extractive", "azure-openai", "ollama"],
                   help="auto: Azure if configured, else Ollama if OLLAMA_MODEL is set, else extractive")
    p.add_argument("--model", help="Ollama model (e.g. llama3.1:8b) or Azure deployment name")


def _loaded_app(args) -> App:
    app = App(FhirStore(), BridgeConfig.from_env(), _provider(args))
    _seed(app, quiet=True)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hl7bridge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--http-port", type=int, default=8080)
    p.add_argument("--mllp-port", type=int, default=2575)
    p.add_argument("--db", default=":memory:")
    p.add_argument("--seed", action="store_true", help="load the synthetic sample feeds on start")
    _add_provider_args(p)
    p = sub.add_parser("convert")
    p.add_argument("file")
    p = sub.add_parser("send")
    p.add_argument("files", nargs="+")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=2575)
    p = sub.add_parser("ask")
    _add_provider_args(p)
    p.add_argument("--user", default="dr-primary", help="pilot clinician: dr-primary, dr-sud-clinic, dr-other-site")
    p.add_argument("question", nargs="?", default="")
    p = sub.add_parser("mcp", help="MCP server on stdio (read-only unless --allow-write)")
    p.add_argument("--db", default=":memory:")
    p.add_argument("--seed", action="store_true", help="load the synthetic sample feeds on start")
    p.add_argument("--allow-write", action="store_true", help="expose ingest_hl7v2 / ingest_ccda tools")
    p.add_argument("--launch-icn", help="bind the session to one patient, as an EHR launch would")
    p.add_argument("--user", help="clinician the agent acts for (default: BRIDGE_MCP_USER or dr-primary)")
    _add_provider_args(p)
    p = sub.add_parser("import-fhir", help="pull one patient's record from a FHIR R4 server")
    p.add_argument("--base", required=True, help="FHIR base URL, e.g. https://r4.smarthealthit.org")
    p.add_argument("--patient", required=True, help="Patient id on that server")
    p.add_argument("--token", help="bearer token (or set FHIR_BEARER_TOKEN)")
    p.add_argument("--save", help="also save the fetched record as a fixture (.json.gz)")
    p.add_argument("--db", default=":memory:")
    p = sub.add_parser("eval")
    p.add_argument("--gold", default=str(ROOT / "eval" / "gold.json"))
    p.add_argument("--out", help="default: eval/report.md, or eval/report-<provider>.md for hosted/local models")
    _add_provider_args(p)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.cmd == "serve" else logging.WARNING,
                        format="%(asctime)s %(name)s %(message)s")

    if args.cmd == "serve":
        app = App(FhirStore(args.db), BridgeConfig.from_env(), _provider(args))
        mllp = MLLPServer(lambda text: app.pipeline.process(text).ack, args.host, args.mllp_port).start_in_thread()
        httpd = serve_http(app, args.host, args.http_port)
        if args.seed:
            print("Loading synthetic VistA feeds:")
            _seed(app)
        print(f"\n  Dashboard   http://{args.host}:{args.http_port}/\n"
              f"  FHIR R4     http://{args.host}:{args.http_port}/fhir/metadata\n"
              f"  MLLP        {args.host}:{mllp.port}\n"
              f"  Model       {app.assistant.provider.name}\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            mllp.stop()
        return 0

    if args.cmd == "mcp":
        # stdout is the protocol channel: nothing else may print there.
        app = App(FhirStore(args.db), BridgeConfig.from_env(), _provider(args))
        if args.seed:
            _seed(app, quiet=True)
        McpServer(app, allow_write=args.allow_write, launch_icn=args.launch_icn, user=args.user).serve()
        return 0

    if args.cmd == "convert":
        result = Pipeline(FhirStore(), BridgeConfig.from_env()).convert(Path(args.file).read_text())
        print(json.dumps(result.bundle or result.outcome, indent=2))
        print(f"\n{result.status}: {result.ack.split(chr(13))[1]}", file=sys.stderr)
        for w in result.warnings:
            print(f"warning: {w}", file=sys.stderr)
        return 0 if result.status == "accepted" else 1

    if args.cmd == "send":
        for f in args.files:
            ack = send(Path(f).read_text(), args.host, args.port)
            print(f"{Path(f).name}: {ack.split(chr(13))[1]}")
        return 0

    if args.cmd == "ask":
        app = _loaded_app(args)
        pid = app.store.search("Patient", {"identifier": SAMPLE_ICN})[0]["id"]
        try:
            r = app.assistant.ask(pid, args.question, user=args.user, agent="cli")
        except AccessDenied as exc:
            print(f"refused: {exc} (audited)", file=sys.stderr)
            return 3
        print(f"\n{r['answer']}\n\n{r['disclaimer']}\n")
        for c in r["citations"]:
            print(f"  [{c['n']}] {c['label']}  ({c['resource']})")
        g = r["guardrails"]
        print(f"\n  groundedness {g['groundedness']} · quarantined {len(g['quarantined'])} · "
              f"provider {r['provider']} · {r['timing_ms']['total']} ms")
        return 0

    if args.cmd == "import-fhir":
        import os
        warnings: list[str] = []
        try:
            client = FhirClient(args.base, token=args.token or os.environ.get("FHIR_BEARER_TOKEN"))
            resources = client.patient_record(args.patient, on_warning=warnings.append)
        except FhirError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.save:
            save_fixture(args.save, args.base, resources)
        store = FhirStore(args.db)
        result = import_resources(store, resources, args.base)
        print(json.dumps({**result, "requests": client.requests, "fetch_warnings": warnings}, indent=2))
        return 0 if result["status"] == "accepted" else 1

    if args.cmd == "eval":
        app = _loaded_app(args)
        report = run_eval(app.assistant, args.gold)
        name = app.assistant.provider.name
        if not args.out:
            slug = re.sub(r"[^a-z0-9.]+", "-", name.lower()).strip("-")
            args.out = str(ROOT / "eval" / ("report.md" if name == "extractive" else f"report-{slug}.md"))
        md = to_markdown(report)
        Path(args.out).write_text(md)
        print(md)
        return 0 if report["summary"]["passed"] == report["summary"]["cases"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
