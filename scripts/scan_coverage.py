#!/usr/bin/env python3
"""Offline, evidence-gated job-list coverage ledger. Python 3.9+, stdlib only."""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DISPOSITIONS = {
    "known_existing_exact_id", "user_skip", "senior", "hard_qualification_gap",
    "official_closed", "review_ready", "qualification_hold",
    "needs_official_verification", "duplicate_source", "out_of_scope",
}
EXEMPT = {"known_existing_exact_id", "user_skip", "senior",
          "duplicate_source", "out_of_scope"}


def now():
    return datetime.now(timezone.utc).isoformat()


def text_evidence(value):
    """Booleans/empty objects are declarations, not evidence."""
    return isinstance(value, str) and bool(value.strip())


def end_evidence(value, read_count):
    if text_evidence(value):
        return True
    # EconLaunch UI captures carry structured list-exhaustion evidence. Merely
    # {complete:true} or a nonempty arbitrary object is not sufficient.
    if not isinstance(value, dict):
        return False
    sequence = value.get("sequence", [])
    return (value.get("actualDOMCount") == read_count
            and value.get("buttonHidden") is True
            and isinstance(sequence, list) and bool(sequence) and sequence[-1] == read_count
            and text_evidence(value.get("cuaResult")))


def canonical_url(value, scope=False):
    if not value:
        return ""
    parts = urlsplit(str(value).strip())
    ignored = {"page", "cursor", "job"} if scope else set()
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ignored]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path.rstrip("/"), urlencode(sorted(query)), ""))


def aliases(row):
    result = []
    source_id = row.get("sourceId", row.get("id"))
    if source_id is not None and str(source_id).strip():
        result.append("id:" + str(source_id).strip())
    url = canonical_url(row.get("url", row.get("href", "")))
    if url:
        result.append("url:" + url)
    # Fingerprints are only the fallback: identical titles with different IDs
    # must remain distinct jobs. Index/updatedAt are deliberately excluded.
    label = row.get("title") or row.get("label") or ""
    paragraphs = row.get("paragraphs", [])
    stable_paragraphs = [p for p in paragraphs if isinstance(p, str)
                         and not re.search(r"(?i)\b(updated|posted|ago)\b|[$€£]", p)] if isinstance(paragraphs, list) else []
    payload = {"company": row.get("company", ""), "title": label,
               "label": row.get("label", ""),
               "location": row.get("location", ""), "context": stable_paragraphs[:2]}
    if label or stable_paragraphs:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True,
                                          ensure_ascii=False).encode()).hexdigest()[:24]
        result.append("fp:" + digest)
    if not result:
        raise ValueError("Every row needs sourceId/id, url, or nonempty content")
    return result


def load_ledger(path):
    if not Path(path).exists():
        return {"schemaVersion": 1, "sources": {}}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value.get("schemaVersion") != 1 or not isinstance(value.get("sources"), dict):
        raise ValueError("Unsupported coverage ledger schema")
    return value


