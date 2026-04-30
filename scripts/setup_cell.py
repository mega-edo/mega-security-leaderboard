"""Build a single (slot × scenario) cell workspace.

Materializes a `.mega_security/` directory whose probe layout matches
what the prompt-check / prompt-optimize skills expect:

    <cell_dir>/
      system_prompt.txt                      (copy of souls/<scenario>.SOUL.md)
      .mega_security/
        probes/
          train/
            prompt_injection.jsonl     (25, subsampled from data/attacks/train)
            jailbreak.jsonl            (25)
            pii_disclosure.jsonl       (25)
            system_prompt_leak.jsonl   (25)
            benign.jsonl               (16, copied from data/benign/train/<scenario>.jsonl)
          val/
            (same shape)
        cell_manifest.json             (records subsample seeds + sha256s)

Determinism:
  - Attack subsample uses sha256(scenario_id + split + cat + pool_seed)
    as the shuffle seed → same 25 picked for every slot within one
    scenario, so cross-slot comparison is fair.
  - Benign is already pre-split (data/benign/{train,val}/<scenario>.jsonl,
    16 rows each, stratified). The cell just copies the file.

Idempotent: if cell_manifest.json already exists with a matching
input_signature, the cell is left untouched.

Usage (CLI, for inspection):
  python setup_cell.py --scenario compass --output /tmp/test-cell

Programmatic use:
  from setup_cell import setup_cell, load_scenario
  setup_cell(scenario_id="compass", cell_dir=Path(...))
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LEADERBOARD = REPO_ROOT / "leaderboard"
DATA_ATTACKS = LEADERBOARD / "data" / "attacks"
DATA_BENIGN = LEADERBOARD / "data" / "benign"
SOULS_DIR = LEADERBOARD / "souls"

CATEGORIES = ("prompt_injection", "jailbreak", "pii_disclosure", "system_prompt_leak")
SPLITS = ("train", "val")
ATTACK_N_PER_SPLIT_PER_CAT = 25  # subsampled from the frozen pool's 50/cat/split


# ---------- I/O ----------

def _read_jsonl(path: Path) -> list[dict]:
    """Split only on \\n. Some probe rows contain unicode line separators
    (U+2028) inside their prompt fields; str.splitlines() would treat
    those as line breaks and corrupt the JSON.
    """
    out: list[dict] = []
    if not path.is_file():
        return out
    for ln in path.read_text().split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        out.append(json.loads(ln))
    return out


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    path.write_text(body + ("\n" if rows else ""))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed_from(*parts: str) -> int:
    h = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return int(h[:16], 16)


# ---------- pool & scenario ----------

def _load_pool_manifest() -> dict:
    p = DATA_ATTACKS / "manifest.json"
    if not p.is_file():
        raise SystemExit(f"frozen attack pool not found at {p}")
    m = json.loads(p.read_text())
    if not m.get("frozen"):
        raise SystemExit("attack pool manifest is not frozen — refuse to use")
    return m


def load_scenario(scenario_id: str) -> dict:
    """A 'scenario' is the convention {souls/<id>.SOUL.md, data/benign/{train,val}/<id>.jsonl}."""
    soul = SOULS_DIR / f"{scenario_id}.SOUL.md"
    btr = DATA_BENIGN / "train" / f"{scenario_id}.jsonl"
    bva = DATA_BENIGN / "val" / f"{scenario_id}.jsonl"
    for label, p in (("soul", soul), ("benign train", btr), ("benign val", bva)):
        if not p.is_file():
            raise SystemExit(f"{label} not found for scenario '{scenario_id}': {p}")
    return {"id": scenario_id, "soul": soul, "benign_train": btr, "benign_val": bva}


def list_scenarios() -> list[str]:
    return sorted(p.stem.replace(".SOUL", "") for p in SOULS_DIR.glob("*.SOUL.md"))


# ---------- subsampling ----------

def _subsample_attacks(split: str, cat: str, n: int, seed: int) -> tuple[list[dict], str]:
    src = DATA_ATTACKS / split / f"{cat}.jsonl"
    rows = _read_jsonl(src)
    rows.sort(key=lambda r: r["id"])
    import random
    rng = random.Random(seed)
    rng.shuffle(rows)
    picked = rows[:n]
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in picked)
    return picked, _sha256(body)


# ---------- main ----------

def setup_cell(
    scenario_id: str,
    cell_dir: Path,
    *,
    pool_manifest: dict | None = None,
    force: bool = False,
) -> dict:
    """Materialize <cell_dir>/.mega_security/probes/... for a scenario.

    Returns the cell manifest dict (also written to cell_manifest.json).
    """
    pool_manifest = pool_manifest or _load_pool_manifest()
    pool_seed = int(pool_manifest["probe_seed"])

    scenario = load_scenario(scenario_id)
    soul_path = scenario["soul"]
    benign_train_path = scenario["benign_train"]
    benign_val_path = scenario["benign_val"]

    input_signature = {
        "pool_manifest_sha256": pool_manifest["manifest_sha256"],
        "pool_seed": pool_seed,
        "scenario_id": scenario_id,
        "soul_sha256": _sha256(soul_path.read_text()),
        "benign_train_sha256": _sha256(benign_train_path.read_text()),
        "benign_val_sha256": _sha256(benign_val_path.read_text()),
        "attack_n_per_split_per_cat": ATTACK_N_PER_SPLIT_PER_CAT,
    }

    ws = cell_dir / ".mega_security"
    cell_manifest_path = ws / "cell_manifest.json"
    if cell_manifest_path.is_file() and not force:
        existing = json.loads(cell_manifest_path.read_text())
        if existing.get("input_signature") == input_signature:
            return existing

    (ws / "probes" / "train").mkdir(parents=True, exist_ok=True)
    (ws / "probes" / "val").mkdir(parents=True, exist_ok=True)

    # System prompt = SOUL.md verbatim
    (cell_dir / "system_prompt.txt").write_text(soul_path.read_text())

    # Attacks: same 25 per scenario across all slots
    cells_attacks: dict[str, dict[str, dict]] = {}
    for split in SPLITS:
        for cat in CATEGORIES:
            seed = _seed_from(scenario_id, split, cat, str(pool_seed))
            picked, sha = _subsample_attacks(split, cat, ATTACK_N_PER_SPLIT_PER_CAT, seed)
            _write_jsonl(ws / "probes" / split / f"{cat}.jsonl", picked)
            cells_attacks.setdefault(split, {})[cat] = {
                "n": len(picked), "seed": seed, "sha256": sha,
            }

    # Benign: pre-split files, just copy
    train_b = _read_jsonl(benign_train_path)
    val_b = _read_jsonl(benign_val_path)
    _write_jsonl(ws / "probes" / "train" / "benign.jsonl", train_b)
    _write_jsonl(ws / "probes" / "val" / "benign.jsonl", val_b)

    cell_manifest = {
        "scenario_id": scenario_id,
        "input_signature": input_signature,
        "attacks": cells_attacks,
        "benign": {"train": len(train_b), "val": len(val_b)},
    }
    cell_manifest_path.write_text(json.dumps(cell_manifest, indent=2))
    return cell_manifest


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--scenario", required=True,
                   help=f"Scenario id (one of: {', '.join(list_scenarios())})")
    p.add_argument("--output", required=True, type=Path,
                   help="Cell directory (will create .mega_security/ inside)")
    p.add_argument("--force", action="store_true",
                   help="Re-materialize even if cell_manifest matches")
    args = p.parse_args(argv)

    res = setup_cell(args.scenario, args.output, force=args.force)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
