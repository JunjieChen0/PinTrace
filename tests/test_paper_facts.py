import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

# The manuscript is archived at Zenodo, not in this repository; copy the archive's paper/ into the clone to run these.
PAPER = Path(__file__).resolve().parents[1] / "paper" / "jsep_v2"
TOKEN = re.compile(r"<<([A-Za-z0-9_]+)>>")
pytestmark = pytest.mark.skipif(not (PAPER / "main.src.tex").is_file(),
                                reason="manuscript not present: copy paper/ from the Zenodo results archive")


def load_facts():
    spec = importlib.util.spec_from_file_location("paper_v2_facts", PAPER / "tools" / "facts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build()


def test_every_token_in_the_manuscript_source_resolves_to_a_fact():
    facts = load_facts()
    tokens = set(TOKEN.findall((PAPER / "main.src.tex").read_text(encoding="utf-8")))
    assert tokens and not tokens - set(facts.values)


def test_every_fact_names_a_result_file_or_its_derivation():
    for row in load_facts().ledger:
        source = row["source"]
        assert ("file" in source and source["pointer"].startswith("/")) or source.get("derived_from"), row["key"]


def test_resolved_manuscript_matches_its_ledger_and_has_no_unresolved_token():
    ledger = json.loads((PAPER / "claim_ledger.json").read_text(encoding="utf-8"))
    text = (PAPER / "main.tex").read_bytes()
    assert b"<<" not in text
    assert hashlib.sha256(text).hexdigest() == ledger["resolved"]["sha256"]
    assert hashlib.sha256((PAPER / "main.src.tex").read_bytes()).hexdigest() == ledger["source"]["sha256"]


def test_ledger_values_equal_a_fresh_resolution_of_the_current_reports():
    facts = load_facts()
    ledger = json.loads((PAPER / "claim_ledger.json").read_text(encoding="utf-8"))
    assert ledger["claims"]
    for claim in ledger["claims"]:
        assert facts.values[claim["key"]] == claim["rendered"], claim["key"]


def test_every_figure_the_manuscript_includes_is_present():
    text = (PAPER / "main.tex").read_text(encoding="utf-8")
    for name in re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", text):
        assert (PAPER / "figures" / (name if name.endswith(".pdf") else name + ".pdf")).is_file(), name