def save_ledger(path, ledger):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(ledger, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def ledger_lock(path):
    # Concurrent captures/classifications must not silently lose each other.
    import fcntl
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield
        fcntl.flock(stream, fcntl.LOCK_UN)


def ingest(ledger, source_id, snapshot, original_path):
    rows = snapshot.get("rows")
    if not isinstance(rows, list):
        raise ValueError("snapshot.rows must be a list")
    url = snapshot.get("url", "")
    if not url:
        raise ValueError("snapshot.url is required to identify the filter scope")
    scope = {"url": canonical_url(url, scope=True), "query": snapshot.get("query")}
    sources = ledger["sources"]
    source = sources.setdefault(source_id, {"scope": scope, "items": {}, "captures": []})
    if source["scope"] != scope:
        raise ValueError("Source filter/query changed; use a new source ID for this sweep")
    items = source["items"]
    captured_at = snapshot.get("capturedAt", snapshot.get("checkedAt", now()))
    capture_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True,
                                            ensure_ascii=False).encode()).hexdigest()
    if any(c["snapshotSHA256"] == capture_hash for c in source["captures"]):
        return source
    capture_keys = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Every snapshot row must be an object")
        names = aliases(row)
        strong = [a for a in names if not a.startswith("fp:")]
        matches = [key for key, item in items.items()
                   if set(strong or names) & set(item["aliases"])]
        if not matches and strong:
            # A later capture may finally expose an ID for a fingerprint-only
            # row. Upgrade that identity, but never merge two distinct strong IDs.
            fingerprints = {a for a in names if a.startswith("fp:")}
            matches = [key for key, item in items.items()
                       if all(a.startswith("fp:") for a in item["aliases"])
                       and fingerprints & set(item["aliases"])]
        if len(matches) > 1:
            raise ValueError("Ambiguous identity aliases; resolve duplicate ledger items explicitly")
        key = matches[0] if matches else names[0]
        capture_keys.append(key)
        item = items.setdefault(key, {"aliases": [], "firstSeenAt": captured_at})
        item["aliases"] = sorted(set(item["aliases"]) | set(names))
        # Preserve classification and its evidence across all later captures.
        item["row"] = row
        item["lastSeenAt"] = captured_at
    total = snapshot.get("total")
    if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
        raise ValueError("snapshot.total must be a nonnegative integer or null")
    capture = {"url": url, "query": snapshot.get("query"), "total": total,
               "readCount": len(items), "capturedRows": len(rows),
               "distinctCaptureIdentities": len(set(capture_keys)),
               "repeatedIdentityRows": len(capture_keys) - len(set(capture_keys)),
               "cursor": snapshot.get("cursor", snapshot.get("lastCursor",
                           rows[-1].get("index", rows[-1].get("sourceId", rows[-1].get("id")))
                           if rows else None)),
               "pages": snapshot.get("pages"), "complete": snapshot.get("complete", False),
               "endEvidence": snapshot.get("endEvidence"),
               "watermarkProof": snapshot.get("watermarkProof"),
               "blocked": snapshot.get("blocked"),
               "capturedAt": captured_at,
               "originalPath": str(Path(original_path).resolve()),
               "snapshotSHA256": capture_hash}
    source["captures"].append(capture)
    source["latest"] = capture
    return source


def find_item(source, item_ref):
    if item_ref in source["items"]:
        return source["items"][item_ref]
    matches = [item for item in source["items"].values()
               if item_ref in item["aliases"] or "id:" + item_ref in item["aliases"]
               or "url:" + canonical_url(item_ref) in item["aliases"]]
    if len(matches) != 1:
        raise ValueError("Item must resolve to exactly one ingested stable ID/URL/key")
    return matches[0]


def official_verified(classification):
    status = classification.get("officialStatus")
    evidence = classification.get("officialApplicationEvidence")
    if status == "official_closed":
        return text_evidence(classification.get("officialClosureEvidence"))
    return status == "official_open" and text_evidence(evidence)


def classify(source, item_ref, disposition, reason, evidence=None, **fields):
    if disposition not in DISPOSITIONS:
        raise ValueError("Unknown disposition")
    if not text_evidence(reason):
        raise ValueError("A nonempty reason is required; titles/keywords are not decisions")
    if disposition == "out_of_scope" and not text_evidence(evidence):
        raise ValueError("out_of_scope requires explicit geographic exclusion evidence; unknown eligibility is not out of scope")
    item = find_item(source, item_ref)
    classification = dict(item.get("classification", {}))
    classification.update({"disposition": disposition, "reason": reason, "classifiedAt": now()})
    if disposition == "out_of_scope":
        classification["scopeDimension"] = "geography"
    if evidence is not None:
        classification["evidence"] = evidence
    classification.update({k: v for k, v in fields.items() if v is not None})
    if disposition == "review_ready" and not (
            classification.get("jdReviewed") is True and official_verified(classification)
            and classification.get("officialStatus") == "official_open"):
        raise ValueError("review_ready requires full JD review AND separate official application evidence")
    if disposition == "official_closed" and not (
            classification.get("officialStatus") == "official_closed" and official_verified(classification)):
        raise ValueError("official_closed requires official closure evidence, not aggregator absence/403")
    item["classification"] = classification


