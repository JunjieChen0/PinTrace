"""Error-oriented gold set for reference classification and change labels, with an interval blind re-label.

Sampling is stratified by the cases where the pipeline is most likely to be wrong: references the lineage rules could
not match (``ambiguous_transition``), replacements, first/copied/reintroduced entries, references carrying a version
comment that is not verified, and each claimed change class (pin, unpin, SHA change, tag change, replace, entry). No
stratum is a uniform random sample of the corpus; the point is to concentrate the audit where errors would live.

Each item shows only what a reader of the repository would see at that commit: file path, job, step anchors, the raw
``uses:`` value and any attached comment. The pipeline's own labels live in a separate answer key and are joined only at
scoring time.

Labelling protocol. The labels are produced twice by the same model: round one in the working context immediately after
the packet is built, round two in a fresh context that sees only the packet in a different order and neither the answer
key nor round one's answers. The report gives agreement of each round with the pipeline and agreement between the two
rounds (self-consistency), with cluster-of-items bootstrap intervals, and discloses the wall-clock interval between the
rounds. This is a reproducibility check of one labeller, not agreement between independent human raters.
"""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import statistics

from .formal_common import sha256, write_json

RULE_VERSION = "validity-goldset-v1"
BASE = "data/derived/validity-v1/goldset"
EVENTS = "data/derived/tables-v5/lineage_events.csv"
SALT = "pintrace-goldset-2026-10-20"
BOOTSTRAP_SEED = 20261018
BOOTSTRAP_REPLICATES = 2000
EVENT_LABELS = {"pin": "pin", "unpin": "unpin", "sha_target_change": "sha_change", "mutable_update": "tag_change",
                "replace": "replace", "remove": "remove", "first_observed_action": "entry", "copied_reference": "entry",
                "reintroduced_action": "entry", "ambiguous_transition": "unknown", "unknown_boundary": "unknown"}
ENTRY_EVENTS = ("first_observed_action", "copied_reference", "reintroduced_action")
MAX_REFS = 6
REFERENCE_STRATA = {"ambiguous_after": 20, "ambiguous_before": 10, "replace_after": 10, "entry_after": 10, "commented": 10}
TRANSITION_STRATA = {"pin": 25, "unpin": 10, "sha_target_change": 10, "mutable_update": 10, "replace": 10,
                     "ambiguous_transition": 15, "entry": 10}
REFERENCE_LABELS = ("pinned", "mutable", "not_direct", "unresolvable")
TRANSITION_LABELS = ("pin", "unpin", "sha_change", "tag_change", "no_change", "replace", "remove", "entry", "unknown")


def rank(item, salt=SALT):
    return hashlib.sha256((salt + "\0" + item).encode("utf-8")).hexdigest()


def visible_reference(ref):
    """Only what a reader of the file sees; pipeline judgements (kind, ref_type, lineage status, coverage) are left out."""
    return dict(path=ref.get("path"), job=ref.get("job"), step_index=ref.get("step_index"), step_id=ref.get("step_id"),
                step_name=ref.get("step_name"), line=ref.get("line"), value=ref.get("value"),
                comment=ref.get("comment_text"))


def pipeline_reference_label(ref):
    if ref.get("kind") == "action":
        if ref.get("ref_type") == "full_sha":
            return "pinned"
        if ref.get("ref_type") == "mutable_or_unresolved":
            return "mutable"
        return "unresolvable"
    if ref.get("kind") in {"local", "docker", "reusable_workflow", "dynamic", "unsupported"}:
        return "not_direct"
    return "unresolvable"


def pipeline_transition_label(event):
    return EVENT_LABELS.get(event, "unknown")


def _refs(value):
    """A payload side is a single reference object, a list of them, or absent."""
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def rows(path):
    csv.field_size_limit(2**31 - 1)
    with Path(path).open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            payload = json.loads(row["payload"])
            yield dict(event_id=row["event_id"], event=row["event"], repository_id=row["repository_id"],
                       before=_refs(payload.get("before")), after=_refs(payload.get("after")),
                       commit=row["commit"], previous_commit=row["previous_commit"])


