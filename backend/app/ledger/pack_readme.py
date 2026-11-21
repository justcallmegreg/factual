"""The plain-language README of an evidence pack. Prose lines may run long."""

from __future__ import annotations

RELATION = {
    "record.edited": "a later version (edit) of",
    "record.filed": "a project choice for",
    "attachment.stored": "the stored copy of a file of",
    "attachment.unavailable": "a file that could not be stored for",
    "record.created": "an album item of",
}


def readme(manifest: dict) -> str:
    records = ", ".join(f"E{seq}" for seq in manifest["records"])
    fingerprint = manifest["key_fingerprint"]
    lines = [
        f"FACTUAL EVIDENCE PACK — {records}",
        f"Made {manifest['generated_at']} (UTC). Signing key {fingerprint}.",
        "",
        "WHAT THIS PACK IS",
        "  Factual records the messages that people send to one private Telegram group into an",
        "  append-only, hash-chained ledger, seals that ledger every few minutes with a signature,",
        "  and has independent time-stamping authorities stamp each seal. This pack lets you check,",
        "  without trusting whoever runs Factual, that the records below existed with exactly this",
        "  content by the stamped times, and that nothing in them has changed since.",
        "",
        "TWO KINDS OF TIME",
        "  'Recorded' times are what Telegram and Factual reported from their own clocks: claims,",
        "  like any other content. The proven time is the time-stamping authority's: the content",
        "  existed no later than that moment. Expect the proven time to be minutes after the",
        "  recorded time; a big gap means the record was sealed and stamped later.",
        "",
        "RECORDS",
    ]
    for summary in manifest.get("record_summaries", []):
        seq = summary["seq"]
        if "who" not in summary:
            lines.append(f"  E{seq} · ledger entry ({summary.get('type', 'entry')})")
            continue
        lines.append(f"  E{seq} · project {summary['project'] or 'none yet'} · {summary['who']}")
        if summary.get("text"):
            lines.append(f'    Text: "{summary["text"]}"')
        lines.append(f"    Recorded {summary['recorded_at']} (reported by Telegram and Factual).")
        for edit in summary.get("edits", []):
            lines.append(
                f"    Edited: E{edit['seq']} at {edit['at']} is a later version; both versions are in this pack."
            )
        for item in summary.get("filings", []):
            lines.append(
                f"    Filed: E{item['seq']} puts it under {item['project']} ({item['at']})."
            )
        for att in summary.get("attachments", []):
            lines.append(
                f"    File: {att.get('file_name') or att['file']} ({att.get('mime') or 'unknown type'}), "
                f"stored copy E{att['entry_seq']}, SHA-256 {att['sha256_plain'][:16]}… → {att['file']}"
            )
        proof = next((p for p in manifest["proofs"] if p["seq"] == seq), None)
        if proof is None or proof.get("seal_no") is None:
            lines.append(
                "    NOT YET SEALED: no third-party time covers this record yet (see below)."
            )
        else:
            stamps = [
                a for a in manifest.get("authorities", []) if a["seal_no"] == proof["seal_no"]
            ]
            if stamps:
                for a in stamps:
                    flag = " — TEST AUTHORITY, not independent" if a["test"] else ""
                    lines.append(
                        f"    Proven to exist by {a['gen_time'] or 'see token'} UTC: {a['host']}{flag} (seal {proof['seal_no']})."
                    )
            else:
                lines.append(
                    f"    Sealed in seal {proof['seal_no']}, but the seal carries no timestamp yet."
                )
    lines += ["", "ENTRIES IN THIS PACK"]
    for item in manifest["entries"]:
        rel = next((r for r in manifest.get("related", []) if r["seq"] == item["seq"]), None)
        note = "requested" if rel is None else f"{rel['relation']} E{rel['of']}"
        lines.append(f"  E{item['seq']:<6} {item['type']:<24} {note}")
    lines.append("  Every other entry of the ledger appears only as a hash inside proofs/.")
    lines += ["", "TIME-STAMPING AUTHORITIES"]
    if manifest.get("authorities"):
        for a in manifest["authorities"]:
            cert = (
                f'certificate "{a["subject"]}" issued by "{a["issuer"]}", valid {a["valid_from"]} to {a["valid_to"]}'
                if a["subject"]
                else "certificate not readable"
            )
            lines.append(f"  {a['token']}: {a['url']} — {cert}.")
            if a.get("root_subject"):
                lines.append(
                    f'    Root: "{a["root_subject"]}", fingerprint {a["root_fingerprint"]}. Compare it with the root'
                    " certificate the authority publishes: the chain in this pack comes from the token"
                    " itself and proves consistency, not independence."
                )
            if a["test"]:
                lines.append(
                    "    WARNING: TEST AUTHORITY. It runs on the pack maker's own machine or signs itself;"
                    " its time is not independent evidence."
                )
        lines.append(
            "  Inspect one yourself: openssl x509 -in tsa/chain-1.pem -noout -subject -issuer -dates"
        )
    else:
        lines.append("  None in this pack.")
    lines += [
        "",
        "HOW TO VERIFY (two minutes)",
        "  1. Unpack the zip, open a terminal in the folder, run:  python3 verify.py",
        "     (python3 verify.py <the zip> works too). You need Python 3.9 or newer and",
        "     OpenSSL 1.1.1 or newer. macOS ships LibreSSL, which cannot check timestamps:",
        "     run `brew install openssl`; the script finds it by itself. Debian/Ubuntu:",
        "     `apt install openssl`. Windows: install Python and OpenSSL and run the same command.",
        "  2. Read the result: VERIFIED means every check passed. FAILED means a file was changed:",
        "     do not trust the pack. INCOMPLETE means a tool was missing: nothing is refuted, but the",
        "     timestamps are not confirmed; install OpenSSL and run again.",
        "",
        "WHAT IS CHECKED, IN PLAIN WORDS",
        "  - Entry hash: every entry file is hashed (SHA-256). One changed character gives another",
        "    hash and breaks the chain.",
        "  - Payload commitment: the record's content plus a random salt hashes to the value written",
        "    inside its entry; the salt keeps short messages from being guessed from the hash alone.",
        "  - Chain proof: each entry's hash is folded with the hashes of the entries after it until",
        "    the chain head that the seal names is reached, so nothing between was removed or changed.",
        "  - Seal signature: the seal is signed with the instance's Ed25519 key (keys/…pub); the",
        "    key's fingerprint is recomputed and compared with the one above and inside each seal.",
        "  - Timestamps: each RFC 3161 token is checked with `openssl ts -verify` against the",
        "    authority's certificate chain; the token's time is the proven time.",
        "  - Attachments: each file in blobs/ is hashed and compared with its stored-copy entry.",
        "  Formatting of the JSON files (spaces, line breaks) is not protected, only their content.",
        "",
        "BY HAND, FOR THIS PACK",
    ]
    for a in manifest.get("authorities", []):
        lines.append(
            f"  openssl ts -verify -in {a['token']} -data seals/{a['seal_no']}.stamped -CAfile {a['chain']}"
        )
    for item in manifest["seals"]:
        lines.append(
            f"  openssl pkeyutl -verify -pubin -inkey keys/instance-ed25519.pub -rawin"
            f" -in seals/{item['no']}.json -sigfile seals/{item['no']}.sig"
        )
    lines.append(
        f"  openssl pkey -pubin -in keys/instance-ed25519.pub -outform DER | shasum -a 256   # {fingerprint[7:]}"
    )
    for att in manifest.get("attachments", []):
        lines.append(
            f"  shasum -a 256 {att['file']}   # {att['sha256_plain']} (sha256_plain in payloads/{att['entry_seq']}.json)"
        )
    lines += [
        '  Recipe: entry hash = SHA-256("ledger-entry-v1" + entry bytes); chain(n) = SHA-256(',
        '  "ledger-chain-v1" + chain(n-1) + entry hash(n)); payload commitment = SHA-256(salt +',
        '  canonical payload); seal hash = SHA-256("ledger-seal-v1" + seal bytes). Canonical means',
        "  RFC 8785 JSON: sorted keys, no spaces. verify.py applies exactly this recipe.",
        "",
        "LIMITS",
        "  - README.txt and manifest.json are labels for your convenience and are not signed. What is",
        "    proven is the content of entries/, payloads/, seals/ and blobs/. Compare names, dates and",
        "    numbers in this text against the payload files before you rely on them.",
        "  - A record proves what was sent and when, not that it is true, that a photo was genuine",
        "    before it was sent, or who typed on an account. Telegram supplied the sender.",
        "  - Only the requested records and the entries that belong to them are disclosed. A later",
        "    edit, reply or correction outside this pack is not shown; ask for a fresh pack when",
        "    that matters.",
        "  - A reply or a forwarded message carries, in its raw payload, what it quotes: the text of",
        "    the message replied to, or the forwarded original's sender and time. Disclosing such a",
        "    record discloses that quoted content too.",
    ]
    if manifest.get("unsealed"):
        lines += [
            "  - "
            + ", ".join(f"E{seq}" for seq in manifest["unsealed"])
            + " are not yet sealed: their chain proofs reach the current chain head, but no",
            "    third-party time covers them yet. Ask for a new pack after the next seal.",
        ]
    lines += ["", f"Generated {manifest['generated_at']} · key {fingerprint}", ""]
    return "\n".join(lines)