def block_report(block):
    if not block:
        return None
    if not isinstance(block, dict):
        return {"valid": False, "reason": "Block declaration lacks structured evidence/next step/retryAt"}
    if block.get("type") in {"signup_promotion", "registration_promotion", "newsletter", "ordinary_popup"}:
        return {"valid": False, "reason": "Ordinary registration/promotion popup is not a security block",
                "nextStep": "Dismiss normal promotional UI and continue the existing list"}
    valid = (all(text_evidence(block.get(k)) for k in ("reason", "nextStep", "evidence"))
             and (text_evidence(block.get("retryAt"))
                  or text_evidence(block.get("retryCondition"))))
    return dict(block, valid=valid)


def source_report(source_id, source):
    latest = source.get("latest", {})
    items = list(source["items"].values())
    total = latest.get("total")
    count = len(items)
    block = block_report(latest.get("blocked"))
    declared_block = bool(latest.get("blocked"))
    count_matches = isinstance(total, int) and not isinstance(total, bool) and count == total
    watermark = latest.get("watermarkProof") or {}
    # Incremental stopping is allowed only at an actually captured known boundary.
    boundary = watermark.get("boundarySourceId") if isinstance(watermark, dict) else None
    boundary_seen = bool(boundary) and any("id:" + str(boundary) in i["aliases"] for i in items)
    incremental_proof = (isinstance(watermark, dict) and watermark.get("reached") is True
                         and text_evidence(watermark.get("previousWatermark"))
                         and text_evidence(watermark.get("evidence")) and boundary_seen)
    list_complete = (not declared_block and latest.get("complete") is True
                     and end_evidence(latest.get("endEvidence"), count)
                     and (count_matches or incremental_proof))
    counts = {name: 0 for name in sorted(DISPOSITIONS)}
    unclassified = []
    unverified = []
    unhanded = []
    for key, item in source["items"].items():
        c = item.get("classification", {})
        disposition = c.get("disposition")
        if disposition not in DISPOSITIONS or not text_evidence(c.get("reason")):
            unclassified.append(key)
            continue
        counts[disposition] += 1
        # A JD experience/skills gap is not proof an honest application cannot
        # proceed. Holds need a concrete obstacle, not merely "required 2 years".
        unresolved_hold = (disposition in {"qualification_hold", "hard_qualification_gap"}
                           and not text_evidence(c.get("nonProceedableEvidence")))
        if disposition not in EXEMPT and (disposition == "needs_official_verification"
                or unresolved_hold or c.get("jdReviewed") is not True or not official_verified(c)):
            unverified.append(key)
        if disposition == "review_ready" and not text_evidence(c.get("handoffEvidence")):
            unhanded.append(key)
    triage_complete = list_complete and not unclassified
    verification_complete = triage_complete and not unverified
    handoff_complete = verification_complete and not unhanded
    return {"source": source_id, "scope": source.get("scope"), "total": total,
            "readCount": count, "lastCaptureRows": latest.get("capturedRows"),
            "repeatedIdentityRows": latest.get("repeatedIdentityRows", 0),
            "missingIdentifiedItems": max(0, total - count) if isinstance(total, int) else None,
            "lastCursor": latest.get("cursor"), "pages": latest.get("pages"),
            "capturedAt": latest.get("capturedAt"), "originalPath": latest.get("originalPath"),
            "endEvidence": latest.get("endEvidence"), "blocked": block,
            "countMatchesTotal": count_matches, "incrementalWatermarkProven": incremental_proof,
            "listComplete": list_complete, "classificationComplete": triage_complete,
            "officialVerificationComplete": verification_complete, "handoffComplete": handoff_complete,
            "allComplete": handoff_complete, "dispositions": counts,
            "unclassified": unclassified, "needsOfficialVerification": unverified,
            "readyNotHandedOff": unhanded,
            "status": "all_complete" if handoff_complete else "blocked" if block and block["valid"] else "incomplete"}