def candidates(root):
    """Every event that can yield a gold-set item, as (stratum, evidence, pipeline label, source).

    A reference can qualify for more than one stratum (for example a commented reference that also follows a
    replacement); it is kept once, under the first stratum that produced it, so the 150 items are distinct references.
    """
    found, seen = [], {}

    def add(item):
        key = (item["kind"], item["source"]["event_id"], item["source"]["step"])
        if key not in seen:
            seen[key] = item
            found.append(item)

    for row in rows(Path(root) / EVENTS):
        event, before, after = row["event"], row["before"], row["after"]
        if event in ("pin", "unpin", "sha_target_change", "mutable_update") and len(before) == 1 and len(after) == 1:
            add(dict(kind="transition", stratum=event, evidence=dict(before=[visible_reference(before[0])], after=[visible_reference(after[0])]),
                     pipeline=pipeline_transition_label(event), source=dict(step="event", event_id=row["event_id"])))
        elif event == "replace" and len(before) == 1 and len(after) == 1:
            add(dict(kind="transition", stratum="replace", evidence=dict(before=[visible_reference(before[0])], after=[visible_reference(after[0])]),
                     pipeline="replace", source=dict(step="event", event_id=row["event_id"])))
            add(dict(kind="reference", stratum="replace_after", evidence=visible_reference(after[0]),
                     pipeline=pipeline_reference_label(after[0]), source=dict(step="after", event_id=row["event_id"])))
        elif event in ENTRY_EVENTS and after:
            add(dict(kind="transition", stratum="entry", evidence=dict(before=[], after=[visible_reference(ref) for ref in after[:MAX_REFS]]),
                     pipeline="entry", source=dict(step="event", event_id=row["event_id"])))
            add(dict(kind="reference", stratum="entry_after", evidence=visible_reference(after[0]),
                     pipeline=pipeline_reference_label(after[0]), source=dict(step="after", event_id=row["event_id"])))
        elif event == "ambiguous_transition" and len(before) <= MAX_REFS and len(after) <= MAX_REFS:
            add(dict(kind="transition", stratum="ambiguous_transition",
                     evidence=dict(before=[visible_reference(ref) for ref in before], after=[visible_reference(ref) for ref in after]),
                     pipeline="unknown", source=dict(step="event", event_id=row["event_id"])))
            if after:
                add(dict(kind="reference", stratum="ambiguous_after", evidence=visible_reference(after[0]),
                         pipeline=pipeline_reference_label(after[0]), source=dict(step="after", event_id=row["event_id"])))
            if before:
                add(dict(kind="reference", stratum="ambiguous_before", evidence=visible_reference(before[0]),
                         pipeline=pipeline_reference_label(before[0]), source=dict(step="before", event_id=row["event_id"])))
        if event not in ("ambiguous_transition",) and len(after) == 1 and after[0].get("comment_text"):
            add(dict(kind="reference", stratum="commented", evidence=visible_reference(after[0]),
                     pipeline=pipeline_reference_label(after[0]), source=dict(step="after", event_id=row["event_id"])))
    return found


def sample(found):
    """Take the requested count from each stratum by a hash ranking, independently of file order."""
    chosen, shortfall = [], {}
    for kind, strata in (("reference", REFERENCE_STRATA), ("transition", TRANSITION_STRATA)):
        for stratum, count in strata.items():
            pool = [item for item in found if item["kind"] == kind and item["stratum"] == stratum]
            pool.sort(key=lambda item: rank(item["source"]["event_id"] + "/" + item["source"]["step"]))
            take = pool[:count]
            if len(take) < count:
                shortfall[stratum] = count - len(take)
            chosen.extend(take)
    return chosen, shortfall


def source_key(item):
    return item["source"]["event_id"] + "/" + item["source"]["step"]


def item_id(item):
    """Stable, opaque id: the same source item keeps its id across the two packets, and the id reveals no class."""
    return "g" + hashlib.sha256((SALT + "|" + source_key(item)).encode("utf-8")).hexdigest()[:10]


def packet(items, order_salt):
    """The same items in a packet-specific order; only the order differs between the two rounds."""
    ordered = sorted(items, key=lambda item: rank(source_key(item) + "/" + order_salt))
    return [dict(id=item_id(item), kind=item["kind"], task="classify_reference" if item["kind"] == "reference" else "classify_change",
                 evidence=item["evidence"]) for item in ordered]


