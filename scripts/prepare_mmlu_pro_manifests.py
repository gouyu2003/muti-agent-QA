"""Create the fixed MMLU-Pro experiment manifests from local Parquet."""

import hashlib
import json
from pathlib import Path
import platform
import random
import subprocess
import unicodedata


ROOT = Path(__file__).resolve().parents[1] / "data" / "experiments" / "mmlu-pro"
DATASET_ID = "TIGER-Lab/MMLU-Pro"
REVISION = "b189ec765aa7ed75c8acfea42df31fdae71f97be"
CHOICE_LETTERS = "ABCDEFGHIJ"
PARQUET_READER = """
import { pathToFileURL } from 'node:url';
const [readerPath, sourcePath] = process.argv.slice(1);
const { asyncBufferFromFile, parquetReadObjects } = await import(pathToFileURL(readerPath).href);
const file = await asyncBufferFromFile(sourcePath);
const columns = ['question_id', 'category', 'question', 'options', 'answer', 'answer_index'];
const rows = await parquetReadObjects({ file, columns });
process.stdout.write(JSON.stringify(rows, (_, value) =>
    typeof value === 'bigint' ? Number(value) : value
));
"""
SELECTIONS = (
    ("development-100", "development", "test", 20261123, {
        "math": 25, "physics": 25, "engineering": 25, "business": 25,
    }),
    ("smoke-5", "smoke", "validation", 20261124, {
        "math": 1, "physics": 1, "engineering": 2, "business": 1,
    }),
    ("calibration-60", "development", "test", 20261125, {
        "chemistry": 15, "economics": 15, "law": 15, "philosophy": 15,
    }),
)
COMPARISON_SELECTION = ("comparison-100", "development", "test", 20261126, {
    "chemistry": 25, "economics": 25, "law": 25, "philosophy": 25,
})


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def manifest_digest(manifest):
    canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256_bytes(canonical.encode("utf-8"))


def convert_choice_row(row):
    question_id = row["question_id"]
    raw_options = row["options"]
    answer_index = row["answer_index"]
    if not 2 <= len(raw_options) <= len(CHOICE_LETTERS):
        raise ValueError(f"Unexpected option count for question {question_id}")
    if not 0 <= answer_index < len(raw_options):
        raise ValueError(f"Invalid answer index for question {question_id}")
    if row["answer"] != CHOICE_LETTERS[answer_index] or raw_options[answer_index] == "N/A":
        raise ValueError(f"Inconsistent reference answer for question {question_id}")

    options = [option for option in raw_options if option != "N/A"]
    effective_index = sum(option != "N/A" for option in raw_options[:answer_index])
    if not 2 <= len(options) or any(not option.strip() for option in options):
        raise ValueError(f"Invalid effective options for question {question_id}")
    if not row["question"].strip():
        raise ValueError(f"Empty question {question_id}")
    return {
        "question_id": question_id,
        "category": row["category"],
        "question": row["question"],
        "options": options,
        "answer": CHOICE_LETTERS[effective_index],
        "answer_index": effective_index,
    }


def normalized_stem(question):
    return " ".join(unicodedata.normalize("NFC", question).split())


def unexposed_rows(rows, split, manifests):
    excluded_ids = {
        row["question_id"] for manifest in manifests if manifest["source_split"] == split
        for row in manifest["records"]
    }
    seen_stems = {
        normalized_stem(row["question"]) for manifest in manifests for row in manifest["records"]
    }
    eligible = []
    for row in sorted(rows, key=lambda row: row["question_id"]):
        stem = normalized_stem(row["question"])
        if row["question_id"] in excluded_ids or stem in seen_stems:
            continue
        seen_stems.add(stem)
        eligible.append(row)
    return eligible


def build_manifest(name, purpose, split, seed, quotas, *, exclude=()):
    source_file = ROOT / "raw" / f"{split}-00000-of-00001.parquet"
    reader = ROOT / ".deps" / "package" / "src" / "node.js"
    if not reader.is_file():
        raise FileNotFoundError(f"Extract hyparquet 1.33.1 into {ROOT / '.deps'}")
    decoded = subprocess.run(
        ["node", "--input-type=module", "-e", PARQUET_READER, str(reader), str(source_file)],
        check=True, stdout=subprocess.PIPE, encoding="utf-8",
    )
    rows = [row for row in json.loads(decoded.stdout) if row["category"] in quotas]
    if len({row["question_id"] for row in rows}) != len(rows):
        raise ValueError(f"Duplicate question_id in source split {split}")
    if exclude:
        rows = unexposed_rows(rows, split, exclude)

    randomizer = random.Random(seed)
    records = []
    for category, count in quotas.items():
        pool = sorted(
            (row for row in rows if row["category"] == category),
            key=lambda row: row["question_id"],
        )
        records.extend(convert_choice_row(row) for row in randomizer.sample(pool, count))

    manifest = {
        "schema_version": 1,
        "name": name,
        "purpose": purpose,
        "dataset_id": DATASET_ID,
        "dataset_revision": REVISION,
        "source_split": split,
        "source_path": source_file.relative_to(ROOT).as_posix(),
        "source_sha256": sha256_bytes(source_file.read_bytes()),
        "sample_count": len(records),
        "sampling": {
            "algorithm": "random.Random.sample from question_id-sorted pools",
            "seed": seed,
            "category_order": list(quotas),
            "quotas": quotas,
            "python_version": platform.python_version(),
        },
        "records": records,
    }
    if exclude:
        manifest["sampling"]["excluded_manifests"] = {
            previous["name"]: previous["manifest_sha256"] for previous in exclude
        }
        manifest["sampling"]["algorithm"] += " after excluding prior IDs and duplicate normalized stems"
    manifest["manifest_sha256"] = manifest_digest(manifest)
    return manifest


def check_distinct_questions(manifests):
    seen = set()
    for manifest in manifests:
        for row in manifest["records"]:
            stem = normalized_stem(row["question"])
            if stem in seen:
                raise ValueError(
                    f"Repeated normalized question in {manifest['name']} at {row['question_id']}"
                )
            seen.add(stem)


def main():
    manifests = [build_manifest(*selection) for selection in SELECTIONS]
    manifests.append(build_manifest(*COMPARISON_SELECTION, exclude=manifests))
    check_distinct_questions(manifests)
    outputs = {
        ROOT / f"{manifest['name']}.json": (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        for manifest in manifests
    }
    for path, data in outputs.items():
        if path.exists() and path.read_bytes() != data:
            raise FileExistsError(f"Frozen manifest differs from existing file {path}")
    for path, data in outputs.items():
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(data)
    for manifest in manifests:
        print(f"{manifest['name']}: {manifest['sample_count']} questions, "
              f"manifest SHA-256 {manifest['manifest_sha256']}")


if __name__ == "__main__":
    main()