def required_sources(manifest):
    if manifest is None:
        return []
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be an object with nonempty requiredSources[]")
    required = manifest.get("requiredSources")
    if (not isinstance(required, list) or not required
            or any(not text_evidence(key) for key in required)):
        raise ValueError("Manifest requiredSources must be a nonempty list of source IDs")
    required = [key.strip() for key in required]
    if len(required) != len(set(required)):
        raise ValueError("Manifest requiredSources must not contain duplicate IDs")
    return required


def report(ledger, source_id=None, manifest=None):
    sources = ledger["sources"]
    required = required_sources(manifest)
    missing = [key for key in required if key not in sources]
    if source_id:
        if source_id not in sources and source_id not in required:
            raise ValueError("Unknown source ID")
        # A filter must not hide other manifest-required incomplete sources.
        selected = {key: sources[key] for key in dict.fromkeys([source_id] + required)
                    if key in sources}
    else:
        selected = sources
    results = [source_report(key, value) for key, value in selected.items()]
    return {"schemaVersion": 1, "sources": results,
            "manifestProvided": manifest is not None,
            "sourcePresence": not missing if manifest is not None else None,
            "requiredSources": required, "missingSources": missing,
            "allComplete": bool(results) and not missing and all(r["allComplete"] for r in results)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("ingest", "classify", "report", "gate"):
        p = commands.add_parser(name)
        p.add_argument("--ledger", required=True)
        p.add_argument("--source", required=name in {"ingest", "classify"})
        if name in {"report", "gate"}:
            p.add_argument("--manifest", help="JSON with nonempty requiredSources[]; missing sources fail the gate")
        if name == "ingest":
            p.add_argument("--snapshot", required=True)
        if name == "classify":
            p.add_argument("--item", required=True)
            p.add_argument("--disposition", required=True, choices=sorted(DISPOSITIONS))
            p.add_argument("--reason", required=True)
            p.add_argument("--evidence")
            p.add_argument("--jd-reviewed", action="store_true", default=None)
            p.add_argument("--official-status", choices=["official_open", "official_closed", "aggregator_only", "unknown"])
            p.add_argument("--official-application-evidence")
            p.add_argument("--official-closure-evidence")
            p.add_argument("--handoff-evidence")
            p.add_argument("--non-proceedable-evidence", help="Concrete obstacle to truthful application; JD years alone is not enough")
            p.add_argument("--application-tier", choices=["priority", "stretch"])
            p.add_argument("--qualification-gap")
        if name == "gate":
            p.add_argument("--stage", choices=["list", "classify", "verify", "handoff", "all"], default="all")
    args = parser.parse_args(argv)
    try:
        with ledger_lock(args.ledger):
            ledger = load_ledger(args.ledger)
            if args.command == "ingest":
                snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
                ingest(ledger, args.source, snapshot, args.snapshot)
                save_ledger(args.ledger, ledger)
            elif args.command == "classify":
                classify(ledger["sources"][args.source], args.item, args.disposition,
                         args.reason, args.evidence, jdReviewed=args.jd_reviewed,
                         officialStatus=args.official_status,
                         officialApplicationEvidence=args.official_application_evidence,
                         nonProceedableEvidence=args.non_proceedable_evidence,
                         applicationTier=args.application_tier,
                         qualificationGap=args.qualification_gap,
                         officialClosureEvidence=args.official_closure_evidence,
                         handoffEvidence=args.handoff_evidence)
                save_ledger(args.ledger, ledger)
            manifest_path = getattr(args, "manifest", None)
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8")) if manifest_path else None
            result = report(ledger, args.source, manifest)
            if manifest_path:
                result["manifestPath"] = str(Path(manifest_path).resolve())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "gate":
            field = {"list": "listComplete", "classify": "classificationComplete",
                     "verify": "officialVerificationComplete", "handoff": "handoffComplete",
                     "all": "allComplete"}[args.stage]
            return 0 if (result["sources"] and not result["missingSources"]
                         and all(r[field] for r in result["sources"])) else 1
        return 0
    except (ValueError, KeyError, OSError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