def tasks():
    return dict(classify_reference=dict(question="这条 uses 引用是什么？", labels=list(REFERENCE_LABELS)),
                classify_change=dict(question="按 before/after 两列证据，相对 before 发生了什么？", labels=list(TRANSITION_LABELS)))


def build(root, base=BASE):
    root = Path(root).resolve()
    found = candidates(root)
    chosen, shortfall = sample(found)
    if len({item_id(item) for item in chosen}) != len(chosen):
        raise ValueError("Two sampled items share an id")
    first, second = packet(chosen, "round-one"), packet(chosen, "round-two")
    if {entry["id"] for entry in first} != {entry["id"] for entry in second}:
        raise ValueError("The two packets must contain the same items")
    if [entry["id"] for entry in first] == [entry["id"] for entry in second]:
        raise ValueError("The two packets must not share an order")
    key = {item_id(item): dict(kind=item["kind"], pipeline=item["pipeline"], stratum=item["stratum"], source=item["source"]) for item in chosen}
    directory = root / base
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "packet_round1.json", dict(rule_version=RULE_VERSION, tasks=tasks(), items=first))
    write_json(directory / "packet_round2.json", dict(rule_version=RULE_VERSION, tasks=tasks(), items=second))
    write_json(directory / "answer_key.json", key)
    write_json(directory / "build.json", dict(rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(),
                                              implementation_sha256=sha256(Path(__file__)), event_file=EVENTS, event_sha256=sha256(root / EVENTS),
                                              salt=SALT, candidates=len(found), chosen=len(chosen), shortfall=shortfall,
                                              strata={**REFERENCE_STRATA, **TRANSITION_STRATA},
                                              pipeline_label_counts=_counts(item["pipeline"] for item in chosen)))
    return dict(items=first, key=key, directory=directory, build=json.loads((directory / "build.json").read_text(encoding="utf-8")))


def _counts(values):
    output = {}
    for value in values:
        output[value] = output.get(value, 0) + 1
    return dict(sorted(output.items()))


def kappa(first, second):
    """Cohen's kappa over the union of labels used by either rater; None when either rater is constant."""
    pairs = list(zip(first, second))
    if not pairs:
        return None
    labels = sorted(set(first) | set(second))
    observed = sum(a == b for a, b in pairs) / len(pairs)
    left = {label: sum(a == label for a, _ in pairs) for label in labels}
    right = {label: sum(b == label for _, b in pairs) for label in labels}
    expected = sum(left[label] * right[label] for label in labels) / len(pairs) ** 2
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)


def agreement(first, second):
    pairs = list(zip(first, second))
    return dict(items=len(pairs), agree=sum(a == b for a, b in pairs), rate=sum(a == b for a, b in pairs) / len(pairs) if pairs else None,
                kappa=kappa(first, second))


def bootstrap_agreement(first, second, seed=BOOTSTRAP_SEED, replicates=BOOTSTRAP_REPLICATES):
    pairs = list(zip(first, second))
    if not pairs:
        return dict(rate=[None, None], kappa=[None, None])
    rng = random.Random(seed)
    rates, kappas = [], []
    for _ in range(replicates):
        draw = [pairs[rng.randrange(len(pairs))] for _ in range(len(pairs))]
        rates.append(sum(a == b for a, b in draw) / len(draw))
        value = kappa([a for a, _ in draw], [b for _, b in draw])
        if value is not None:
            kappas.append(value)
    quantile = lambda values: [statistics.quantiles(values, n=40)[0], statistics.quantiles(values, n=40)[-1]] if values else [None, None]
    return dict(rate=quantile(rates), kappa=quantile(kappas), kappa_valid_replicates=len(kappas))


def load_labels(path, packet_path):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    items = json.loads(Path(packet_path).read_text(encoding="utf-8"))["items"]
    labels = document["labels"] if isinstance(document, dict) else document
    expected = {item["id"] for item in items}
    if set(labels) != expected:
        missing, extra = sorted(expected - set(labels))[:5], sorted(set(labels) - expected)[:5]
        raise ValueError(f"Labels do not match the packet: missing {missing}, unexpected {extra}")
    allowed = {item["id"]: set(REFERENCE_LABELS if item["kind"] == "reference" else TRANSITION_LABELS) for item in items}
    bad = {key: value for key, value in labels.items() if value not in allowed[key]}
    if bad:
        raise ValueError("Labels outside the stated vocabulary: " + json.dumps(dict(list(bad.items())[:5])))
    return labels


def directional(key, labels):
    """Event-level errors that matter: an event the pipeline claims but the label does not, and the reverse.

    A pipeline ``unknown`` against a ``no_change`` label is conservative (it declines to match where the reference
    multiset did not change); it is counted separately from a missed event.
    """
    claimed = {"pin", "unpin", "sha_change", "tag_change", "replace", "remove", "entry"}
    output = dict(items=0, fabricated=[], missed=[], conservative=[])
    for identifier, value in labels.items():
        if key[identifier]["kind"] != "transition":
            continue
        output["items"] += 1
        pipeline = key[identifier]["pipeline"]
        if pipeline in claimed and value != pipeline:
            output["fabricated"].append(dict(id=identifier, pipeline=pipeline, label=value))
        elif pipeline == "unknown" and value in claimed:
            output["missed"].append(dict(id=identifier, label=value))
        elif pipeline == "unknown" and value == "no_change":
            output["conservative"].append(dict(id=identifier))
    for name in ("fabricated", "missed", "conservative"):
        output[name] = dict(count=len(output[name]), items=output[name][:10])
    return output


def score(key, round1, round2):
    """Agreement of each round with the pipeline and between rounds, overall and per stratum."""
    identifiers = sorted(key)
    pipeline = [key[i]["pipeline"] for i in identifiers]
    first = [round1[i] for i in identifiers]
    second = [round2[i] for i in identifiers]
    result = dict(
        overall=dict(round1_vs_pipeline=dict(agreement(first, pipeline), interval=bootstrap_agreement(first, pipeline)),
                     round2_vs_pipeline=dict(agreement(second, pipeline), interval=bootstrap_agreement(second, pipeline)),
                     round1_vs_round2=dict(agreement(first, second), interval=bootstrap_agreement(first, second))),
        directional=dict(round1=directional(key, round1), round2=directional(key, round2)),
        by_kind={}, by_stratum={}, confusion={}, disagreements=[])
    for kind in ("reference", "transition"):
        group = [i for i in identifiers if key[i]["kind"] == kind]
        if not group:
            continue
        result["by_kind"][kind] = dict(items=len(group),
                                       round1_vs_pipeline=agreement([round1[i] for i in group], [key[i]["pipeline"] for i in group]),
                                       round2_vs_pipeline=agreement([round2[i] for i in group], [key[i]["pipeline"] for i in group]),
                                       round1_vs_round2=agreement([round1[i] for i in group], [round2[i] for i in group]))
    for stratum in sorted({key[i]["stratum"] for i in identifiers}):
        group = [i for i in identifiers if key[i]["stratum"] == stratum]
        result["by_stratum"][stratum] = dict(items=len(group), kind=key[group[0]]["kind"],
                                             round1_vs_pipeline=agreement([round1[i] for i in group], [key[i]["pipeline"] for i in group]),
                                             round1_vs_round2=agreement([round1[i] for i in group], [round2[i] for i in group]))
    pairs = {}
    for i in identifiers:
        pairs[(key[i]["pipeline"], round1[i])] = pairs.get((key[i]["pipeline"], round1[i]), 0) + 1
    result["confusion"] = {f"{pipeline_label} -> {labelled}": count for (pipeline_label, labelled), count in sorted(pairs.items())}
    for i in identifiers:
        if round1[i] != key[i]["pipeline"] or round2[i] != key[i]["pipeline"]:
            result["disagreements"].append(dict(id=i, stratum=key[i]["stratum"], pipeline=key[i]["pipeline"], round1=round1[i], round2=round2[i],
                                                source=key[i]["source"]))
    return result


def label_interval(first_path, second_path):
    """Minutes between the two recorded completion stamps; falls back to file times when a stamp is absent."""
    stamps = []
    for path in (first_path, second_path):
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        value = document.get("completed_at_utc") if isinstance(document, dict) else None
        try:
            stamps.append(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None)
        except ValueError:
            stamps.append(None)
    if any(stamp is None for stamp in stamps):
        stamps = [os.path.getmtime(path) for path in (first_path, second_path)]
    return round(abs(stamps[1] - stamps[0]) / 60, 1)


def run(root, round1, round2, base=BASE, interval_minutes=None):
    root = Path(root).resolve()
    directory = root / base
    key = json.loads((directory / "answer_key.json").read_text(encoding="utf-8"))
    first = load_labels(round1, directory / "packet_round1.json")
    second = load_labels(round2, directory / "packet_round2.json")
    body = score(key, first, second)
    if interval_minutes is None:
        interval_minutes = label_interval(round1, round2)
    build_record = json.loads((directory / "build.json").read_text(encoding="utf-8"))
    report = dict(
        rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(), implementation_sha256=sha256(Path(__file__)),
        input_sha256={EVENTS: build_record["event_sha256"], str((directory / "answer_key.json").relative_to(root).as_posix()): sha256(directory / "answer_key.json"),
                      str((Path(round1).resolve()).relative_to(root).as_posix()): sha256(round1),
                      str((Path(round2).resolve()).relative_to(root).as_posix()): sha256(round2)},
        design=dict(strata=build_record["strata"], chosen=build_record["chosen"], candidates=build_record["candidates"], shortfall=build_record["shortfall"],
                    salt=build_record["salt"], pipeline_label_counts=build_record["pipeline_label_counts"],
                    sampling="hash-ranked within each error-prone stratum; not a uniform random sample of the corpus"),
        protocol=dict(labellers="one model (Qoder Desktop; DeepSeek v4.1 flash; accessed October 2026), two independent contexts",
                      round1="working context, immediately after the packet was built",
                      round2="fresh context given only the reshuffled packet, without the answer key or round-one labels",
                      interval_minutes=interval_minutes,
                      interval_note="The interval is the wall-clock gap between the two label files. Independence of round two comes "
                                    "from its fresh context, not from elapsed time; a short interval limits drift but is a weak test of it.",
                      independence="same model; this measures self-consistency, not agreement between human raters"),
        **body,
        checks=dict(one_hundred_fifty_items=len(key) == 150, every_stratum_filled=not build_record["shortfall"],
                    round1_vs_round2_rate_at_least_0_8=(body["overall"]["round1_vs_round2"]["rate"] or 0) >= 0.8,
                    pinned_disagreements_listed=all(d["stratum"] in build_record["strata"] for d in body["disagreements"])),
        limits=["The sample is error-oriented by design; agreement rates do not estimate accuracy over the whole corpus.",
                "Both rounds come from the same model in different contexts, minutes apart; perfect self-consistency shows the labelling is stable, not that it is correct.",
                "The pipeline's `unknown` for an ambiguous transition concerns instance identity; the gold label describes the reference multiset, so those 15 cases count as disagreements by construction.",
                "Reference-level items were all direct actions in the sampled events, so `not_direct` is not exercised on real data; the adversarial suite covers those forms.",
                "Labels are the researcher's own reading of the same evidence the pipeline used, without independent human raters."])
    write_json(directory / "score.json", report)
    (root / "reports/validity_goldset.json").write_bytes((directory / "score.json").read_bytes())
    rates = {name: block["rate"] for name, block in body["overall"].items()}
    print(json.dumps(dict(completed=True, checks=report["checks"], rates=rates, directory=directory.relative_to(root).as_posix())), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "score"))
    parser.add_argument("--round1")
    parser.add_argument("--round2")
    parser.add_argument("--interval-minutes", type=float)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if arguments.command == "build":
        built = build(root)
        print(json.dumps(dict(built=True, directory=str(built["directory"]))), flush=True)
    else:
        if not arguments.round1 or not arguments.round2:
            parser.error("score needs --round1 and --round2")
        run(root, arguments.round1, arguments.round2, interval_minutes=arguments.interval_minutes)
